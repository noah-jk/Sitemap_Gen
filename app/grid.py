"""SQL for the Pages grid: columns, filters, built-in views. Every name that reaches SQL comes
from the dictionaries here, never from the request."""
import json
import re
from typing import Dict, List, Optional, Tuple

from app.rules import PROBLEMS_SQL

FIELDS: Dict[str, str] = {
    "id": "p.id", "label": "p.label", "url_path": "p.url_path", "current_url": "p.current_url", "status_code": "p.status_code",
    "sessions": "p.sessions", "views": "p.views", "inlinks": "p.inlinks", "word_count": "p.word_count", "in_current_nav": "p.in_current_nav",
    "collection": "p.collection", "recommendation": "p.recommendation", "decision": "p.decision",
    "status": "p.status", "notes": "p.notes", "missing_from_crawl": "p.missing_from_crawl",
    "redirect_to": "p.redirect_to", "redirect_label": "t.label", "redirect_path": "t.url_path",
    "parent_id": "p.parent_id", "parent_label": "pp.label", "is_new": "p.is_new", "page_type": "p.page_type",
    "menus": "(SELECT group_concat(DISTINCT n.menu) FROM nav_items n WHERE n.page_id = p.id)",
    "content_targets": "(SELECT json_group_array(target_id) FROM page_content_targets WHERE page_id = p.id)",
    "problem": "pr.msg", "updated_at": "p.updated_at",
    "new_url": "p.new_url", "owner": "p.owner", "writer": "p.writer", "priority": "p.priority",
    "suggested_content": "p.suggested_content",
}
# Grid columns that can be hidden (Label, the checkbox, and the problem marker always show).
HIDEABLE = {"url_path", "status_code", "sessions", "inlinks", "word_count", "in_current_nav", "collection",
            "parent_label", "menus", "page_type", "recommendation", "decision", "redirect_label", "status", "notes",
            "new_url", "owner", "writer", "priority", "suggested_content"}
TEXT_FILTERS = {"label", "url_path", "notes", "collection", "redirect_label", "parent_label", "problem", "menus",
                "new_url", "owner", "writer", "suggested_content"}
EXACT_FILTERS = {"recommendation", "decision", "status", "status_code", "in_current_nav", "missing_from_crawl", "page_type",
                 "priority"}

# Same cut-off as Screaming Frog's own "Low Content Pages" filter. Short pages are candidates to combine.
SHORT_WORDS = 200

EFFECTIVE = "COALESCE(p.decision, p.recommendation)"
KEPT = (f"COALESCE({EFFECTIVE}, '') NOT IN ('delete','combine','split') "
        f"AND (p.status_code IS NULL OR p.status_code < 400) AND p.url_path IS NOT '/'")
PRESETS: Dict[str, Tuple[str, str]] = {
    "problems": ("Problems", "pr.msg IS NOT NULL"),
    "undecided": ("Recommended but undecided", "p.recommendation IS NOT NULL AND p.decision IS NULL"),
    "overridden": ("Client overrode the recommendation",
                   "p.decision IS NOT NULL AND p.recommendation IS NOT NULL AND p.decision <> p.recommendation"),
    "removed": ("Being removed", f"{EFFECTIVE} IN ('delete','combine','split')"),
    "gone_visited": ("404/403 but still visited", "p.status_code >= 400 AND p.sessions > 0"),
    "no_traffic": ("No traffic", "p.status_code = 200 AND p.sessions = 0"),
    "current_nav": ("In the current navigation", "p.in_current_nav = 1"),
    "nav_dropped": ("In the current navigation but not in a new menu",
                    f"p.in_current_nav = 1 AND NOT EXISTS (SELECT 1 FROM nav_items n WHERE n.page_id = p.id) AND {KEPT}"),
    "not_in_menu": ("Kept but not in any menu", f"NOT EXISTS (SELECT 1 FROM nav_items n WHERE n.page_id = p.id) AND {KEPT} AND p.collection = ''"),
    "short": (f"Short pages (under {SHORT_WORDS} words)",
              f"p.word_count < {SHORT_WORDS} AND p.status_code = 200 AND {KEPT} AND p.collection = ''"),
    "missing": ("Missing from the latest crawl", "p.missing_from_crawl = 1"),
    "new": ("New pages (not on the current site)", "p.is_new = 1"),
    "moving": ("Moving to a new URL", "p.is_new = 0 AND p.new_url <> '' AND p.new_url IS NOT p.url_path"),
}

FROM = (f"FROM pages p LEFT JOIN pages t ON t.id = p.redirect_to LEFT JOIN pages pp ON pp.id = p.parent_id "
        f"LEFT JOIN (SELECT id, msg FROM ({PROBLEMS_SQL}) WHERE msg <> '') pr ON pr.id = p.id")
SELECT = "SELECT " + ", ".join(f"{expr} AS {name}" for name, expr in FIELDS.items()) + " " + FROM


def bracket_params(items, name: str) -> List[dict]:
    """Tabulator sends filter[0][field]=... style parameters; collect them in order."""
    found: Dict[int, dict] = {}
    for key, value in items:
        m = re.fullmatch(rf"{name}\[(\d{{1,3}})\]\[(field|type|value|dir)\]", key)
        if m:
            found.setdefault(int(m[1]), {})[m[2]] = value
    return [found[i] for i in sorted(found)]


