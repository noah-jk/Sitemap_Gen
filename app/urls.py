"""Where each page will live on the new site.

A page's planned URL is, in order: the URL typed for it (`new_url`); for a page from the crawl, its
current path; for a new page, its parent's planned URL plus a slug of its name. New pages follow the
sitemap, so moving or renaming one changes its URL until a URL is typed for it. Parents come from the
sitemap as shown (pages being removed are skipped), and clashes get -2, -3, ...
"""
import sqlite3
from typing import Dict, Optional

import sitemap2proto as sp


def planned(conn: sqlite3.Connection) -> Dict[int, str]:
    """Planned URL path for every page."""
    from app import tree
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, parent_id, label, url_path, new_url, is_new FROM pages")}
    parent: Dict[int, Optional[int]] = {r["id"]: r["parent_id"] for r in rows.values()}
    for node, up, _ in tree.walk(tree.load(conn)):  # the sitemap as shown
        parent[node.id] = up.id if up else None
    out: Dict[int, str] = {}
    taken = {r["url_path"] for r in rows.values() if r["url_path"] and not r["new_url"]}
    taken |= {r["new_url"] for r in rows.values() if r["new_url"]}
    for r in rows.values():
        if r["new_url"]:
            out[r["id"]] = r["new_url"]
        elif not r["is_new"] and r["url_path"]:
            out[r["id"]] = r["url_path"]

    def resolve(page_id: int, seen: frozenset) -> str:
        if page_id in out:
            return out[page_id]
        up = parent.get(page_id)
        base = resolve(up, seen | {page_id}) if up is not None and up in rows and up not in seen else ""
        stem = f"{base.rstrip('/')}/{sp.slugify(rows[page_id]['label'])}"
        candidate, n = stem, 2
        while candidate in taken:
            candidate, n = f"{stem}-{n}", n + 1
        taken.add(candidate)
        out[page_id] = candidate
        return candidate

    for page_id in sorted(rows):  # oldest first, so a page keeps its URL when a later one clashes
        resolve(page_id, frozenset())
    return out


def taken_by(conn: sqlite3.Connection, path: str, page_id: int) -> Optional[str]:
    """The label of another page that already has this path (typed, or its current URL), if any."""
    row = conn.execute(
        "SELECT label FROM pages WHERE id <> ? AND (new_url = ? OR (url_path = ? AND new_url = '')) "
        "AND COALESCE(decision, recommendation, '') NOT IN ('delete', 'combine', 'split') LIMIT 1",
        (page_id, path, path)).fetchone()
    return row["label"] if row else None
