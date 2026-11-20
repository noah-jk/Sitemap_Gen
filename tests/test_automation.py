from contextlib import closing
from pathlib import Path

import pytest

from app import automation as auto, db, importer, rules, tree

FIXTURE = Path(__file__).parent / "fixtures" / "crawl-example.csv"


@pytest.fixture
def conn(tmp_path):
    store = db.Store(str(tmp_path / "data"))
    store.create_project("example", "Example University")
    with closing(store.project("example")) as c:
        with open(FIXTURE, encoding="utf-8", newline="") as f:
            importer.import_crawl(c, f, "crawl.csv")
        yield c


def pid(conn, path):
    return conn.execute("SELECT id FROM pages WHERE url_path = ?", (path,)).fetchone()[0]


def page(conn, path, *cols):
    return tuple(conn.execute(f"SELECT {', '.join(cols)} FROM pages WHERE url_path = ?", (path,)).fetchone())


def rule(groups, actions, mode="include"):
    return auto.clean_definition({"mode": mode, "groups": groups, "actions": actions})


def cond(field, op, value=None):
    return {"field": field, "op": op, "value": value}


def test_definitions_are_checked():
    ok = rule([[cond("path", "matches", "/academics/*/facilities")]],
              [{"field": "recommendation", "kind": "value", "value": "combine"},
               {"field": "redirect_to", "kind": "sitemap_parent"}])
    assert ok["groups"][0][0]["value"] == "/academics/*/facilities"
    bad = [
        ({"groups": [[cond("password_hash", "is", "x")]], "actions": [{"field": "status", "value": "done"}]}, "Unknown field"),
        ({"groups": [[cond("sessions", "contains", "x")]], "actions": [{"field": "status", "value": "done"}]}, "can't be used"),
        ({"groups": [[cond("sessions", "lt", "lots")]], "actions": [{"field": "status", "value": "done"}]}, "number"),
        ({"groups": [[cond("date", "before", "last year")]], "actions": [{"field": "status", "value": "done"}]}, "YYYY-MM-DD"),
        ({"groups": [], "actions": []}, "at least one"),
        ({"groups": [], "actions": [{"field": "label", "value": "x"}]}, "can't set"),
        ({"groups": [], "actions": [{"field": "decision", "kind": "field", "value": "notes"}]}, "copied"),
        ({"groups": [], "actions": [{"field": "status", "kind": "sitemap_parent"}]}, "can't be set"),
        ({"groups": [], "actions": [{"field": "redirect_to", "kind": "value", "value": 3}]}, "set to a page"),
        ({"groups": [], "actions": [{"field": "decision", "value": "maybe"}]}, "must be one of"),
        ({"groups": [], "actions": [{"field": "status", "value": "done"}, {"field": "status", "value": "todo"}]}, "once"),
    ]
    for definition, message in bad:
        with pytest.raises(auto.RuleDefinitionError, match=message):
            auto.clean_definition(definition)


def test_merge_sub_pages_into_their_parent(conn):
    merge = rule([[cond("path", "matches", "/admissions/*")]],
                 [{"field": "recommendation", "kind": "value", "value": "combine"},
                  {"field": "redirect_to", "kind": "sitemap_parent"}])
    preview = auto.run(conn, [("Merge into Admissions", merge)], write=False)[0]
    assert preview.matched == 2 and len(preview.changed) == 2   # /admissions/apply and /admissions/contact
    assert page(conn, "/admissions/apply", "recommendation") == (None,)  # a preview changes nothing
    auto.run(conn, [("Merge into Admissions", merge)], write=True)
    adm = pid(conn, "/admissions")
    assert page(conn, "/admissions/apply", "recommendation", "redirect_to") == ("combine", adm)
    assert page(conn, "/admissions/contact", "recommendation", "redirect_to") == ("combine", adm)
    marks = conn.execute("SELECT field, rule_name FROM rule_marks WHERE page_id = ? ORDER BY field",
                         (pid(conn, "/admissions/apply"),)).fetchall()
    assert [tuple(m) for m in marks] == [("recommendation", "Merge into Admissions"), ("redirect_to", "Merge into Admissions")]
    # Running it again changes nothing; a hand edit clears the mark for that field.
    assert auto.run(conn, [("Merge into Admissions", merge)], write=True)[0].changed == {}
    ts = page(conn, "/admissions/apply", "updated_at")[0]
    rules.apply_changes(conn, pid(conn, "/admissions/apply"), {"recommendation": "keep"}, ts)
    assert [m[0] for m in conn.execute("SELECT field FROM rule_marks WHERE page_id = ?",
                                       (pid(conn, "/admissions/apply"),))] == ["redirect_to"]


