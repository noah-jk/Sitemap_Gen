"""The to-be navigation: kept pages nested by parent_id, edits to it, and prototypes built from it."""
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import sitemap2proto as sp
from app.db import now_ms
from app.rules import REMOVED, RuleError, check_parent

MAX_LABEL = 200
FORM_LABEL = re.compile(r"^(apply now|request|register|rsvp|sign up|contact us)\b|\b(form|inquiry)$", re.I)


@dataclass
class TreeNode:
    id: int
    label: str
    url_path: Optional[str]
    is_new: bool
    is_button: bool
    disposition: Optional[str]
    page_type: str
    updated_at: int
    children: List["TreeNode"] = field(default_factory=list)
    collection_count: int = 0   # collection items under this page (not shown in the tree)
    descendants: int = 0        # every page below, collections included
    absorbs: int = 0            # pages combining or splitting into this one

    @property
    def type(self) -> str:
        """The page type: set explicitly, or worked out from where the page sits in the sitemap."""
        if self.page_type in sp.PAGE_TYPES:
            return self.page_type
        if self.collection_count:
            return "listing"
        if self.children:
            return "landing"
        if FORM_LABEL.search(self.label):
            return "form"
        return "article"

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "url_path": self.url_path, "is_new": self.is_new,
                "page_type": self.page_type, "type": self.type,
                "is_button": self.is_button, "disposition": self.disposition, "updated_at": self.updated_at,
                "collection_count": self.collection_count, "descendants": self.descendants,
                "absorbs": self.absorbs, "children": [c.to_dict() for c in self.children]}


def _shown(r) -> bool:
    """Pages that exist after launch and belong in the navigation tree."""
    if r["url_path"] == "/" or r["collection"] or r["e"] in REMOVED:
        return False
    return not (r["status_code"] or 0) >= 400 or r["e"] == "keep"


def load(conn: sqlite3.Connection) -> List[TreeNode]:
    """Top-level pages with their subtrees. Children of hidden pages (removed, gone, collection
    items) attach to the nearest shown ancestor, so nothing disappears from the tree."""
    rows = conn.execute(
        "SELECT id, parent_id, label, url_path, sort_order, is_new, is_button, page_type, collection, updated_at, "
        "status_code, COALESCE(decision, recommendation) AS e FROM pages").fetchall()
    by_id = {r["id"]: r for r in rows}

    def shown_parent(parent: Optional[int]) -> Optional[int]:
        seen = set()
        while parent is not None and parent not in seen and not _shown(by_id[parent]):
            seen.add(parent)
            parent = by_id[parent]["parent_id"]
        return parent if parent is not None and parent not in seen else None

    nodes = {r["id"]: TreeNode(r["id"], r["label"], r["url_path"], bool(r["is_new"]), bool(r["is_button"]),
                               r["e"], r["page_type"], r["updated_at"]) for r in rows if _shown(r)}
    roots: List[TreeNode] = []
    order = sorted((r for r in rows if r["id"] in nodes), key=lambda r: (r["sort_order"], r["label"].lower(), r["id"]))
    for r in order:
        parent = shown_parent(r["parent_id"])
        (nodes[parent].children if parent in nodes else roots).append(nodes[r["id"]])
    for r in rows:
        if r["collection"] and r["e"] not in REMOVED:
            parent = shown_parent(r["parent_id"])
            if parent in nodes:
                nodes[parent].collection_count += 1

    for target, n in conn.execute(
            "SELECT target, count(DISTINCT source) FROM ("
            " SELECT redirect_to AS target, id AS source FROM pages"
            "  WHERE COALESCE(decision, recommendation) IN ('combine','split') AND redirect_to IS NOT NULL"
            " UNION ALL SELECT c.target_id, c.page_id FROM page_content_targets c JOIN pages p ON p.id = c.page_id"
            "  WHERE COALESCE(p.decision, p.recommendation) = 'split') GROUP BY target"):
        if target in nodes:
            nodes[target].absorbs = n

    def count(node: TreeNode) -> int:
        node.descendants = node.collection_count + sum(1 + count(c) for c in node.children)
        return node.descendants

    for root in roots:
        count(root)
    return roots


def walk(roots: List[TreeNode]):
    stack = [(n, None, 1) for n in reversed(roots)]
    while stack:
        node, parent, depth = stack.pop()
        yield node, parent, depth
        stack.extend((c, node, depth + 1) for c in reversed(node.children))


# ---- edits -------------------------------------------------------------------

def _clean_label(label: str) -> str:
    label = " ".join(str(label or "").split())
    if not label:
        raise RuleError(["Give the page a name."])
    return label[:MAX_LABEL]


