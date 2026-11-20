"""If-then rules: "include pages where (A and B) or (C) -> set these fields".

A rule definition is plain data, checked by clean_definition() before use and evaluated here in
Python. Nothing in a rule is ever turned into SQL. Rules run in list order and each one sees what
the earlier ones changed, so "Recommendation = combine" followed by "Final Decision =
Recommendation" works in one run. A run is one transaction; pages whose result would break the
audit rules are left as they were and reported. Each value a rule sets is marked with the rule's
name (rule_marks) until a person changes it.

Definition:
  {"mode": "include" | "exclude",
   "groups": [[{"field": "path", "op": "matches", "value": "/academics/*/facilities"}, ...], ...],
   "actions": [{"field": "recommendation", "kind": "value", "value": "combine"},
               {"field": "redirect_to", "kind": "sitemap_parent"}, ...]}
Conditions in a group must all match (AND); a page matches the rule when any group does (OR).
"""
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from app import dates, rules
from app.db import DISPOSITIONS, now_ms

# ---- what rules can look at and change -----------------------------------------

TEXT, PATH, NUMBER, DATE, BOOL, CHOICE = "text", "path", "number", "date", "bool", "choice"
TYPE_NAMES = {"": "Auto", "landing": "Landing", "listing": "Listing", "list": "List", "article": "Article", "form": "Form"}
CHOICES = {
    "recommendation": list(DISPOSITIONS), "decision": list(DISPOSITIONS), "status": ["todo", "done"],
    "priority": ["high", "medium", "low"], "page_type": [k for k in TYPE_NAMES if k],
}

# field -> (name shown, type)
CONDITION_FIELDS: Dict[str, Tuple[str, str]] = {
    "path": ("Path", PATH), "label": ("Label", TEXT), "title": ("Title", TEXT), "h1": ("H1", TEXT),
    "meta_description": ("Meta description", TEXT), "status_code": ("HTTP status", NUMBER),
    "sessions": ("Sessions", NUMBER), "views": ("Views", NUMBER), "inlinks": ("Inlinks", NUMBER),
    "word_count": ("Words", NUMBER), "crawl_depth": ("Crawl depth", NUMBER),
    "near_duplicates": ("Near duplicates", NUMBER), "date": ("Date", DATE),
    "collection": ("Collection", TEXT), "in_current_nav": ("In current navigation", BOOL),
    "missing_from_crawl": ("Missing from latest crawl", BOOL), "is_new": ("New page", BOOL),
    "page_type": ("Page type", CHOICE), "recommendation": ("Recommendation", CHOICE),
    "decision": ("Final Decision", CHOICE), "status": ("Status", CHOICE), "priority": ("Priority", CHOICE),
    "parent": ("Parent (in the sitemap)", TEXT), "section": ("Top-level section", TEXT),
    "menus": ("Menus", TEXT), "owner": ("Owner", TEXT), "writer": ("Writer", TEXT), "notes": ("Notes", TEXT),
}
CRAWL_PREFIX = "crawl:"  # any other crawl column, such as a custom extraction, as text

OPS: Dict[str, List[Tuple[str, str]]] = {
    TEXT: [("is", "is"), ("is_not", "is not"), ("contains", "contains"), ("not_contains", "doesn't contain"),
           ("starts_with", "starts with"), ("ends_with", "ends with"), ("is_empty", "is empty"),
           ("not_empty", "isn't empty")],
    NUMBER: [("eq", "="), ("ne", "≠"), ("lt", "<"), ("lte", "≤"), ("gt", ">"), ("gte", "≥"),
             ("is_empty", "is empty"), ("not_empty", "isn't empty")],
    DATE: [("before", "before"), ("after", "after"), ("is_empty", "has no date"), ("not_empty", "has a date")],
    BOOL: [("is_true", "yes"), ("is_false", "no")],
    CHOICE: [("is", "is"), ("is_not", "is not"), ("is_empty", "is empty"), ("not_empty", "isn't empty")],
}
OPS[PATH] = [("matches", "matches pattern"), ("not_matches", "doesn't match pattern")] + OPS[TEXT]
NO_VALUE = {"is_empty", "not_empty", "is_true", "is_false"}

