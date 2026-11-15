"""Disposition rules: what makes a page's decision invalid, and saving changes that respect them.

A page's effective disposition is its Final Decision when set, otherwise the Recommendation.
"""
import json
import sqlite3
from typing import Dict, Iterable, List, Optional, Tuple

from app.db import DISPOSITIONS, now_ms

REMOVED = ("delete", "combine", "split")  # pages that won't exist after launch
_REM = "('delete','combine','split')"
STATUSES = ("todo", "done")
MAX_NOTES = 5000
SCALAR_FIELDS = ("recommendation", "decision", "status", "notes", "redirect_to")
EDITABLE = set(SCALAR_FIELDS) | {"content_targets"}
BULK_EDITABLE = {"recommendation", "decision", "status", "redirect_to"}

_CHECKS = [
    ("p.e IN ('combine','split') AND p.redirect_to IS NULL",
     "'Needs a page to redirect to.'"),
    (f"p.e IN {_REM} AND t.e IN {_REM}",
     "'Redirects to \"' || t.label || '\", which is marked ' || t.e || '.'"),
    ("p.e = 'split' AND (SELECT count(*) FROM page_content_targets x WHERE x.page_id = p.id) < 2",
     "'A split needs two or more pages for its content.'"),
    ("p.e = 'split' AND p.redirect_to IS NOT NULL AND NOT EXISTS "
     "(SELECT 1 FROM page_content_targets x WHERE x.page_id = p.id AND x.target_id = p.redirect_to)",
     "'A split must redirect to one of the pages its content goes to.'"),
    (f"p.e = 'split' AND EXISTS (SELECT 1 FROM page_content_targets x JOIN eff y ON y.id = x.target_id "
     f"WHERE x.page_id = p.id AND y.e IN {_REM})",
     "'Some of its content goes to a page that is being removed.'"),
]

# One row per page with its problems joined into `msg` ('' when there are none).
PROBLEMS_SQL = (
    "WITH eff AS (SELECT id, label, redirect_to, COALESCE(decision, recommendation) AS e FROM pages) "
    "SELECT p.id AS id, trim("
    + " || ".join(f"CASE WHEN {cond} THEN {msg} || ' ' ELSE '' END" for cond, msg in _CHECKS)
    + ") AS msg FROM eff p LEFT JOIN eff t ON t.id = p.redirect_to"
)


class RuleError(ValueError):
    def __init__(self, messages: List[str]):
        self.messages = messages
        super().__init__(" ".join(messages))


class Conflict(Exception):
    def __init__(self, row: sqlite3.Row):
        self.row = row


def problems(conn: sqlite3.Connection, ids: Optional[Iterable[int]] = None) -> Dict[int, str]:
    sql = f"SELECT id, msg FROM ({PROBLEMS_SQL}) WHERE msg <> ''"
    args: list = []
    if ids is not None:
        sql += " AND id IN (SELECT value FROM json_each(?))"
        args.append(json.dumps(list(ids)))
    return {r["id"]: r["msg"] for r in conn.execute(sql, args)}