def move(conn: sqlite3.Connection, page_id: int, parent: Optional[int], index: int) -> None:
    """Put a page (and everything below it) under `parent` at position `index` among the shown siblings."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM pages WHERE id = ?", (page_id,)).fetchone() is None:
            raise KeyError(page_id)
        check_parent(conn, [page_id], parent)
        roots = load(conn)
        siblings = roots if parent is None else next((n.children for n, _, _ in walk(roots) if n.id == parent), [])
        ids = [n.id for n in siblings if n.id != page_id]
        ids.insert(max(0, min(index, len(ids))), page_id)
        conn.executemany("UPDATE pages SET sort_order = ? WHERE id = ?", [((i + 1) * 10, pid) for i, pid in enumerate(ids)])
        conn.execute("UPDATE pages SET parent_id = ?, updated_at = ? WHERE id = ?", (parent, now_ms(), page_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def create_page(conn: sqlite3.Connection, label: str, parent: Optional[int]) -> int:
    """A page that doesn't exist on the current site yet."""
    label = _clean_label(label)
    conn.execute("BEGIN IMMEDIATE")
    try:
        check_parent(conn, [], parent)
        last = conn.execute("SELECT COALESCE(max(sort_order), 0) FROM pages WHERE parent_id IS ?", (parent,)).fetchone()[0]
        page_id = conn.execute(
            "INSERT INTO pages(is_new, label, parent_id, sort_order, provenance, updated_at) "
            "VALUES (1, ?, ?, ?, 'designed', ?)", (label, parent, last + 10, now_ms())).lastrowid
        conn.execute("COMMIT")
        return page_id
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def delete_new_page(conn: sqlite3.Connection, page_id: int) -> None:
    """Only pages added in the app can be deleted; their sub-pages move up a level."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT is_new, parent_id, label FROM pages WHERE id = ?", (page_id,)).fetchone()
        if row is None:
            raise KeyError(page_id)
        if not row["is_new"]:
            raise RuleError(["Pages from the crawl can't be deleted here. Set their Final Decision instead."])
        pointing = conn.execute(
            "SELECT count(*) FROM (SELECT id FROM pages WHERE redirect_to = ? "
            "UNION SELECT page_id FROM page_content_targets WHERE target_id = ?)", (page_id, page_id)).fetchone()[0]
        if pointing:
            raise RuleError([f'{pointing} page{"s" if pointing != 1 else ""} redirect to "{row["label"]}". '
                             f"Point them somewhere else first."])
        conn.execute("UPDATE pages SET parent_id = ? WHERE parent_id = ?", (row["parent_id"], page_id))
        conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---- prototypes --------------------------------------------------------------

def settings_row(conn) -> sqlite3.Row:
    return conn.execute("SELECT * FROM settings").fetchone()


def build_site(conn: sqlite3.Connection) -> Tuple[sp.Site, sp.Settings]:
    from app import nav
    s = settings_row(conn)
    max_depth = s["max_depth"]
    root = sp.Node(title="Home")
    pages: Dict[int, sp.Node] = {}

    def convert(node: TreeNode, parent: sp.Node, depth: int) -> None:
        out = sp.Node(title=node.label, parent=parent, id=node.id, page_type=node.type,
                      more_below=node.collection_count)
        parent.children.append(out)
        pages[node.id] = out
        if max_depth and depth >= max_depth:
            out.more_below = node.descendants
            return
        for child in node.children:
            convert(child, out, depth + 1)

    for top in load(conn):
        convert(top, root, 1)
    sp.assign_paths(root)

    def entries(items: List[dict], parent: sp.Node) -> List[sp.Node]:
        """Menu entries for the generator. Entries whose page isn't built are skipped and
        anything under them moves up, so the menu never links to a missing page."""
        out: List[sp.Node] = []
        for item in items:
            target = pages.get(item["page_id"]) if item["page_id"] else None
            if item["page_id"] and target is None:
                out.extend(entries(item["children"], parent))
                continue
            entry = sp.Node(title=item["label"] or target.title, parent=parent,
                            out_path=target.out_path if target else "", is_button=item["is_button"])
            entry.children = entries(item["children"], entry)
            out.append(entry)
        return out

    menus = nav.load(conn)
    nav_root = sp.Node(title="")
    nav_root.children = entries(menus["main"], nav_root)
    if menus["utility"] or menus["footer"]:
        holder = sp.Node(title="")
        utility = [sp.LinkGroup(e.title, e if e.out_path else None, [c for c in e.children if c.out_path])
                   for e in entries(menus["utility"], holder)]
        footer = [sp.LinkGroup(e.title, None, [c for c in e.children if c.out_path])
                  for e in entries(menus["footer"], holder)]
        standalone: List[sp.Node] = []
    else:  # projects set up before menus existed
        footer, utility, standalone = sp.resolve_links(
            sp.parse_footer(s["footer_md"]), sp.parse_utility_nav(s["utility_md"]), root)
    settings = sp.Settings(nav_style=s["nav_style"], dropdown_depth=s["dropdown_depth"],
                           mega_menu=bool(s["mega_menu"]), sticky_header=bool(s["sticky_header"]), noindex=True)
    return sp.Site(root=root, footer=footer, utility=utility, standalone=standalone, nav_root=nav_root), settings


def render_files(conn: sqlite3.Connection) -> Dict[str, str]:
    site, settings = build_site(conn)
    return sp.render_site(site, settings)


# What the prototype is built from. Notes, status, redirects, and crawl numbers aren't in it, so
# auditing in the grid doesn't make the prototype rebuild.
STAMP_SQL = (
    "SELECT id, parent_id, label, sort_order, is_new, page_type, collection, status_code, "
    "COALESCE(decision, recommendation) FROM pages ORDER BY id",
    "SELECT id, menu, parent_id, page_id, label, sort_order, is_button FROM nav_items ORDER BY id",
    "SELECT * FROM settings",
)


def stamp(conn: sqlite3.Connection) -> str:
    """Changes whenever anything the prototype shows changes (and only then)."""
    h = hashlib.blake2b(digest_size=16)
    for sql in STAMP_SQL:
        for row in conn.execute(sql):
            h.update(repr(tuple(row)).encode())
        h.update(b"\x00")
    return h.hexdigest()
