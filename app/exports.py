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
    ("Status", "status"), ("Label", "label"), ("Title", "title"), ("H1", "h1"),
    ("Meta Description", "meta_description"), ("HTTP Status", "status_code"), ("Crawl Depth", "crawl_depth"),
    ("Inlinks", "inlinks"), ("Unique Inlinks", "unique_inlinks"), ("% of Total", "pct_of_total"),
    ("In Current Nav", "in_nav"), ("Collection", "collection"), ("GA4 Sessions", "sessions"),
    ("GA4 Views", "views"), ("GA4 Engaged Sessions", "engaged_sessions"),
    ("GA4 Engagement Rate", "engagement_rate"), ("GA4 Key Events", "key_events"),
    ("Indexability", "indexability"), ("Word Count", "word_count"), ("No. Near Duplicates", "near_duplicates"),
    ("Closest Near Duplicate Match", "closest_near_duplicate"), ("Missing From Latest Crawl", "missing"),
]
ALWAYS_KEEP = {"Address", "Recommendation", "Final Decision", "Notes"}


def content_audit_csv(conn) -> str:
    rows = conn.execute(
        "SELECT p.*, COALESCE(t.current_url, t.label) AS redirect_url, "
        "CASE p.in_current_nav WHEN 1 THEN 'Yes' ELSE '' END AS in_nav, "
        "CASE p.missing_from_crawl WHEN 1 THEN 'Yes' ELSE '' END AS missing, "
        "(SELECT group_concat(COALESCE(x.current_url, x.label), '; ') FROM page_content_targets c "
        " JOIN pages x ON x.id = c.target_id WHERE c.page_id = p.id) AS content_urls "
        "FROM pages p LEFT JOIN pages t ON t.id = p.redirect_to ORDER BY p.is_new, p.url_path, p.label").fetchall()
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

    # Removed pages: url key -> final URL ('' when the page is deleted with no redirect).
    final: Dict[str, str] = {}
    for r in conn.execute(
            "SELECT p.id, p.label, p.decision, p.current_url, p.url_path, t.current_url AS to_url, t.label AS to_label, "
            "t.is_new AS to_new FROM pages p LEFT JOIN pages t ON t.id = p.redirect_to "
            "WHERE p.decision IN ('delete','combine','split') AND p.url_path IS NOT NULL ORDER BY p.url_path"):
        if r["id"] in m.blocking:
            continue
        if r["to_url"]:
            final[r["url_path"]] = r["to_url"]
            m.rows.append(["page", _path(r["current_url"]), r["current_url"], r["to_url"],
                           f'{r["label"]} ({r["decision"]})'])
        elif r["to_new"]:
            final[r["url_path"]] = ""
            m.unresolved.append(f'{r["current_url"]} → new page "{r["to_label"]}" has no URL yet')
        else:
            final[r["url_path"]] = ""
            m.will_404 += 1

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
