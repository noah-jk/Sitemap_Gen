"""Undo and redo for edits made in the app.

Each edit route runs inside `record(conn, user, label)`. While it runs, temporary triggers on this
connection copy every changed row of the tracked tables into `action_rows` (the values before and
after), grouped under one `actions` row. Undo replays an action's rows backwards, restoring the
before values; redo replays them forwards. Imports aren't recorded, and an update records only the
columns a person can change, so undoing an edit never puts back old crawl numbers.

Actions form one stack per project: undo takes the newest action that isn't undone, redo the
oldest undone one, and a new edit drops anything waiting to be redone.
"""
import json
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple

from app.db import now_ms
from app.importer import UPDATABLE

# table -> (key columns, columns whose changes aren't recorded)
TRACKED: Dict[str, Tuple[Tuple[str, ...], frozenset]] = {
    "pages": (("id",), frozenset(UPDATABLE) | {"import_id", "missing_from_crawl", "updated_at"}),
    "page_content_targets": (("page_id", "target_id"), frozenset()),
    "nav_items": (("id",), frozenset({"updated_at"})),
    "settings": (("id",), frozenset({"canonical_host", "title_suffix"})),
}
STAMPED = {"pages", "nav_items"}  # tables with updated_at, bumped when undo or redo touches a row


class NothingToDo(Exception):
    pass


class Stale(Exception):
    """The rows an action touched have changed in a way that wasn't recorded."""


