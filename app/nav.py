"""Menus (main, utility, footer): built by adding entries that point at pages in the sitemap.

The sitemap (pages.parent_id) says where every page lives. Menus are a separate, smaller
structure: you add what belongs in them instead of removing what doesn't.
"""
import json
import sqlite3
from typing import Dict, List, Optional, Set

from app.db import now_ms
from app.rules import REMOVED, RuleError

MENUS = ("main", "utility", "footer")
MENU_NAMES = {"main": "Main menu", "utility": "Utility nav", "footer": "Footer"}
MAX_LEVELS = {"main": 4, "utility": 2, "footer": 2}
MAX_TOP = {"footer": 4}
MAX_LABEL = 100


def _problem(r) -> str:
    if r["page_id"] is None:
        return ""
    if r["e"] in REMOVED:
        return f"This page is marked {r['e']}; it won't exist after launch."
    if (r["status_code"] or 0) >= 400 and r["e"] != "keep":
        return f"This page returns HTTP {r['status_code']}."
    if r["collection"]:
        return f"{r['collection'].capitalize()} items aren't built in the prototype."
    return ""


def load(conn: sqlite3.Connection) -> Dict[str, List[dict]]:
    rows = conn.execute(
        "SELECT n.id, n.menu, n.parent_id, n.page_id, n.label, n.sort_order, n.is_button, n.updated_at, "
        "p.label AS page_label, p.url_path, p.is_new, p.status_code, p.collection, "
        "COALESCE(p.decision, p.recommendation) AS e "
        "FROM nav_items n LEFT JOIN pages p ON p.id = n.page_id ORDER BY n.sort_order, n.id").fetchall()
    items = {r["id"]: {"id": r["id"], "menu": r["menu"], "page_id": r["page_id"], "label": r["label"],
                       "display": r["label"] or r["page_label"], "page_label": r["page_label"],
                       "url_path": r["url_path"], "is_new": bool(r["is_new"]), "is_button": bool(r["is_button"]),
                       "heading": r["page_id"] is None, "problem": _problem(r), "updated_at": r["updated_at"],
                       "children": []} for r in rows}
    menus: Dict[str, List[dict]] = {m: [] for m in MENUS}
    for r in rows:
        parent = items.get(r["parent_id"])
        (parent["children"] if parent else menus[r["menu"]]).append(items[r["id"]])
    return menus


def page_menus(conn: sqlite3.Connection) -> Dict[int, List[str]]:
    out: Dict[int, Set[str]] = {}
    for page_id, menu in conn.execute("SELECT page_id, menu FROM nav_items WHERE page_id IS NOT NULL"):
        out.setdefault(page_id, set()).add(menu)
    return {k: [m for m in MENUS if m in v] for k, v in out.items()}


# ---- checks ------------------------------------------------------------------

def _item(conn, item_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM nav_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise KeyError(item_id)
    return row


def _level(conn, item_id: Optional[int]) -> int:
    """1 for a top-level entry, 2 for its children, ... (0 for the menu itself)."""
    if item_id is None:
        return 0
    return conn.execute(
        "WITH RECURSIVE up(id, parent_id, n) AS (SELECT id, parent_id, 1 FROM nav_items WHERE id = ? "
        "UNION ALL SELECT i.id, i.parent_id, up.n + 1 FROM nav_items i JOIN up ON i.id = up.parent_id) "
        "SELECT max(n) FROM up", (item_id,)).fetchone()[0]


def _height(conn, item_id: int) -> int:
    """Levels in the subtree under and including this entry."""
    return conn.execute(
        "WITH RECURSIVE down(id, n) AS (SELECT ?, 1 UNION ALL SELECT i.id, down.n + 1 FROM nav_items i "
        "JOIN down ON i.parent_id = down.id) SELECT max(n) FROM down", (item_id,)).fetchone()[0]


def _check_place(conn, menu: str, parent: Optional[int], height: int, moving: Optional[int] = None) -> None:
    if menu not in MENUS:
        raise RuleError(["Unknown menu."])
    if parent is not None:
        p = _item(conn, parent)
        if p["menu"] != menu:
            raise RuleError(["That parent entry is in a different menu."])
        if moving is not None:
            below = {r[0] for r in conn.execute(
                "WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT i.id FROM nav_items i JOIN d ON i.parent_id = d.id) "
                "SELECT id FROM d", (moving,))}
            if parent in below:
                raise RuleError(["An entry can't go inside itself."])
    if _level(conn, parent) + height > MAX_LEVELS[menu]:
        raise RuleError([f"The {MENU_NAMES[menu].lower()} only goes {MAX_LEVELS[menu]} levels deep."])
    if parent is None and menu in MAX_TOP:
        tops = conn.execute("SELECT count(*) FROM nav_items WHERE menu = ? AND parent_id IS NULL AND id IS NOT ?",
                            (menu, moving)).fetchone()[0]
        if tops >= MAX_TOP[menu]:
            raise RuleError([f"The footer has room for {MAX_TOP[menu]} columns. Add links inside a column instead."])


def _clean_label(label) -> str:
    return " ".join(str(label or "").split())[:MAX_LABEL]


def _renumber(conn, menu: str, parent: Optional[int], ordered_ids: List[int]) -> None:
    conn.executemany("UPDATE nav_items SET sort_order = ? WHERE id = ?",
                     [((i + 1) * 10, item_id) for i, item_id in enumerate(ordered_ids)])


def _siblings(conn, menu: str, parent: Optional[int], exclude: Optional[int] = None) -> List[int]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM nav_items WHERE menu = ? AND parent_id IS ? AND id IS NOT ? ORDER BY sort_order, id",
        (menu, parent, exclude))]