# field -> (name shown, type); "page" fields take a page as their value.
PAGE = "page"
ACTION_FIELDS: Dict[str, Tuple[str, str]] = {
    "recommendation": ("Recommendation", CHOICE), "decision": ("Final Decision", CHOICE),
    "redirect_to": ("Redirect to", PAGE), "parent_id": ("Parent (move under)", PAGE),
    "status": ("Status", CHOICE), "page_type": ("Page type", CHOICE), "priority": ("Priority", CHOICE),
    "owner": ("Owner", TEXT), "writer": ("Writer", TEXT), "notes": ("Notes", TEXT),
    "suggested_content": ("Suggested content", TEXT), "new_url": ("New URL", TEXT),
}
# How a value is given. Copying works between fields of the same kind.
KINDS = {
    "value": "a value", "field": "the value of another field", "blank": "blank",
    "append": "itself plus a line of text",
    "sitemap_parent": "its parent in the sitemap", "url_parent": "its parent in the URL",
    "path": "the page at a path",
}
PAGE_KINDS = {"sitemap_parent", "url_parent", "path", "blank"}
COPY_FROM = {  # target field -> fields it can copy from
    "recommendation": ["decision"], "decision": ["recommendation"],
    **{f: ["label", "title", "h1", "owner", "writer", "collection", "parent", "section"]
       for f in ("owner", "writer", "notes", "suggested_content")},
}
APPENDABLE = {"notes", "suggested_content"}
MAX_GROUPS, MAX_CONDITIONS, MAX_ACTIONS, MAX_TEXT = 10, 20, 12, 500


class RuleDefinitionError(ValueError):
    pass


def schema() -> dict:
    """What the rule builder offers, so the page and the server agree."""
    return {
        "conditions": {k: {"name": n, "type": t, "choices": CHOICES.get(k)} for k, (n, t) in CONDITION_FIELDS.items()},
        "ops": {t: [{"op": o, "name": n} for o, n in ops] for t, ops in OPS.items()},
        "no_value": sorted(NO_VALUE),
        "actions": {k: {"name": n, "type": t, "choices": CHOICES.get(k), "copy_from": COPY_FROM.get(k, []),
                        "append": k in APPENDABLE} for k, (n, t) in ACTION_FIELDS.items()},
        "kinds": KINDS, "type_names": TYPE_NAMES,
    }


# ---- checking a definition -----------------------------------------------------

def _field_type(name: str) -> str:
    if name in CONDITION_FIELDS:
        return CONDITION_FIELDS[name][1]
    if name.startswith(CRAWL_PREFIX) and 0 < len(name) - len(CRAWL_PREFIX) <= 200:
        return TEXT
    raise RuleDefinitionError(f"Unknown field: {name}")


def _clean_condition(c: Any) -> dict:
    if not isinstance(c, dict):
        raise RuleDefinitionError("Each condition needs a field, a test, and a value.")
    name, op = str(c.get("field", "")), str(c.get("op", ""))
    kind = _field_type(name)
    if op not in dict(OPS[kind]):
        raise RuleDefinitionError(f"“{op}” can't be used with {CONDITION_FIELDS.get(name, (name,))[0]}.")
    out = {"field": name, "op": op}
    if op in NO_VALUE:
        return out
    value = c.get("value", "")
    if kind == NUMBER:
        try:
            out["value"] = float(value)
        except (TypeError, ValueError):
            raise RuleDefinitionError(f"{CONDITION_FIELDS[name][0]} needs a number.")
    elif kind == DATE:
        try:
            out["value"] = date.fromisoformat(str(value)).isoformat()
        except ValueError:
            raise RuleDefinitionError("Dates are written YYYY-MM-DD.")
    elif kind == CHOICE:
        value = str(value or "")
        if value not in CHOICES[name]:
            raise RuleDefinitionError(f"{CONDITION_FIELDS[name][0]} can't be “{value}”.")
        out["value"] = value
    else:
        value = str(value if value is not None else "").strip()[:MAX_TEXT]
        if not value:
            raise RuleDefinitionError(f"{CONDITION_FIELDS.get(name, (name[len(CRAWL_PREFIX):],))[0]} needs something to compare with.")
        out["value"] = value
        if op in ("matches", "not_matches"):
            _pattern(value)  # make sure it compiles
    return out


