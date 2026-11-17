"""Screaming Frog "Internal: All" CSV -> pages, existing redirects, and site issues."""
import collections
import csv
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit

from app.db import now_ms

REQUIRED = ("Address", "Content Type", "Status Code")

# Screaming Frog column -> pages column, for the row that represents the page.
CRAWL_COLUMNS = {
    "Title 1": "title",
    "Meta Description 1": "meta_description",
    "Canonical Link Element 1": "canonical",
    "Crawl Depth": "crawl_depth",
    "Inlinks": "inlinks",
    "Unique Inlinks": "unique_inlinks",
    "% of Total": "pct_of_total",
    "Hash": "hash",
    "Indexability": "indexability",
    "Word Count": "word_count",
    "No. Near Duplicates": "near_duplicates",
    "Closest Near Duplicate Match": "closest_near_duplicate",
    "GA4 Engagement rate": "engagement_rate",
}
INT_COLUMNS = {"crawl_depth", "inlinks", "unique_inlinks", "word_count", "near_duplicates"}
FLOAT_COLUMNS = {"pct_of_total", "engagement_rate"}
# Summed across hosts (each host has its own traffic in GA4).
GA_COLUMNS = {"GA4 Sessions": "sessions", "GA4 Views": "views",
              "GA4 Engaged sessions": "engaged_sessions", "GA4 Key events": "key_events"}
IGNORED = {"Address", "URL Encoded Address", "Content Type", "Status Code", "Status", "H1-1", "H1-2",
           "Redirect URL", "Redirect Type", "Last Modified", "Crawl Timestamp", "GA4 Total revenue",
           *GA_COLUMNS, *CRAWL_COLUMNS}
IGNORED_PREFIXES = ("H2-", "Closest Semantically", "Semantic ", "No. Semantically")

# Optional Screaming Frog columns the app uses; when a crawl leaves them empty, the import says so.
WATCHED_COLUMNS = ("Word Count", "No. Near Duplicates")

NAV_THRESHOLD = 25.0  # % of Total: pages linked from (nearly) every page
COLLECTION_SECTIONS = ("news", "calendar", "events", "blog", "stories")
FILE_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|jpe?g|png|gif|svg|webp|mp[34]|csv|txt|ics)$", re.I)
FILE_DIRS = ("/sites/default/files/", "/wp-content/uploads/", "/files/")
HOST_IN_PATH = re.compile(
    r"(^|/)(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)*\.(edu|com|org|net|gov|io|us|uk|co|ly|gle|me|info)(/|$)", re.I)
EMAIL_IN_PATH = re.compile(r"[^/@]+@[a-z0-9-]+(\.[a-z0-9-]+)+$", re.I)
BAD_MARKUP = re.compile(r'["\\<>]|&nbsp|&amp;|%22|%5C', re.I)
MALFORMED_DETAIL = {
    "email": "Email address linked without mailto: on the live site",
    "host": "Link is missing https:// on the live site",
    "markup": "Broken link markup (stray characters in the href) on the live site",
}

UPDATABLE = ["current_url", "title", "h1", "meta_description", "canonical", "status_code", "crawl_depth",
             "inlinks", "unique_inlinks", "pct_of_total", "hash", "indexability", "word_count",
             "near_duplicates", "closest_near_duplicate", "in_current_nav", "sessions", "views",
             "engaged_sessions", "engagement_rate", "key_events", "variants", "crawl_extra"]


@dataclass
class Summary:
    rows_read: int = 0
    pages_new: int = 0
    pages_updated: int = 0
    pages_missing: int = 0
    error_pages: int = 0
    redirects: int = 0
    skipped: Dict[str, int] = field(default_factory=lambda: collections.defaultdict(int))
    site_issues: Dict[str, int] = field(default_factory=lambda: collections.defaultdict(int))
    canonical_host: str = ""
    title_suffix: str = ""
    empty_columns: List[str] = field(default_factory=list)


class CrawlFormatError(ValueError):
    pass


def _int(v: str) -> int:
    try:
        return int(float(v)) if v and v.strip() else 0
    except ValueError:
        return 0


def _coerce(col: str, v: str):
    if col in INT_COLUMNS:
        return _int(v) if v.strip() else None
    if col in FLOAT_COLUMNS:
        try:
            return float(v) if v.strip() else None
        except ValueError:
            return None
    return v.strip()


def host_aliases(host: str) -> set:
    bare = host[4:] if host.startswith("www.") else host
    return {bare, "www." + bare}


