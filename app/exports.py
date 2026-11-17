"""Client deliverables built from a project database: content audit, redirect map, Markdown sitemap."""
import csv
import io
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from app import rules
from app.importer import host_aliases, page_key

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
BOM = "﻿"  # lets Excel detect UTF-8


def safe_cell(value) -> str:
    """Text that Excel would run as a formula gets a leading apostrophe. Numbers are left alone."""
    if value is None:
        return ""
    if isinstance(value, str):
        return "'" + value if value.startswith(FORMULA_START) else value
    return str(value)


def _csv(header: List[str], rows: List[list]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(header)
    w.writerows([[safe_cell(v) for v in r] for r in rows])
    return BOM + out.getvalue()


# ---- content audit -----------------------------------------------------------

AUDIT_COLUMNS: List[Tuple[str, str]] = [
    ("Address", "current_url"), ("Recommendation", "recommendation"), ("Final Decision", "decision"),
    ("Notes", "notes"), ("Redirect To", "redirect_url"), ("Content Goes To", "content_urls"),
    ("New URL", "planned_url"), ("Owner", "owner"), ("Writer", "writer"), ("Priority", "priority"),
    ("Suggested Content", "suggested_content"),
    ("Status", "status"), ("Page Type", "type"), ("Label", "label"), ("Title", "title"), ("H1", "h1"),
    ("Meta Description", "meta_description"), ("HTTP Status", "status_code"), ("Crawl Depth", "crawl_depth"),
    ("Inlinks", "inlinks"), ("Unique Inlinks", "unique_inlinks"), ("% of Total", "pct_of_total"),
    ("In Current Nav", "in_nav"), ("Collection", "collection"), ("GA4 Sessions", "sessions"),
    ("GA4 Views", "views"), ("GA4 Engaged Sessions", "engaged_sessions"),
    ("GA4 Engagement Rate", "engagement_rate"), ("GA4 Key Events", "key_events"),
    ("Indexability", "indexability"), ("Word Count", "word_count"), ("No. Near Duplicates", "near_duplicates"),
    ("Closest Near Duplicate Match", "closest_near_duplicate"), ("Missing From Latest Crawl", "missing"),
]
ALWAYS_KEEP = {"Address", "Recommendation", "Final Decision", "Notes"}


def site_base(conn) -> str:
    """https://host of the site, for turning planned paths into addresses."""
    host = (conn.execute("SELECT canonical_host FROM settings").fetchone() or {"canonical_host": ""})["canonical_host"]
    row = conn.execute("SELECT current_url FROM pages WHERE current_url LIKE 'http%' LIMIT 1").fetchone()
    scheme = urlsplit(row["current_url"]).scheme if row else "https"
    return f"{scheme}://{host}" if host else ""


def planned_addresses(conn) -> Dict[int, str]:
    """Every page's address on the new site: its current address unless it's new or moving."""
    from app import urls
    base = site_base(conn)
    current = {r["id"]: (r["current_url"], r["url_path"], r["new_url"])
               for r in conn.execute("SELECT id, current_url, url_path, new_url FROM pages")}
    out = {}
    for page_id, path in urls.planned(conn).items():
        cur, key, typed = current[page_id]
        out[page_id] = cur if cur and not typed else base + path
    return out


def content_audit_csv(conn) -> str:
    rows = conn.execute(
        "SELECT p.*, p.redirect_to AS redirect_id, "
        "CASE p.in_current_nav WHEN 1 THEN 'Yes' ELSE '' END AS in_nav, "
        "CASE p.missing_from_crawl WHEN 1 THEN 'Yes' ELSE '' END AS missing, "
        "(SELECT json_group_array(c.target_id) FROM page_content_targets c WHERE c.page_id = p.id) AS content_ids "
        "FROM pages p ORDER BY p.is_new, p.url_path, p.label").fetchall()
    from app import tree, urls
    resolved = {n.id: n.type for n, _, _ in tree.walk(tree.load(conn))}
    planned, addresses = urls.planned(conn), planned_addresses(conn)

    def new_url(r) -> str:  # only when it differs from today's
        return addresses[r["id"]] if r["is_new"] or (r["new_url"] and r["new_url"] != r["url_path"]) else ""

    rows = [dict(r, type=resolved.get(r["id"], r["page_type"]), planned_url=new_url(r),
                 redirect_url=addresses.get(r["redirect_id"], "") if r["redirect_id"] else "",
                 content_urls="; ".join(addresses[i] for i in json.loads(r["content_ids"]) if i in addresses))
            for r in rows]
    extras = [json.loads(r["crawl_extra"] or "{}") for r in rows]
    extra_cols = list(dict.fromkeys(k for e in extras for k in e))
    header = [h for h, _ in AUDIT_COLUMNS] + extra_cols
    table = [[r[c] for _, c in AUDIT_COLUMNS] + [e.get(k, "") for k in extra_cols] for r, e in zip(rows, extras)]
    keep = [i for i, h in enumerate(header) if h in ALWAYS_KEEP or any(row[i] not in (None, "", 0) for row in table)]
    return _csv([header[i] for i in keep], [[row[i] for i in keep] for row in table])


# ---- redirect map ------------------------------------------------------------

@dataclass
class RedirectMap:
    rows: List[list] = field(default_factory=list)   # type, from path, from URL, to URL, note
    blocking: Dict[int, str] = field(default_factory=dict)   # page id -> problem
    blocking_labels: Dict[int, str] = field(default_factory=dict)
    undecided: int = 0        # recommended for removal, no final decision yet
    will_404: int = 0         # deleted with no redirect
    unresolved: List[str] = field(default_factory=list)  # existing redirects that would end on a 404

    @property
    def ok(self) -> bool:
        return not self.blocking

    def csv(self) -> str:
        return _csv(["Type", "From Path", "From URL", "To URL", "Status Code", "Note"],
                    [[t, fp, fu, tu, 301, note] for t, fp, fu, tu, note in self.rows])


def _path(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def redirect_map(conn) -> RedirectMap:
    m = RedirectMap()
    decided = [r["id"] for r in conn.execute("SELECT id FROM pages WHERE decision IS NOT NULL")]
    m.blocking = rules.problems(conn, decided)
    m.blocking_labels = {r["id"]: r["label"] for r in conn.execute(
        "SELECT id, label FROM pages WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(list(m.blocking)),))}
    m.undecided = conn.execute(
        "SELECT count(*) FROM pages WHERE decision IS NULL AND recommendation IN ('delete','combine','split')"
    ).fetchone()[0]
    settings = conn.execute("SELECT canonical_host FROM settings").fetchone()
    canonical = settings["canonical_host"] if settings else ""
    aliases = host_aliases(canonical) if canonical else set()

    addresses = planned_addresses(conn)  # new pages and moving pages: where they'll be

    # Removed pages: url key -> final URL ('' when the page is deleted with no redirect).
    final: Dict[str, str] = {}
    for r in conn.execute(
            "SELECT p.id, p.label, p.decision, p.current_url, p.url_path, p.redirect_to FROM pages p "
            "WHERE p.decision IN ('delete','combine','split') AND p.url_path IS NOT NULL ORDER BY p.url_path"):
        if r["id"] in m.blocking:
            continue
        to_url = addresses.get(r["redirect_to"], "") if r["redirect_to"] else ""
        if to_url:
            final[r["url_path"]] = to_url
            m.rows.append(["page", _path(r["current_url"]), r["current_url"], to_url,
                           f'{r["label"]} ({r["decision"]})'])
        else:
            final[r["url_path"]] = ""
            m.will_404 += 1

    # Pages that stay but move to a new URL.
    for r in conn.execute(
            "SELECT id, label, current_url, url_path, new_url FROM pages WHERE is_new = 0 AND url_path IS NOT NULL "
            "AND new_url <> '' AND new_url IS NOT url_path "
            "AND COALESCE(decision, recommendation, '') NOT IN ('delete','combine','split') ORDER BY url_path"):
        final[r["url_path"]] = addresses[r["id"]]
        m.rows.append(["moved", _path(r["current_url"]), r["current_url"], addresses[r["id"]],
                       f'{r["label"]} (new URL)'])

    # Existing redirects that land on a removed page (possibly through other old redirects).
    existing = {page_key(r["source_url"], aliases): r for r in conn.execute("SELECT * FROM existing_redirects")}
    for key, r in sorted(existing.items(), key=lambda kv: kv[1]["source_url"]):
        hop, seen = page_key(r["target_url"], aliases), {key}
        while hop in existing and hop not in seen and hop not in final:
            seen.add(hop)
            hop = page_key(existing[hop]["target_url"], aliases)
        if hop in final:
            if final[hop]:
                m.rows.append(["existing", _path(r["source_url"]), r["source_url"], final[hop],
                               f"Old redirect re-pointed (used to go to {r['target_url']})"])
            else:
                m.unresolved.append(f"{r['source_url']} redirects to {r['target_url']}, which is being deleted")

    duplicate_host = conn.execute("SELECT 1 FROM site_issues WHERE kind = 'duplicate_host'").fetchone()
    if duplicate_host and canonical:
        other = next(iter(aliases - {canonical}))
        m.rows.insert(0, ["host", "/*", f"https://{other}/*", f"https://{canonical}/*",
                          "Redirect the whole host, keeping the path"])
    return m


# ---- Markdown sitemap --------------------------------------------------------

def markdown_sitemap(conn, project_name: str, today: str) -> str:
    """The to-be tree as a nested list the sitemap2proto.py CLI can read."""
    from app import tree
    lines = [f"{'  ' * (depth - 1)}- {node.label}{' [button]' if node.is_button else ''}"
             for node, _, depth in tree.walk(tree.load(conn))]
    collections = conn.execute("SELECT collection, count(*) FROM pages WHERE collection <> '' "
                               "AND COALESCE(decision, recommendation, '') NOT IN ('delete','combine','split') "
                               "GROUP BY collection ORDER BY collection").fetchall()
    note = ", ".join(f"{c} ({n} pages)" for c, n in collections)
    header = [f"# {' '.join(project_name.split())} sitemap",
              f"# Exported from Sitemap Gen on {today}. {len(lines)} pages.",
              "# Pages being deleted, combined, or split are left out."]
    if note:
        header.append(f"# Collections left out: {note}.")
    return "\n".join(header + [""] + lines) + "\n"