def _clean_action(a: Any) -> dict:
    if not isinstance(a, dict):
        raise RuleDefinitionError("Each action needs a field and what to set it to.")
    name, kind = str(a.get("field", "")), str(a.get("kind", "value"))
    if name not in ACTION_FIELDS:
        raise RuleDefinitionError(f"Rules can't set {name}.")
    title, ftype = ACTION_FIELDS[name]
    if ftype == PAGE:
        if kind not in PAGE_KINDS:
            raise RuleDefinitionError(f"{title} is set to a page: its sitemap parent, its URL parent, or a path.")
    elif kind not in ("value", "field", "blank", "append"):
        raise RuleDefinitionError(f"{title} can't be set to {KINDS.get(kind, kind)}.")
    out = {"field": name, "kind": kind}
    if kind == "value":
        try:
            cleaned = rules._clean({name: a.get("value")}, set(ACTION_FIELDS))
        except rules.RuleError as e:
            raise RuleDefinitionError(str(e))
        if cleaned[name] in (None, "") and name not in ("notes", "suggested_content"):
            raise RuleDefinitionError(f"Pick a value for {title} (or choose “blank”).")
        out["value"] = cleaned[name]
    elif kind == "field":
        source = str(a.get("value", ""))
        if source not in COPY_FROM.get(name, []):
            raise RuleDefinitionError(f"{title} can't be copied from {source or 'that'}.")
        out["value"] = source
    elif kind == "append":
        if name not in APPENDABLE:
            raise RuleDefinitionError(f"Only notes and suggested content can be added to.")
        text = str(a.get("value", "")).strip()[:MAX_TEXT]
        if not text:
            raise RuleDefinitionError("Give the text to add.")
        out["value"] = text
    elif kind == "path":
        try:
            out["value"] = rules.clean_url(a.get("value"))
        except rules.RuleError as e:
            raise RuleDefinitionError(str(e))
        if not out["value"]:
            raise RuleDefinitionError("Give the path of the page.")
    return out


def clean_definition(d: Any) -> dict:
    if not isinstance(d, dict):
        raise RuleDefinitionError("A rule needs conditions and actions.")
    mode = d.get("mode", "include")
    if mode not in ("include", "exclude"):
        raise RuleDefinitionError("A rule includes or excludes the pages it matches.")
    groups = d.get("groups") or []
    if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
        raise RuleDefinitionError(f"Up to {MAX_GROUPS} OR groups.")
    clean_groups = []
    for g in groups:
        if not isinstance(g, list) or len(g) > MAX_CONDITIONS:
            raise RuleDefinitionError(f"Up to {MAX_CONDITIONS} conditions in a group.")
        if g:
            clean_groups.append([_clean_condition(c) for c in g])
    actions = d.get("actions") or []
    if not isinstance(actions, list) or not actions:
        raise RuleDefinitionError("Add at least one thing for the rule to set.")
    if len(actions) > MAX_ACTIONS:
        raise RuleDefinitionError(f"Up to {MAX_ACTIONS} actions in a rule.")
    clean_actions = [_clean_action(a) for a in actions]
    if len({a["field"] for a in clean_actions}) != len(clean_actions):
        raise RuleDefinitionError("Each field can be set once per rule.")
    return {"mode": mode, "groups": clean_groups, "actions": clean_actions}


# ---- the pages as rules see them -------------------------------------------------