def page_key(url: str, aliases: set) -> Optional[str]:
    """Normalized identity of a page: path without trailing slash, sorted query, no fragment.
    Pages on hosts outside the site's aliases keep their host in the key."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    path = parts.path.rstrip("/") or "/"
    if parts.query:
        path += "?" + urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    host = parts.netloc.lower()
    return path if host in aliases else f"//{host}{path}"


def detect_title_suffix(titles: Iterable[str]) -> str:
    titles = [t for t in titles if t]
    ends = collections.Counter(t.rsplit(" | ", 1)[1] for t in titles if " | " in t)
    if not ends:
        return ""
    common, n = ends.most_common(1)[0]
    return " | " + common if n >= len(titles) / 2 else ""


def make_label(title: str, h1: str, key: str, suffix: str) -> str:
    label = title
    if suffix:
        site = suffix[3:]
        label = label.removesuffix(suffix)
        if label.startswith(site + " | "):
            label = label[len(site) + 3:]
    label = label.strip() or h1.strip()
    if not label:
        tail = key.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        label = tail or "Home"
    return label


def _classify_error(path: str) -> Tuple[Optional[str], str]:
    """Error URLs that aren't real pages: (kind, detail), or (None, '') for a page that's gone."""
    if EMAIL_IN_PATH.search(path):
        return "malformed_link", MALFORMED_DETAIL["email"]
    if BAD_MARKUP.search(path):
        return "malformed_link", MALFORMED_DETAIL["markup"]
    if HOST_IN_PATH.search(path):
        return "malformed_link", MALFORMED_DETAIL["host"]
    if FILE_EXT.search(path) or any(path.startswith(d) for d in FILE_DIRS):
        return "broken_file_link", ""
    return None, ""


def import_crawl(conn: sqlite3.Connection, lines: Iterable[str], filename: str,
                 ga_date_range: str = "") -> Summary:
    reader = csv.DictReader(lines)
    header = reader.fieldnames or []
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise CrawlFormatError(f"not a Screaming Frog export: missing column(s) {', '.join(missing)}")

    s = Summary()
    html_rows: List[dict] = []
    crawl_times: List[str] = []
    for row in reader:
        s.rows_read += 1
        if not row.get("Content Type", "").lower().startswith("text/html"):
            ctype = (row.get("Content Type") or "unknown").split(";")[0].strip() or "unknown"
            s.skipped[f"non-HTML ({ctype})"] += 1
            continue
        html_rows.append(row)
        if row.get("Crawl Timestamp"):
            crawl_times.append(row["Crawl Timestamp"])

    ok_rows = [r for r in html_rows if r["Status Code"] == "200"]
    s.empty_columns = [c for c in WATCHED_COLUMNS
                       if ok_rows and not any((r.get(c) or "").strip() for r in ok_rows)]

    settings = conn.execute("SELECT canonical_host, title_suffix FROM settings").fetchone()
    ok_hosts = collections.Counter(urlsplit(r["Address"]).netloc.lower()
                                   for r in html_rows if r["Status Code"] == "200")
    if settings["canonical_host"]:
        canonical = settings["canonical_host"]
    elif ok_hosts:
        top = max(ok_hosts.values())
        tied = sorted(h for h, n in ok_hosts.items() if n == top)
        canonical = next((h for h in tied if h.startswith("www.")), tied[0])
    else:
        canonical = ""
    aliases = host_aliases(canonical) if canonical else set()
    s.canonical_host = canonical
    s.title_suffix = settings["title_suffix"] or detect_title_suffix(
        r.get("Title 1", "") for r in html_rows if r["Status Code"] == "200")

    groups: Dict[str, List[dict]] = collections.defaultdict(list)
    for r in html_rows:
        key = page_key(r["Address"], aliases)
        if key is None:
            s.skipped["not http(s)"] += 1
            continue
        groups[key].append(r)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE settings SET canonical_host = ?, title_suffix = ?", (canonical, s.title_suffix))
        import_id = conn.execute(
            "INSERT INTO imports(filename, crawl_timestamp, ga_date_range, imported_at) VALUES (?,?,?,?)",
            (filename, min(crawl_times) if crawl_times else None, ga_date_range, now_ms())).lastrowid
        seen = _write_groups(conn, groups, canonical, s, import_id, header)
        cur = conn.execute(
            f"UPDATE pages SET missing_from_crawl = 1 WHERE is_new = 0 AND url_path IS NOT NULL "
            f"AND url_path NOT IN (SELECT value FROM json_each(?))", (json.dumps(sorted(seen)),))
        s.pages_missing = cur.rowcount
        conn.execute("UPDATE imports SET summary = ? WHERE id = ?", (json.dumps(summary_dict(s)), import_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return s


def _write_groups(conn, groups, canonical, s: Summary, import_id: int, header: List[str]) -> set:
    existing = {r["url_path"]: r["id"] for r in conn.execute("SELECT id, url_path FROM pages WHERE url_path IS NOT NULL")}
    extra_cols = [h for h in header if h not in IGNORED and not h.startswith(IGNORED_PREFIXES)]
    seen = set()
    new_pages: List[Tuple[int, str]] = []
    both_hosts = 0
    now = now_ms()

    for key in sorted(groups):
        rows = groups[key]
        for r in rows:
            if r["Status Code"].startswith("3"):
                conn.execute(
                    "INSERT INTO existing_redirects(source_url, target_url, status_code, import_id) VALUES (?,?,?,?) "
                    "ON CONFLICT(source_url) DO UPDATE SET target_url=excluded.target_url, "
                    "status_code=excluded.status_code, import_id=excluded.import_id",
                    (r["Address"], r.get("Redirect URL", ""), _int(r["Status Code"]), import_id))
                s.redirects += 1

        ok = [r for r in rows if r["Status Code"] == "200"]
        errors = [r for r in rows if r["Status Code"][:1] in ("4", "5")]
        if ok:
            best = next((r for r in ok if urlsplit(r["Address"]).netloc.lower() == canonical), ok[0])
            if len({urlsplit(r["Address"]).netloc.lower() for r in ok}) > 1:
                both_hosts += 1
        elif errors:
            kind, detail = _classify_error(key)
            if kind == "broken_file_link":
                s.skipped["broken file link (404 to a document)"] += 1
                continue
            if kind == "malformed_link":
                for r in errors:
                    conn.execute("INSERT OR IGNORE INTO site_issues(kind, url, detail, import_id) VALUES (?,?,?,?)",
                                 (kind, r["Address"], detail, import_id))
                s.site_issues[kind] += 1
                continue
            best = errors[0]
        else:
            continue  # redirect only

        seen.add(key)
        per_host: Dict[str, Dict[str, int]] = collections.defaultdict(dict)
        for r in rows:
            host = urlsplit(r["Address"]).netloc.lower()
            for col, name in GA_COLUMNS.items():
                per_host[host][name] = max(per_host[host].get(name, 0), _int(r.get(col, "")))
        ga = {name: sum(h.get(name, 0) for h in per_host.values()) for name in GA_COLUMNS.values()}

        title = best.get("Title 1", "").strip()
        h1 = (best.get("H1-1") or best.get("H1-2") or "").strip()
        values = {
            "current_url": best["Address"],
            "label": make_label(title, h1, key, s.title_suffix),
            "h1": h1,
            "status_code": _int(best["Status Code"]),
            "variants": json.dumps(sorted(r["Address"] for r in rows)),
            "crawl_extra": json.dumps({h: best[h] for h in extra_cols if best.get(h, "").strip()}),
            **{col: _coerce(col, best.get(sf, "")) for sf, col in CRAWL_COLUMNS.items()},
            **ga,
        }
        values["in_current_nav"] = int((values["pct_of_total"] or 0) >= NAV_THRESHOLD)

        if key in existing:
            sets = ", ".join(f"{c} = ?" for c in UPDATABLE)
            conn.execute(f"UPDATE pages SET {sets}, missing_from_crawl = 0, import_id = ?, updated_at = ? WHERE id = ?",
                         [values[c] for c in UPDATABLE] + [import_id, now, existing[key]])
            s.pages_updated += 1
        else:
            if not ok:
                ga_note = " but still visited; likely needs a redirect" if ga["sessions"] else ""
                values.update(recommendation="review", notes=f"HTTP {values['status_code']} in crawl{ga_note}")
            segs = key.split("?")[0].strip("/").split("/")
            if ok and len(segs) > 1 and segs[0] in COLLECTION_SECTIONS:
                values["collection"] = segs[0]
            values.update(url_path=key, import_id=import_id, updated_at=now)
            cols = list(values)
            page_id = conn.execute(f"INSERT INTO pages({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                   [values[c] for c in cols]).lastrowid
            new_pages.append((page_id, key))
            s.pages_new += 1
        if not ok:
            s.error_pages += 1

    if both_hosts:
        conn.execute("INSERT OR REPLACE INTO site_issues(kind, url, detail, import_id) VALUES (?,?,?,?)",
                     ("duplicate_host", canonical,
                      f"{both_hosts} pages answer on more than one host with no redirect between them", import_id))
        s.site_issues["duplicate_host"] += 1

    _place_new_pages(conn, new_pages)
    return seen


def _place_new_pages(conn, new_pages: List[Tuple[int, str]]) -> None:
    """Initial tree position: the nearest existing ancestor URL. Only new pages are placed,
    so re-imports never move pages someone has arranged."""
    by_path = {r["url_path"]: r["id"] for r in conn.execute("SELECT id, url_path FROM pages WHERE url_path IS NOT NULL")}
    for page_id, key in new_pages:
        if key.startswith("//") or "?" in key or key == "/":
            continue
        parts = key.strip("/").split("/")
        parent = next((by_path[p] for p in ("/" + "/".join(parts[:i]) for i in range(len(parts) - 1, 0, -1))
                       if p in by_path), None)
        if parent is not None:
            conn.execute("UPDATE pages SET parent_id = ? WHERE id = ?", (parent, page_id))


def summary_dict(s: Summary) -> dict:
    return {"rows_read": s.rows_read, "pages_new": s.pages_new, "pages_updated": s.pages_updated,
            "pages_missing": s.pages_missing, "error_pages": s.error_pages, "redirects": s.redirects,
            "skipped": dict(s.skipped), "site_issues": dict(s.site_issues),
            "canonical_host": s.canonical_host, "title_suffix": s.title_suffix,
            "empty_columns": s.empty_columns}