def where_clause(filters: List[dict], preset: Optional[str]) -> Tuple[str, list]:
    where, args = [], []
    for f in filters:
        field, value = f.get("field"), f.get("value", "")
        if field in TEXT_FILTERS:
            where.append(f"{FIELDS[field]} LIKE ? ESCAPE '\\'")
            args.append("%" + re.sub(r"([%_\\])", r"\\\1", value) + "%")
        elif field in EXACT_FILTERS:
            if value == "(blank)":
                where.append(f"COALESCE({FIELDS[field]}, '') = ''")
            else:
                where.append(f"{FIELDS[field]} = ?")
                args.append(value)
    if preset in PRESETS:
        where.append(f"({PRESETS[preset][1]})")
    return (f"WHERE {' AND '.join(where)}" if where else ""), args


def order_clause(sorts: List[dict]) -> str:
    order = [f"{FIELDS[s['field']]} {'DESC' if s.get('dir') == 'desc' else 'ASC'}"
             for s in sorts if s.get("field") in FIELDS]
    return "ORDER BY " + ", ".join(order + ["p.id"])


def page_rows(conn, filters: List[dict], sorts: List[dict], preset: Optional[str], page: int, size: int):
    where, args = where_clause(filters, preset)
    total = conn.execute(f"SELECT count(*) {FROM} {where}", args).fetchone()[0]
    rows = conn.execute(f"{SELECT} {where} {order_clause(sorts)} LIMIT ? OFFSET ?",
                        args + [size, (page - 1) * size]).fetchall()
    return total, rows


def matching_ids(conn, filters: List[dict], preset: Optional[str]) -> List[int]:
    where, args = where_clause(filters, preset)
    return [r[0] for r in conn.execute(f"SELECT p.id {FROM} {where}", args)]


def one_row(conn, page_id: int):
    return conn.execute(f"{SELECT} WHERE p.id = ?", (page_id,)).fetchone()


def search_targets(conn, query: str, exclude: Optional[int], limit: int = 20):
    """Pages that can receive a redirect: not being removed, matching the query by label or path."""
    like = "%" + re.sub(r"([%_\\])", r"\\\1", query.strip()) + "%"
    return conn.execute(
        f"SELECT id, label, url_path, is_new, {EFFECTIVE} AS disposition FROM pages p "
        f"WHERE (label LIKE ? ESCAPE '\\' OR url_path LIKE ? ESCAPE '\\') AND id IS NOT ? "
        f"AND COALESCE({EFFECTIVE}, '') NOT IN ('delete','combine','split') "
        f"AND (status_code IS NULL OR status_code < 400) "
        f"ORDER BY (url_path = ?) DESC, sessions DESC, label LIMIT ?",
        (like, like, exclude, query.strip(), limit)).fetchall()


# ---- one page in full (the detail panel) -------------------------------------

HIDDEN_DETAIL = {"crawl_extra", "variants", "hash", "attention", "hidden", "sort_order", "provenance", "import_id",
                 "is_button"}
MENU_NAMES = {"main": "Main menu", "utility": "Utility nav", "footer": "Footer"}


def page_detail(conn, page_id: int) -> Optional[dict]:
    """Everything known about one page: the grid row, all crawl columns, where it sits in the new
    sitemap and menus, and which pages and old URLs point at it."""
    from app import tree
    full = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    if full is None:
        return None
    out = {k: full[k] for k in full.keys() if k not in HIDDEN_DETAIL}
    out["crawl_extra"] = json.loads(full["crawl_extra"] or "{}")
    variants = json.loads(full["variants"] or "[]")
    out["variants"] = [v for v in variants if v != full["current_url"]]

    row = one_row(conn, page_id)
    for k in ("problem", "redirect_label", "redirect_path", "parent_label", "menus"):
        out[k] = row[k]
    out["content_targets"] = [dict(r) for r in conn.execute(
        "SELECT p.id, p.label, p.url_path FROM page_content_targets c JOIN pages p ON p.id = c.target_id "
        "WHERE c.page_id = ? ORDER BY p.label", (page_id,))]
    out["incoming"] = [dict(r) for r in conn.execute(
        f"SELECT p.id, p.label, p.url_path, {EFFECTIVE} AS disposition FROM pages p WHERE p.id IN ("
        f" SELECT id FROM pages WHERE redirect_to = ? UNION SELECT page_id FROM page_content_targets WHERE target_id = ?)"
        f" ORDER BY p.label", (page_id, page_id))]
    out["menu_entries"] = [{"menu": MENU_NAMES.get(r["menu"], r["menu"]), "label": r["label"]} for r in conn.execute(
        "SELECT menu, label FROM nav_items WHERE page_id = ? ORDER BY menu, id", (page_id,))]
    out["rule_marks"] = {r["field"]: r["rule_name"] for r in conn.execute(
        "SELECT field, rule_name FROM rule_marks WHERE page_id = ?", (page_id,))}
    out["old_redirects"] = [dict(r) for r in conn.execute(
        "SELECT source_url, status_code FROM existing_redirects WHERE target_url IN (SELECT value FROM json_each(?)) "
        "ORDER BY source_url LIMIT 50", (json.dumps(variants),))]

    # Where it sits in the new sitemap (pages being removed, gone, or in a collection aren't in it).
    out["sitemap"], out["trail"] = None, []
    parents = {}
    for node, parent, _ in tree.walk(tree.load(conn)):
        parents[node.id] = parent
        if node.id == page_id:
            out["sitemap"] = {"type": node.type, "children": len(node.children), "descendants": node.descendants,
                              "absorbs": node.absorbs}
            up = parent
            while up is not None:
                out["trail"].insert(0, {"id": up.id, "label": up.label})
                up = parents.get(up.id)
            break
    return out