def _columns(conn, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")]


def _obj(prefix: str, cols: List[str]) -> str:
    return "json_object(" + ", ".join(f"'{c}', {prefix}.{c}" for c in cols) + ")"


def _install(conn) -> None:
    """Temporary triggers live only on this connection, so other connections (imports, previews)
    never record anything. Column lists come from the live schema."""
    if conn.execute("SELECT 1 FROM temp.sqlite_master WHERE name = 'history_cursor'").fetchone():
        return
    stmts = ["CREATE TEMP TABLE history_cursor(token TEXT NOT NULL, user TEXT NOT NULL, label TEXT NOT NULL, at INTEGER NOT NULL)"]
    start = ("INSERT OR IGNORE INTO actions(token, user, label, created_at) "
             "SELECT token, user, label, at FROM history_cursor;")
    for table, (key, untracked) in TRACKED.items():
        cols = _columns(conn, table)
        tracked = [c for c in cols if c not in untracked]
        for event, ref, key_ref, before, after, when in (
            ("INSERT", "NEW", "NEW", "NULL", _obj("NEW", cols), ""),
            ("DELETE", "OLD", "OLD", _obj("OLD", cols), "NULL", ""),
            ("UPDATE", "OLD", "OLD", _obj("OLD", tracked), _obj("NEW", tracked),
             f" AND {_obj('OLD', tracked)} IS NOT {_obj('NEW', tracked)}"),
        ):
            stmts.append(
                f"CREATE TEMP TRIGGER history_{table}_{event.lower()} AFTER {event} ON main.{table} "
                f"WHEN EXISTS (SELECT 1 FROM history_cursor){when} BEGIN {start} "
                f"INSERT INTO action_rows(action_id, tbl, row_key, before, after) "
                f"SELECT a.id, '{table}', {_obj(key_ref, list(key))}, {before}, {after} "
                f"FROM actions a JOIN history_cursor c ON a.token = c.token; END")
    for s in stmts:
        conn.execute(s)


class Recording:
    def __init__(self, label: str):
        self.label = label


@contextmanager
def record(conn: sqlite3.Connection, user: str, label: str) -> Iterator[Recording]:
    """Record the writes made inside the block as one action. Set `.label` on the result to change
    the description afterwards (for counts known only once the edit is done)."""
    _install(conn)
    rec = Recording(label)
    token = secrets.token_hex(12)
    conn.execute("DELETE FROM temp.history_cursor")
    conn.execute("INSERT INTO temp.history_cursor VALUES (?, ?, ?, ?)", (token, user, label, now_ms()))
    try:
        yield rec
    finally:
        conn.execute("DELETE FROM temp.history_cursor")
    if rec.label != label:
        conn.execute("UPDATE actions SET label = ? WHERE token = ?", (rec.label[:300], token))


# ---- reading -----------------------------------------------------------------

def recent(conn: sqlite3.Connection, limit: int = 30) -> dict:
    rows = conn.execute("SELECT id, user, label, created_at, undone FROM actions ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
    undo = conn.execute("SELECT label FROM actions WHERE undone = 0 ORDER BY id DESC LIMIT 1").fetchone()
    redo = conn.execute("SELECT label FROM actions WHERE undone = 1 ORDER BY id LIMIT 1").fetchone()
    return {"actions": [dict(r) for r in rows],
            "undo": undo["label"] if undo else None, "redo": redo["label"] if redo else None}


# ---- undo and redo -----------------------------------------------------------

def undo(conn: sqlite3.Connection) -> str:
    return _step(conn, "SELECT id, label FROM actions WHERE undone = 0 ORDER BY id DESC LIMIT 1", backwards=True)


def redo(conn: sqlite3.Connection) -> str:
    return _step(conn, "SELECT id, label FROM actions WHERE undone = 1 ORDER BY id LIMIT 1", backwards=False)


def _step(conn, pick: str, backwards: bool) -> str:
    conn.execute("BEGIN IMMEDIATE")
    try:
        action = conn.execute(pick).fetchone()
        if action is None:
            raise NothingToDo("Nothing to undo." if backwards else "Nothing to redo.")
        conn.execute("PRAGMA defer_foreign_keys = ON")  # rows come back one at a time
        rows = conn.execute("SELECT tbl, row_key, before, after FROM action_rows WHERE action_id = ? ORDER BY id",
                            (action["id"],)).fetchall()
        columns = {t: set(_columns(conn, t)) for t in TRACKED}
        now = now_ms()
        for r in (reversed(rows) if backwards else rows):
            have, want = (r["after"], r["before"]) if backwards else (r["before"], r["after"])
            _restore(conn, r["tbl"], json.loads(r["row_key"]), _load(have), _load(want), columns, now)
        conn.execute("UPDATE actions SET undone = ? WHERE id = ?", (1 if backwards else 0, action["id"]))
        conn.execute("COMMIT")
        return action["label"]
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _load(value: Optional[str]) -> Optional[dict]:
    return None if value is None else json.loads(value)


def _restore(conn, table: str, key: dict, have: Optional[dict], want: Optional[dict],
             columns: Dict[str, set], now: int) -> None:
    """Change one row from `have` (what the action left) to `want`, checking it still is `have`."""
    if table not in TRACKED or set(key) != set(TRACKED[table][0]):
        raise ValueError(f"unexpected history row for {table}")
    known = columns[table]
    for d in (key, have or {}, want or {}):
        if not set(d) <= known:
            raise Stale("The database has changed shape since this edit.")
    where = " AND ".join(f"{k} = ?" for k in key)
    args = list(key.values())
    current = conn.execute(f"SELECT * FROM {table} WHERE {where}", args).fetchone()
    stale = Stale("Some of these rows have changed since, so this can't be undone safely.")
    if have is None and current is not None:
        raise stale
    if have is not None and current is None:
        raise stale
    if want is None:
        conn.execute(f"DELETE FROM {table} WHERE {where}", args)
        return
    if have is None:
        row = dict(want)
        if table in STAMPED:
            row["updated_at"] = now
        names = list(row)
        conn.execute(f"INSERT INTO {table}({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                     [row[n] for n in names])
        return
    changed = [c for c in want if want[c] != have.get(c)]
    if any(current[c] != have.get(c) for c in changed):
        raise stale
    if table in STAMPED:
        changed_values = {c: want[c] for c in changed}
        changed_values["updated_at"] = now
    else:
        changed_values = {c: want[c] for c in changed}
    if changed_values:
        sets = ", ".join(f"{c} = ?" for c in changed_values)
        conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", [*changed_values.values(), *args])


# ---- descriptions ------------------------------------------------------------

FIELD_NAMES = {"label": "Name", "recommendation": "Recommendation", "decision": "Final Decision",
               "status": "Status", "notes": "Notes", "redirect_to": "Redirect to", "parent_id": "Parent",
               "page_type": "Page type", "content_targets": "Content goes to", "is_button": "Button",
               "new_url": "New URL", "owner": "Owner", "writer": "Writer", "priority": "Priority",
               "suggested_content": "Suggested content"}
MENU_NAMES = {"main": "main menu", "utility": "utility nav", "footer": "footer"}


def page_label(conn, page_id: Optional[int]) -> str:
    row = conn.execute("SELECT label FROM pages WHERE id = ?", (page_id,)).fetchone() if page_id else None
    return row["label"] if row else "a page"


def entry_label(conn, item_id: int) -> Tuple[str, str]:
    """(shown name, menu name) of a menu entry."""
    row = conn.execute("SELECT COALESCE(NULLIF(n.label, ''), p.label, 'an entry') AS name, n.menu FROM nav_items n "
                       "LEFT JOIN pages p ON p.id = n.page_id WHERE n.id = ?", (item_id,)).fetchone()
    return (row["name"], MENU_NAMES.get(row["menu"], "menu")) if row else ("an entry", "menu")


def _value(conn, field: str, value) -> str:
    if field in ("redirect_to", "parent_id"):
        return f'"{page_label(conn, value)}"' if value else "none"
    if field == "page_type":
        return value or "Auto"
    return str(value)


def describe_change(conn, page_id: int, changes: dict) -> str:
    """One page edited in the grid or renamed in the Sitemap tab."""
    name = page_label(conn, page_id)
    if "label" in changes:
        return f'Renamed "{name}" to "{" ".join(str(changes["label"]).split())}"'
    for field in ("decision", "recommendation"):
        if field in changes:
            return _set(conn, FIELD_NAMES[field], field, changes[field], f'"{name}"')
    if "content_targets" in changes or "redirect_to" in changes:
        return f'Changed where "{name}" goes'
    if "notes" in changes:
        return f'Edited notes on "{name}"'
    if "suggested_content" in changes:
        return f'Edited suggested content on "{name}"'
    field = next(iter(changes), "")
    if field in FIELD_NAMES:
        return _set(conn, FIELD_NAMES[field], field, changes[field], f'"{name}"')
    return f'Edited "{name}"'


def describe_bulk(conn, changes: dict) -> str:
    field = next(iter(changes), "")
    return _set(conn, FIELD_NAMES.get(field, field), field, changes.get(field), "{count}")


def _set(conn, title: str, field: str, value, target: str) -> str:
    if value is None or value == "" and field != "page_type":
        return f"Cleared {title} on {target}"
    return f"Set {title} to {_value(conn, field, value)} on {target}"