def _clean(changes: dict, allowed: set) -> dict:
    unknown = set(changes) - allowed
    if unknown:
        raise RuleError([f"{', '.join(sorted(unknown))} can't be edited here."])
    out = {}
    for field, value in changes.items():
        if field in ("recommendation", "decision"):
            value = value or None
            if value is not None and value not in DISPOSITIONS:
                raise RuleError([f"{field} must be one of: {', '.join(DISPOSITIONS)}."])
        elif field == "status":
            value = value or "todo"
            if value not in STATUSES:
                raise RuleError(["status must be todo or done."])
        elif field == "notes":
            value = str(value or "")[:MAX_NOTES]
        elif field == "redirect_to":
            if value in (None, ""):
                value = None
            elif isinstance(value, bool) or not isinstance(value, int):
                raise RuleError(["redirect_to must be a page id."])
        elif field == "content_targets":
            if not isinstance(value, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
                raise RuleError(["content_targets must be a list of page ids."])
            value = list(dict.fromkeys(value))
        out[field] = value
    return out


def _labels(conn, ids: Iterable[int]) -> Dict[int, str]:
    return {r["id"]: r["label"] for r in conn.execute(
        "SELECT id, label FROM pages WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(list(ids)),))}


def _check_targets_exist(conn, page_ids: List[int], targets: List[int]) -> None:
    if not targets:
        return
    found = set(_labels(conn, targets))
    if len(found) != len(set(targets)):
        raise RuleError(["That target page doesn't exist any more. Reload and pick again."])
    if set(page_ids) & set(targets):
        raise RuleError(["A page can't redirect to itself."])


def dependents(conn, page_id: int) -> int:
    """Pages that point at this one (as redirect or content target)."""
    return conn.execute(
        "SELECT count(DISTINCT id) FROM (SELECT id FROM pages WHERE redirect_to = ? "
        "UNION SELECT page_id FROM page_content_targets WHERE target_id = ?)", (page_id, page_id)).fetchone()[0]


def apply_changes(conn: sqlite3.Connection, page_id: int, changes: dict, updated_at: int) -> Tuple[int, str]:
    """Save changes to one page if it was not edited since `updated_at` and the result breaks no rule.
    Returns (page_id, warning) where warning mentions other pages that now need attention."""
    changes = _clean(changes, EDITABLE)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT id, updated_at FROM pages WHERE id = ?", (page_id,)).fetchone()
        if row is None:
            raise KeyError(page_id)
        if row["updated_at"] != updated_at:
            raise Conflict(row)
        targets = [changes["redirect_to"]] if changes.get("redirect_to") is not None else []
        targets += changes.get("content_targets", [])
        _check_targets_exist(conn, [page_id], targets)
        scalars = {k: v for k, v in changes.items() if k in SCALAR_FIELDS}
        sets = "".join(f"{k} = ?, " for k in scalars)
        conn.execute(f"UPDATE pages SET {sets}updated_at = ? WHERE id = ?", [*scalars.values(), now_ms(), page_id])
        if "content_targets" in changes:
            conn.execute("DELETE FROM page_content_targets WHERE page_id = ?", (page_id,))
            conn.executemany("INSERT INTO page_content_targets(page_id, target_id) VALUES (?, ?)",
                             [(page_id, t) for t in changes["content_targets"]])
        found = problems(conn, [page_id])
        if found:
            raise RuleError([found[page_id]])
        warning = ""
        effective = conn.execute("SELECT COALESCE(decision, recommendation) FROM pages WHERE id = ?",
                                 (page_id,)).fetchone()[0]
        if effective in REMOVED:
            n = dependents(conn, page_id)
            if n:
                warning = (f"{n} other page{'s' if n > 1 else ''} point{'' if n > 1 else 's'} here and "
                           f"now need{'' if n > 1 else 's'} a new target. See the Problems view.")
        conn.execute("COMMIT")
        return page_id, warning
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def bulk_apply(conn: sqlite3.Connection, ids: List[int], changes: dict) -> Tuple[int, str]:
    """Apply the same change to many pages, all or nothing. Returns (count, warning)."""
    changes = _clean(changes, BULK_EDITABLE)
    if not changes:
        raise RuleError(["Choose what to change."])
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise RuleError(["No pages selected."])
    conn.execute("BEGIN IMMEDIATE")
    try:
        if changes.get("redirect_to") is not None:
            target = changes["redirect_to"]
            if target in ids:
                label = _labels(conn, [target]).get(target, "The target")
                raise RuleError([f'"{label}" is in the selection, and a page can\'t redirect to itself. '
                                 f'Unselect it and try again.'])
            _check_targets_exist(conn, [], [target])
        sets = "".join(f"{k} = ?, " for k in changes)
        cur = conn.execute(f"UPDATE pages SET {sets}updated_at = ? WHERE id IN (SELECT value FROM json_each(?))",
                           [*changes.values(), now_ms(), json.dumps(ids)])
        found = problems(conn, ids)
        if found:
            labels = _labels(conn, found)
            shown = [f'"{labels[i]}": {msg}' for i, msg in list(found.items())[:5]]
            more = f" …and {len(found) - 5} more." if len(found) > 5 else ""
            raise RuleError([f"Nothing was changed. {len(found)} page{'s' if len(found) > 1 else ''} "
                             f"would break a rule: " + " ".join(shown) + more])
        removed_targets = conn.execute(
            f"SELECT count(*) FROM pages p WHERE p.redirect_to IN (SELECT value FROM json_each(?)) "
            f"AND p.id NOT IN (SELECT value FROM json_each(?)) AND p.id IN "
            f"(SELECT id FROM ({PROBLEMS_SQL}) WHERE msg <> '')", (json.dumps(ids), json.dumps(ids))).fetchone()[0]
        warning = (f"{removed_targets} other page{'s' if removed_targets != 1 else ''} now need a new target. "
                   f"See the Problems view.") if removed_targets else ""
        conn.execute("COMMIT")
        return cur.rowcount, warning
    except BaseException:
        conn.execute("ROLLBACK")
        raise