def test_news_date_cut_off(conn):
    old_news = rule([[cond("collection", "is", "news"), cond("date", "before", "2025-01-01")]],
                    [{"field": "recommendation", "kind": "value", "value": "delete"}])
    out = auto.run(conn, [("Old news", old_news)], write=True)[0]
    assert page(conn, "/news/2024/story-one", "recommendation") == ("delete",)
    assert page(conn, "/news/story-two", "recommendation") == (None,)
    assert out.undated == 1          # story-two has no date, so it's left out (and counted)


def test_rules_run_in_order_and_see_earlier_changes(conn):
    first = rule([[cond("path", "is", "/campus-life")]], [{"field": "recommendation", "kind": "value", "value": "delete"}])
    copy = rule([[cond("recommendation", "not_empty"), cond("decision", "is_empty")]],
                [{"field": "decision", "kind": "field", "value": "recommendation"}])
    outcomes = auto.run(conn, [("Campus life", first), ("Decide", copy)], write=True)
    assert page(conn, "/campus-life", "decision") == ("delete",)
    assert pid(conn, "/campus-life") in outcomes[1].changed
    # Pages with a decision already aren't touched, because the rule says "Final Decision is empty".
    ts = page(conn, "/about/history", "updated_at")[0]
    rules.apply_changes(conn, pid(conn, "/about/history"), {"recommendation": "delete", "decision": "keep"}, ts)
    auto.run(conn, [("Decide", copy)], write=True)
    assert page(conn, "/about/history", "decision") == ("keep",)


def test_results_that_break_a_rule_are_skipped(conn):
    ts = page(conn, "/admissions", "updated_at")[0]
    rules.apply_changes(conn, pid(conn, "/admissions"), {"decision": "delete"}, ts)
    merge = rule([[cond("path", "matches", "/admissions/*")]],
                 [{"field": "decision", "kind": "value", "value": "combine"},
                  {"field": "redirect_to", "kind": "sitemap_parent"}])
    # Admissions is being deleted, so its sub-pages have no parent in the sitemap any more:
    out = auto.run(conn, [("Merge", merge)], write=True)[0]
    assert out.changed == {} and len(out.skipped) == 2
    combine_into_deleted = rule([[cond("path", "is", "/campus-life")]],
                                [{"field": "decision", "kind": "value", "value": "combine"},
                                 {"field": "redirect_to", "kind": "path", "value": "/admissions"}])
    out = auto.run(conn, [("Into a deleted page", combine_into_deleted)], write=True)[0]
    assert out.changed == {} and "marked delete" in out.skipped[0][1]
    assert page(conn, "/campus-life", "decision", "redirect_to") == (None, None)  # put back