def _tx(conn, fn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn()
        conn.execute("COMMIT")
        return result
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---- edits -------------------------------------------------------------------

def _children_map(conn) -> Dict[int, List[int]]:
    """Kept sub-pages of each page in sitemap order (removed, gone, and collection pages left out)."""
    from app import tree
    return {node.id: [c.id for c in node.children] for node, _, _ in tree.walk(tree.load(conn))}


def _pull(conn, item_id: int, menu: str, page_id: int, depth: int, kids: Dict[int, List[int]]) -> int:
    """Copy the page's sitemap children under the entry, `depth` levels down, skipping ones already there."""
    room = MAX_LEVELS[menu] - _level(conn, item_id)
    depth = min(depth, room)
    if depth <= 0:
        return 0
    existing = {r[0]: r[1] for r in conn.execute(
        "SELECT page_id, id FROM nav_items WHERE parent_id = ? AND page_id IS NOT NULL", (item_id,))}
    order = len(_siblings(conn, menu, item_id))
    added = 0
    for child in kids.get(page_id, []):
        if child in existing:
            child_item = existing[child]
        else:
            order += 1
            child_item = conn.execute(
                "INSERT INTO nav_items(menu, parent_id, page_id, sort_order, updated_at) VALUES (?, ?, ?, ?, ?)",
                (menu, item_id, child, order * 10, now_ms())).lastrowid
            added += 1
        added += _pull(conn, child_item, menu, child, depth - 1, kids)
    return added


def add_pages(conn: sqlite3.Connection, menu: str, parent: Optional[int], page_ids: List[int],
              subpage_depth: int = 0) -> int:
    """Add pages to the end of a menu or under an entry, optionally with their sitemap sub-pages
    `subpage_depth` levels down. Pages already there are skipped. Returns entries added."""
    page_ids = list(dict.fromkeys(page_ids))

    def run() -> int:
        _check_place(conn, menu, parent, 1)
        found = {r[0] for r in conn.execute(
            "SELECT id FROM pages WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(page_ids),))}
        if len(found) != len(page_ids):
            raise RuleError(["Some of those pages don't exist any more. Reload and try again."])
        present = {r[0] for r in conn.execute(
            "SELECT page_id FROM nav_items WHERE menu = ? AND parent_id IS ?", (menu, parent))}
        new = [p for p in page_ids if p not in present]
        if parent is None and menu in MAX_TOP and len(_siblings(conn, menu, None)) + len(new) > MAX_TOP[menu]:
            raise RuleError([f"The footer has room for {MAX_TOP[menu]} columns. Add links inside a column instead."])
        last = len(_siblings(conn, menu, parent))
        now = now_ms()
        conn.executemany(
            "INSERT INTO nav_items(menu, parent_id, page_id, sort_order, updated_at) VALUES (?, ?, ?, ?, ?)",
            [(menu, parent, p, (last + i + 1) * 10, now) for i, p in enumerate(new)])
        added = len(new)
        if subpage_depth > 0:
            kids = _children_map(conn)
            entries = {r[0]: r[1] for r in conn.execute(
                "SELECT page_id, id FROM nav_items WHERE menu = ? AND parent_id IS ? AND page_id IS NOT NULL",
                (menu, parent))}
            for p in page_ids:
                added += _pull(conn, entries[p], menu, p, subpage_depth, kids)
        return added

    return _tx(conn, run)


def add_subpages(conn: sqlite3.Connection, item_id: int, depth: int = 1) -> int:
    """Pull an entry's sitemap sub-pages in under it (new ones only). Returns entries added."""
    def run() -> int:
        item = _item(conn, item_id)
        if item["page_id"] is None:
            raise RuleError(["Headings aren't pages, so they have no sub-pages to pull in."])
        if _level(conn, item_id) >= MAX_LEVELS[item["menu"]]:
            raise RuleError([f"This entry is already at the deepest level the "
                             f"{MENU_NAMES[item['menu']].lower()} allows."])
        return _pull(conn, item_id, item["menu"], item["page_id"], depth, _children_map(conn))

    return _tx(conn, run)


def add_heading(conn: sqlite3.Connection, menu: str, parent: Optional[int], label: str) -> int:
    label = _clean_label(label)
    if not label:
        raise RuleError(["Give the heading a name."])

    def run() -> int:
        _check_place(conn, menu, parent, 1)
        last = len(_siblings(conn, menu, parent))
        return conn.execute(
            "INSERT INTO nav_items(menu, parent_id, label, sort_order, updated_at) VALUES (?, ?, ?, ?, ?)",
            (menu, parent, label, (last + 1) * 10, now_ms())).lastrowid

    return _tx(conn, run)


def move(conn: sqlite3.Connection, item_id: int, menu: str, parent: Optional[int], index: int) -> None:
    def run() -> None:
        item = _item(conn, item_id)
        _check_place(conn, menu, parent, _height(conn, item_id), moving=item_id)
        ids = _siblings(conn, menu, parent, exclude=item_id)
        ids.insert(max(0, min(index, len(ids))), item_id)
        if item["menu"] != menu:  # the whole subtree changes menu
            conn.execute(
                "WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT i.id FROM nav_items i JOIN d ON i.parent_id = d.id) "
                "UPDATE nav_items SET menu = ? WHERE id IN (SELECT id FROM d)", (item_id, menu))
            if menu != "main":
                conn.execute("UPDATE nav_items SET is_button = 0 WHERE id = ?", (item_id,))
        conn.execute("UPDATE nav_items SET parent_id = ?, updated_at = ? WHERE id = ?", (parent, now_ms(), item_id))
        _renumber(conn, menu, parent, ids)

    _tx(conn, run)


def update(conn: sqlite3.Connection, item_id: int, label: Optional[str] = None,
           is_button: Optional[bool] = None) -> None:
    def run() -> None:
        item = _item(conn, item_id)
        if label is not None:
            clean = _clean_label(label)
            if not clean and item["page_id"] is None:
                raise RuleError(["A heading needs a name."])
            conn.execute("UPDATE nav_items SET label = ? WHERE id = ?", (clean, item_id))
        if is_button is not None:
            if is_button and (item["menu"] != "main" or item["parent_id"] is not None):
                raise RuleError(["Only top-level main menu entries can be buttons."])
            conn.execute("UPDATE nav_items SET is_button = ? WHERE id = ?", (int(bool(is_button)), item_id))
        conn.execute("UPDATE nav_items SET updated_at = ? WHERE id = ?", (now_ms(), item_id))

    _tx(conn, run)


def count_below(conn: sqlite3.Connection, item_id: int) -> int:
    return conn.execute(
        "WITH RECURSIVE d(id) AS (SELECT id FROM nav_items WHERE parent_id = ? "
        "UNION SELECT i.id FROM nav_items i JOIN d ON i.parent_id = d.id) SELECT count(*) FROM d", (item_id,)).fetchone()[0]


def remove(conn: sqlite3.Connection, item_id: int, keep_children: bool = False) -> int:
    """Remove an entry and everything under it, or with keep_children, only the entry: what was
    under it moves up into its place. Returns how many entries were removed."""
    def run() -> int:
        item = _item(conn, item_id)
        if not keep_children:
            below = count_below(conn, item_id)
            conn.execute("DELETE FROM nav_items WHERE id = ?", (item_id,))  # sub-entries go with it (cascade)
            _renumber(conn, item["menu"], item["parent_id"], _siblings(conn, item["menu"], item["parent_id"]))
            return 1 + below
        if item["parent_id"] is None and item["menu"] in MAX_TOP:
            kids = conn.execute("SELECT count(*) FROM nav_items WHERE parent_id = ?", (item_id,)).fetchone()[0]
            if kids:
                raise RuleError(["Move or remove the links in this footer column first."])
        ids = _siblings(conn, item["menu"], item["parent_id"])
        at = ids.index(item_id)
        kids = _siblings(conn, item["menu"], item_id)
        conn.execute("UPDATE nav_items SET parent_id = ?, updated_at = ? WHERE parent_id = ?",
                     (item["parent_id"], now_ms(), item_id))
        conn.execute("DELETE FROM nav_items WHERE id = ?", (item_id,))
        _renumber(conn, item["menu"], item["parent_id"], ids[:at] + kids + ids[at + 1:])
        return 1

    return _tx(conn, run)


def stamp(conn: sqlite3.Connection) -> tuple:
    return tuple(conn.execute("SELECT count(*), max(updated_at), total(sort_order) FROM nav_items").fetchone())
