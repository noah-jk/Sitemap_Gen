from contextlib import closing

import pytest

from app import db, rules


@pytest.fixture
def conn(tmp_path):
    store = db.Store(str(tmp_path / "data"))
    store.create_project("p", "P")
    with closing(store.project("p")) as c:
        for path in ["/a", "/b", "/c", "/d", "/e"]:
            c.execute("INSERT INTO pages(url_path, label, updated_at) VALUES (?, ?, 1)", (path, path[1:].upper()))
        yield c


def ids(conn):
    return {r["url_path"][1:]: r["id"] for r in conn.execute("SELECT id, url_path FROM pages")}


def ts(conn, page_id):
    return conn.execute("SELECT updated_at FROM pages WHERE id = ?", (page_id,)).fetchone()[0]


def save(conn, page_id, **changes):
    return rules.apply_changes(conn, page_id, changes, ts(conn, page_id))


def test_combine_needs_a_target_and_saves_with_one(conn):
    i = ids(conn)
    with pytest.raises(rules.RuleError, match="Needs a page to redirect to"):
        save(conn, i["a"], decision="combine")
    assert conn.execute("SELECT decision FROM pages WHERE id = ?", (i["a"],)).fetchone()[0] is None
    save(conn, i["a"], decision="combine", redirect_to=i["b"])
    row = conn.execute("SELECT decision, redirect_to FROM pages WHERE id = ?", (i["a"],)).fetchone()
    assert tuple(row) == ("combine", i["b"])


def test_decision_overrides_recommendation(conn):
    i = ids(conn)
    save(conn, i["a"], recommendation="combine", redirect_to=i["b"])
    save(conn, i["a"], decision="keep")                # client keeps it: no target rule applies
    save(conn, i["a"], redirect_to=None)               # fine, effective disposition is keep
    with pytest.raises(rules.RuleError):
        save(conn, i["a"], decision=None)              # back to the recommendation, which needs a target


def test_cannot_target_self_missing_or_removed_pages(conn):
    i = ids(conn)
    with pytest.raises(rules.RuleError, match="itself"):
        save(conn, i["a"], decision="combine", redirect_to=i["a"])
    with pytest.raises(rules.RuleError, match="doesn't exist"):
        save(conn, i["a"], decision="combine", redirect_to=99999)
    save(conn, i["b"], decision="delete")
    with pytest.raises(rules.RuleError, match='Redirects to "B", which is marked delete'):
        save(conn, i["a"], decision="combine", redirect_to=i["b"])


def test_split_rules(conn):
    i = ids(conn)
    with pytest.raises(rules.RuleError, match="two or more"):
        save(conn, i["a"], decision="split", redirect_to=i["b"], content_targets=[i["b"]])
    with pytest.raises(rules.RuleError, match="one of the pages its content goes to"):
        save(conn, i["a"], decision="split", redirect_to=i["d"], content_targets=[i["b"], i["c"]])
    save(conn, i["a"], decision="split", redirect_to=i["b"], content_targets=[i["b"], i["c"]])
    targets = {r[0] for r in conn.execute("SELECT target_id FROM page_content_targets WHERE page_id = ?", (i["a"],))}
    assert targets == {i["b"], i["c"]}


def test_removing_a_target_is_allowed_but_flags_pointing_pages(conn):
    i = ids(conn)
    save(conn, i["a"], decision="combine", redirect_to=i["c"])
    save(conn, i["b"], decision="split", redirect_to=i["c"], content_targets=[i["c"], i["d"]])
    _, warning = save(conn, i["c"], decision="delete")
    assert warning.startswith("2 other pages point here")
    found = rules.problems(conn)
    assert set(found) == {i["a"], i["b"]}
    assert 'which is marked delete' in found[i["a"]]
    assert "being removed" in found[i["b"]]
    save(conn, i["c"], decision="keep")
    assert rules.problems(conn) == {}


def test_stale_edit_is_rejected(conn):
    i = ids(conn)
    rules.apply_changes(conn, i["a"], {"notes": "first"}, 1)
    with pytest.raises(rules.Conflict):
        rules.apply_changes(conn, i["a"], {"notes": "second"}, 1)
    assert conn.execute("SELECT notes FROM pages WHERE id = ?", (i["a"],)).fetchone()[0] == "first"


@pytest.mark.parametrize("changes", [{"decision": "merge"}, {"status": "doing"},
                                     {"redirect_to": "5"}, {"content_targets": "1,2"},
                                     {"url_path": "/x"}, {"label": "   "}, {"is_button": "yes"}])
def test_bad_values_rejected(conn, changes):
    with pytest.raises(rules.RuleError):
        save(conn, ids(conn)["a"], **changes)


def test_bulk_is_all_or_nothing(conn):
    i = ids(conn)
    count, _ = rules.bulk_apply(conn, [i["a"], i["b"]], {"decision": "combine", "redirect_to": i["c"]})
    assert count == 2
    with pytest.raises(rules.RuleError, match="in the selection"):
        rules.bulk_apply(conn, [i["d"], i["e"]], {"decision": "combine", "redirect_to": i["d"]})
    with pytest.raises(rules.RuleError, match="2 pages would break a rule"):
        rules.bulk_apply(conn, [i["a"], i["b"], i["d"]], {"redirect_to": None})
    assert conn.execute("SELECT count(*) FROM pages WHERE redirect_to IS NOT NULL").fetchone()[0] == 2


def test_bulk_warns_about_pages_left_without_a_target(conn):
    i = ids(conn)
    rules.bulk_apply(conn, [i["a"], i["b"]], {"decision": "combine", "redirect_to": i["c"]})
    _, warning = rules.bulk_apply(conn, [i["c"], i["d"]], {"decision": "delete"})
    assert warning.startswith("2 other pages now need a new target")