def test_exclude_paths_url_parents_and_missing_targets(conn):
    everything_but_news = rule([[cond("path", "matches", "/news/**")], [cond("path", "is", "/")]],
                               [{"field": "owner", "kind": "value", "value": "Web team"}], mode="exclude")
    auto.run(conn, [("Owner", everything_but_news)], write=True)
    assert page(conn, "/campus-life", "owner") == ("Web team",)
    assert page(conn, "/news/story-two", "owner") == ("",) and page(conn, "/", "owner") == ("",)
    # URL parent vs sitemap parent: move How to Apply under Campus Life in the sitemap.
    tree.move(conn, pid(conn, "/admissions/apply"), pid(conn, "/campus-life"), 0)
    by_url = rule([[cond("path", "is", "/admissions/apply")]], [{"field": "redirect_to", "kind": "url_parent"}])
    by_sitemap = rule([[cond("path", "is", "/admissions/apply")]], [{"field": "redirect_to", "kind": "sitemap_parent"}])
    url_out = auto.run(conn, [("u", by_url)], write=False)[0]
    sitemap_out = auto.run(conn, [("s", by_sitemap)], write=False)[0]
    apply_id = pid(conn, "/admissions/apply")
    assert url_out.changed[apply_id]["redirect_to"] == pid(conn, "/admissions")
    assert sitemap_out.changed[apply_id]["redirect_to"] == pid(conn, "/campus-life")
    missing = rule([[cond("path", "is", "/campus-life")]], [{"field": "redirect_to", "kind": "path", "value": "/nope"}])
    out = auto.run(conn, [("m", missing)], write=False)[0]
    assert out.skipped == [(pid(conn, "/campus-life"), "there's no page at /nope")]


def test_report_and_appending_notes(conn):
    note = rule([[cond("sessions", "eq", 0), cond("status_code", "lt", 400)]],
                [{"field": "notes", "kind": "append", "value": "No traffic"}])
    outcomes = auto.run(conn, [("No traffic", note)], write=True)
    rep = auto.report(conn, outcomes)[0]
    assert rep["changed"] == len(outcomes[0].changed) > 0 and rep["sample"][0]["changes"][0]["field"] == "Notes"
    again = auto.run(conn, [("No traffic", note)], write=True)[0]
    assert again.changed == {}   # the line is already there, so nothing is added twice


def test_combine_into_is_the_one_way_to_merge(conn):
    merge = rule([[cond("path", "matches", "/admissions/*")]],
                 [{"field": "combine_into", "kind": "sitemap_parent", "as": "decision"}])
    assert merge["actions"] == [{"field": "combine_into", "kind": "sitemap_parent", "as": "decision"}]
    auto.run(conn, [("Merge", merge)], write=True)
    adm = pid(conn, "/admissions")
    assert page(conn, "/admissions/contact", "recommendation", "decision", "redirect_to") == (None, "combine", adm)
    marks = {r[0] for r in conn.execute("SELECT field FROM rule_marks WHERE page_id = ?", (pid(conn, "/admissions/contact"),))}
    assert marks == {"decision", "redirect_to"}
    # Combine as a plain value is pointed at "Combine into"; split isn't something rules do.
    with pytest.raises(auto.RuleDefinitionError, match="Combine into"):
        rule([], [{"field": "recommendation", "kind": "value", "value": "combine"}])
    with pytest.raises(auto.RuleDefinitionError, match="split"):
        rule([], [{"field": "decision", "kind": "value", "value": "split"}])
    with pytest.raises(auto.RuleDefinitionError, match="once per rule"):
        rule([], [{"field": "combine_into", "kind": "url_parent"}, {"field": "redirect_to", "kind": "path", "value": "/x"}])
    with pytest.raises(auto.RuleDefinitionError, match="path of the page"):
        rule([], [{"field": "combine_into", "kind": "path", "value": ""}])


def test_rules_saved_the_old_way_become_combine_into(conn):
    old = rule([[cond("path", "is", "/campus-life")]],
               [{"field": "recommendation", "kind": "value", "value": "combine"},
                {"field": "status", "kind": "value", "value": "done"},
                {"field": "redirect_to", "kind": "path", "value": "/admissions"}])
    assert old["actions"] == [{"field": "combine_into", "kind": "path", "as": "recommendation", "value": "/admissions"},
                              {"field": "status", "kind": "value", "value": "done"}]