def _pattern(text: str) -> "re.Pattern":
    """Path patterns: * is one folder (or part of one), ** is any depth. Case doesn't matter."""
    out, i = "", 0
    while i < len(text):
        if text.startswith("**", i):
            out, i = out + ".*", i + 2
        elif text[i] == "*":
            out, i = out + "[^/]*", i + 1
        else:
            out, i = out + re.escape(text[i]), i + 1
    return re.compile(f"^{out.rstrip('/')}/?$", re.IGNORECASE)


def _norm(path: str) -> str:
    return (path or "").split("?")[0].rstrip("/") or "/"


@dataclass
class Pages:
    """Every page's fields, plus the relationships rules use. Kept up to date as rules run."""
    rows: Dict[int, dict]
    sitemap_parent: Dict[int, Optional[int]]
    section: Dict[int, str]
    by_path: Dict[str, int]
    dates: Dict[int, Optional["dates.Period"]]
    extra: Dict[int, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "Pages":
        from app import tree, urls
        rows = {r["id"]: dict(r) for r in conn.execute(
            "SELECT p.*, (SELECT group_concat(DISTINCT n.menu) FROM nav_items n WHERE n.page_id = p.id) AS menus "
            "FROM pages p")}
        planned = urls.planned(conn)
        sitemap_parent: Dict[int, Optional[int]] = {i: r["parent_id"] for i, r in rows.items()}
        section: Dict[int, str] = {}
        for node, up, _ in tree.walk(tree.load(conn)):
            sitemap_parent[node.id] = up.id if up else None
        for page_id in rows:
            top, seen = page_id, set()
            while sitemap_parent.get(top) is not None and top not in seen:
                seen.add(top)
                top = sitemap_parent[top]
            section[page_id] = rows[top]["label"] if top != page_id or sitemap_parent.get(page_id) is None else ""
        for page_id, r in rows.items():
            r["path"] = r["url_path"] or planned.get(page_id, "")
        by_path = {}
        for page_id, r in sorted(rows.items()):
            by_path.setdefault(_norm(r["path"]).lower(), page_id)
        return cls(rows, sitemap_parent, section, by_path, dates.all_dates(conn))

    def get(self, page_id: int, name: str):
        r = self.rows[page_id]
        if name == "parent":
            up = self.sitemap_parent.get(page_id)
            return self.rows[up]["label"] if up in self.rows else ""
        if name == "section":
            return self.section.get(page_id, "")
        if name == "date":
            return self.dates.get(page_id)
        if name.startswith(CRAWL_PREFIX):
            if page_id not in self.extra:
                self.extra[page_id] = json.loads(r["crawl_extra"] or "{}")
            return self.extra[page_id].get(name[len(CRAWL_PREFIX):], "")
        return r.get(name)

    def url_parent(self, page_id: int) -> Optional[int]:
        """The nearest page up the URL: /academics/art/facilities -> /academics/art."""
        path = _norm(self.rows[page_id]["path"])
        while path not in ("", "/"):
            path = path.rsplit("/", 1)[0] or "/"
            found = self.by_path.get(path.lower())
            if found is not None and found != page_id:
                return found
        return None


def _matches(pages: Pages, page_id: int, c: dict) -> bool:
    op, kind = c["op"], _field_type(c["field"])
    value = pages.get(page_id, c["field"])
    if kind == DATE:
        if op == "is_empty":
            return value is None
        if op == "not_empty":
            return value is not None
        if value is None:
            return False  # no date: date tests don't match
        day = date.fromisoformat(c["value"])
        return value.before(day) if op == "before" else value.after(day)
    if kind == BOOL:
        return bool(value) == (op == "is_true")
    if op == "is_empty":
        return value in (None, "")
    if op == "not_empty":
        return value not in (None, "")
    if kind == NUMBER:
        if value in (None, ""):
            return False
        target = c["value"]
        return {"eq": value == target, "ne": value != target, "lt": value < target, "lte": value <= target,
                "gt": value > target, "gte": value >= target}[op]
    text, target = str(value or "").lower(), str(c["value"]).lower()
    if op in ("matches", "not_matches"):
        return bool(_pattern(c["value"]).match(_norm(str(value or "")))) == (op == "matches")
    return {"is": text == target, "is_not": text != target, "contains": target in text,
            "not_contains": target not in text, "starts_with": text.startswith(target),
            "ends_with": text.endswith(target)}[op]


def matching(pages: Pages, definition: dict) -> List[int]:
    groups = definition["groups"]
    hits = []
    for page_id in sorted(pages.rows):
        hit = not groups or any(all(_matches(pages, page_id, c) for c in g) for g in groups)
        if hit == (definition["mode"] == "include"):
            hits.append(page_id)
    return hits


# ---- planning and applying ---------------------------------------------------------

@dataclass
class Outcome:
    name: str
    matched: int = 0
    changed: Dict[int, Dict[str, Any]] = field(default_factory=dict)   # page -> {field: new value}
    skipped: List[Tuple[int, str]] = field(default_factory=list)       # (page, why)
    undated: int = 0  # pages left out because a date test needs a date they don't have


def _value_for(pages: Pages, page_id: int, action: dict):
    """The new value for one page, or raise ValueError with why it can't be set."""
    kind, name = action["kind"], action["field"]
    if kind == "value":
        return action["value"]
    if kind == "blank":
        return None if ACTION_FIELDS[name][1] in (PAGE, CHOICE) and name not in ("status", "page_type", "priority") else ""
    if kind == "field":
        return pages.get(page_id, action["value"])
    if kind == "append":
        current = pages.rows[page_id].get(name) or ""
        return action["value"] if action["value"] in current else (f"{current}\n{action['value']}" if current else action["value"])
    if kind == "sitemap_parent":
        found = pages.sitemap_parent.get(page_id)
        if found is None:
            raise ValueError("it has no parent in the sitemap")
        return found
    if kind == "url_parent":
        found = pages.url_parent(page_id)
        if found is None:
            raise ValueError("no page above it in the URL")
        return found
    found = pages.by_path.get(_norm(action["value"]).lower())
    if found is None:
        raise ValueError(f"there's no page at {action['value']}")
    return found


def plan(pages: Pages, name: str, definition: dict) -> Outcome:
    """What one rule would change, given the pages as they are now (earlier rules included)."""
    out = Outcome(name)
    hits = matching(pages, definition)
    out.matched = len(hits)
    date_tests = [c for g in definition["groups"] for c in g if c["field"] == "date" and c["op"] in ("before", "after")]
    if date_tests and definition["mode"] == "include":
        groups_with_dates = [g for g in definition["groups"] if any(c in date_tests for c in g)]
        out.undated = sum(1 for page_id in pages.rows if pages.dates.get(page_id) is None and any(
            all(_matches(pages, page_id, c) for c in g if c["field"] != "date") for g in groups_with_dates))
    for page_id in hits:
        row, changes = pages.rows[page_id], {}
        try:
            for action in definition["actions"]:
                value = _value_for(pages, page_id, action)
                if action["kind"] == "field" or action["kind"] == "blank":
                    value = rules._clean({action["field"]: value}, set(ACTION_FIELDS))[action["field"]]
                if action["field"] == "redirect_to" and value == page_id:
                    raise ValueError("it would redirect to itself")
                if action["field"] == "parent_id" and value == page_id:
                    raise ValueError("it would go under itself")
                current = row.get(action["field"])
                if (current if current is not None else "") != (value if value is not None else ""):
                    changes[action["field"]] = value
        except (ValueError, rules.RuleError) as e:
            out.skipped.append((page_id, str(e)))
            continue
        if changes:
            out.changed[page_id] = changes
    return out


def _remember(pages: Pages, changes: Dict[int, Dict[str, Any]]) -> None:
    for page_id, fields in changes.items():
        pages.rows[page_id].update(fields)
        if "parent_id" in fields:
            pages.sitemap_parent[page_id] = fields["parent_id"]


def run(conn: sqlite3.Connection, named: List[Tuple[str, dict]], write: bool) -> List[Outcome]:
    """Plan (and with write=True, apply) rules in order. Applying is one transaction: a page whose
    result would break an audit rule (a combine whose target is being removed, a move under a
    removed page...) keeps its old values and is reported as skipped."""
    if not write:
        pages = Pages.load(conn)
        outcomes = []
        for name, definition in named:
            outcome = plan(pages, name, definition)
            _remember(pages, outcome.changed)
            outcomes.append(outcome)
        return outcomes

    conn.execute("BEGIN IMMEDIATE")
    try:
        pages = Pages.load(conn)
        outcomes = []
        for name, definition in named:
            outcome = plan(pages, name, definition)
            if outcome.changed:
                _write(conn, pages, outcome)
            outcomes.append(outcome)
        conn.execute("COMMIT")
        return outcomes
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _write(conn, pages: Pages, outcome: Outcome) -> None:
    before = rules.problems(conn, list(outcome.changed))
    now = now_ms()
    for page_id, fields in list(outcome.changed.items()):
        if "parent_id" in fields:
            try:
                rules.check_parent(conn, [page_id], fields["parent_id"])
            except rules.RuleError as e:
                outcome.skipped.append((page_id, str(e)))
                del outcome.changed[page_id]
                continue
        old = {k: pages.rows[page_id].get(k) for k in fields}
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE pages SET {sets}, updated_at = ? WHERE id = ?", [*fields.values(), now, page_id])
        fields["__old__"] = old
    # Results that break an audit rule are put back.
    after = rules.problems(conn, list(outcome.changed))
    for page_id, msg in after.items():
        if page_id in outcome.changed and before.get(page_id) != msg:
            old = outcome.changed[page_id].pop("__old__")
            sets = ", ".join(f"{k} = ?" for k in old)
            conn.execute(f"UPDATE pages SET {sets} WHERE id = ?", [*old.values(), page_id])
            outcome.skipped.append((page_id, msg))
            del outcome.changed[page_id]
    marks = []
    for page_id, fields in outcome.changed.items():
        fields.pop("__old__", None)
        marks.extend((page_id, f, outcome.name, now) for f in fields)
    conn.executemany("INSERT INTO rule_marks(page_id, field, rule_name, applied_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(page_id, field) DO UPDATE SET rule_name = excluded.rule_name, "
                     "applied_at = excluded.applied_at", marks)
    _remember(pages, outcome.changed)


# ---- reporting ---------------------------------------------------------------------

def _shown(pages: Pages, name: str, value) -> str:
    if value in (None, ""):
        return "—"
    if ACTION_FIELDS.get(name, ("", ""))[1] == PAGE:
        r = pages.rows.get(value)
        return f"{r['label']} ({r['path']})" if r else str(value)
    if name == "page_type":
        return TYPE_NAMES.get(value, value)
    return str(value)


def report(conn, outcomes: List[Outcome], samples: int = 8) -> List[dict]:
    pages = Pages.load(conn)  # for labels; shows targets as they are now
    out = []
    for o in outcomes:
        sample = []
        for page_id, fields in list(o.changed.items())[:samples]:
            r = pages.rows[page_id]
            sample.append({"id": page_id, "label": r["label"], "path": r["path"],
                           "changes": [{"field": ACTION_FIELDS[f][0], "to": _shown(pages, f, v)}
                                       for f, v in fields.items() if f in ACTION_FIELDS]})
        out.append({"name": o.name, "matched": o.matched, "changed": len(o.changed), "undated": o.undated,
                    "sample": sample,
                    "skipped": [{"label": pages.rows[p]["label"] if p in pages.rows else str(p), "why": why}
                                for p, why in o.skipped[:samples]],
                    "skipped_count": len(o.skipped)})
    return out
