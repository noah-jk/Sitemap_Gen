import sqlite3
from contextlib import closing

import pytest

from app import history
from tests.test_app import FIXTURE, client, csrf_from, grid_csrf, imported, logged_in, page_by_path, patcher  # noqa: F401


def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "data" / "projects" / "example.db")
    conn.row_factory = sqlite3.Row
    return conn


def state(tmp_path) -> dict:
    """Everything undo is meant to restore, without the updated_at stamps."""
    with closing(db(tmp_path)) as conn:
        def rows(sql):
            return [{k: r[k] for k in r.keys() if k != "updated_at"} for r in conn.execute(sql)]
        return {"pages": rows("SELECT * FROM pages ORDER BY id"),
                "targets": rows("SELECT * FROM page_content_targets ORDER BY page_id, target_id"),
                "nav": rows("SELECT * FROM nav_items ORDER BY id"),
                "settings": rows("SELECT * FROM settings")}


@pytest.fixture
def hdr(imported):
    return {"X-CSRF-Token": grid_csrf(imported)}


def undo(client, hdr):
    return client.post("/api/p/example/history/undo", headers=hdr)


def redo(client, hdr):
    return client.post("/api/p/example/history/redo", headers=hdr)


def test_single_edit_undo_and_redo(imported, hdr, tmp_path):
    before = state(tmp_path)
    patch = patcher(imported, hdr["X-CSRF-Token"])
    assert patch(page_by_path(imported, "/about/history"), decision="delete").status_code == 200
    after = state(tmp_path)
    listed = imported.get("/api/p/example/history").json()
    assert listed["undo"] == 'Set Final Decision to delete on "history"' and listed["redo"] is None
    assert listed["actions"][0]["user"] == "noah"

    r = undo(imported, hdr)
    assert r.status_code == 200 and r.json()["label"] == 'Set Final Decision to delete on "history"'
    assert state(tmp_path) == before
    assert r.json()["undo"] is None and r.json()["redo"] == 'Set Final Decision to delete on "history"'
    # The grid's copy of the row is stale now, so saving it again is a conflict, not a silent overwrite.
    row = page_by_path(imported, "/about/history")
    assert row["decision"] is None

    assert redo(imported, hdr).status_code == 200
    assert state(tmp_path) == after
    assert undo(imported, hdr).status_code == 200 and undo(imported, hdr).status_code == 400  # nothing left


def test_bulk_edit_is_one_action(imported, hdr, tmp_path):
    before = state(tmp_path)
    r = imported.post("/api/p/example/pages/bulk", headers=hdr, json={
        "match": {"filters": [{"field": "url_path", "value": "/admissions"}]}, "changes": {"recommendation": "review"}})
    assert r.json()["count"] == 4
    assert imported.get("/api/p/example/history").json()["undo"] == "Set Recommendation to review on 4 pages"
    assert undo(imported, hdr).status_code == 200
    assert state(tmp_path) == before


def test_combine_with_content_targets_undo(imported, hdr, tmp_path):
    before = state(tmp_path)
    patch = patcher(imported, hdr["X-CSRF-Token"])
    row = page_by_path(imported, "/admissions/contact")
    a, b = page_by_path(imported, "/about/contact"), page_by_path(imported, "/campus-life")
    r = patch(row, decision="split", redirect_to=a["id"], content_targets=[a["id"], b["id"]])
    assert r.status_code == 200
    assert undo(imported, hdr).status_code == 200
    assert state(tmp_path) == before


def test_sitemap_edits_undo_in_order(imported, hdr, tmp_path):
    before = state(tmp_path)
    new_id = imported.post("/api/p/example/tree/pages", json={"label": "Student Life", "parent_id": None},
                           headers=hdr).json()["id"]
    adm = page_by_path(imported, "/admissions")
    assert imported.post("/api/p/example/tree/move", json={"id": adm["id"], "parent_id": new_id, "index": 0},
                         headers=hdr).status_code == 200
    imported.post("/api/p/example/nav/pages", json={"menu": "main", "page_ids": [new_id], "subpage_depth": 2},
                  headers=hdr)
    labels = [a["label"] for a in imported.get("/api/p/example/history").json()["actions"]]
    assert labels == ["Added 4 entries to the main menu", 'Moved "Admissions"', 'Added page "Student Life"']

    # Deleting the new page also removes its menu entries and lifts Admissions back up; undo brings it all back.
    middle = state(tmp_path)
    assert imported.delete(f"/api/p/example/tree/pages/{new_id}", headers=hdr).status_code == 200
    assert undo(imported, hdr).status_code == 200
    assert state(tmp_path) == middle

    for _ in range(3):
        assert undo(imported, hdr).status_code == 200
    assert state(tmp_path) == before
    for _ in range(3):
        assert redo(imported, hdr).status_code == 200
    assert state(tmp_path) == middle


def test_menu_edits_undo(imported, hdr, tmp_path):
    post = lambda path, body: imported.post(f"/api/p/example/nav/{path}", json=body, headers=hdr)
    adm = page_by_path(imported, "/admissions")["id"]
    post("pages", {"menu": "main", "page_ids": [adm], "subpage_depth": 1})
    post("headings", {"menu": "footer", "label": "Help"})
    before = state(tmp_path)
    first = imported.get("/api/p/example/nav").json()["menus"]["main"][0]["id"]
    imported.patch(f"/api/p/example/nav/{first}", json={"label": "Apply"}, headers=hdr)
    imported.patch(f"/api/p/example/nav/{first}", json={"is_button": True}, headers=hdr)
    imported.delete(f"/api/p/example/nav/{first}", headers=hdr)  # its sub-entries move up
    labels = [a["label"] for a in imported.get("/api/p/example/history").json()["actions"]][:3]
    assert labels == ['Removed "Apply" from the main menu', 'Made "Apply" a button',
                      'Renamed "Admissions" in the main menu to "Apply"']
    for _ in range(3):
        assert undo(imported, hdr).status_code == 200
    assert state(tmp_path) == before


def test_prototype_settings_undo(imported, hdr, tmp_path):
    before = state(tmp_path)
    token = csrf_from(imported.get("/p/example/prototype").text)
    imported.post("/p/example/prototype/settings", data={"csrf": token, "nav_style": "sidebar", "menu": "mega"},
                  follow_redirects=False)
    assert imported.get("/api/p/example/history").json()["undo"] == "Changed the prototype settings"
    assert undo(imported, hdr).status_code == 200
    assert state(tmp_path) == before


def test_new_edit_drops_redo(imported, hdr):
    patch = patcher(imported, hdr["X-CSRF-Token"])
    patch(page_by_path(imported, "/about/history"), decision="delete")
    undo(imported, hdr)
    patch(page_by_path(imported, "/campus-life"), status="done")
    listed = imported.get("/api/p/example/history").json()
    assert listed["redo"] is None and [a["label"] for a in listed["actions"]] == ['Set Status to done on "Campus <Life>"']
    assert redo(imported, hdr).status_code == 400


def test_reimport_isnt_undone(imported, hdr, tmp_path):
    """Undoing an edit puts back what the person changed, not the crawl numbers from before a re-import."""
    patch = patcher(imported, hdr["X-CSRF-Token"])
    patch(page_by_path(imported, "/about/history"), notes="thin page")
    with closing(db(tmp_path)) as conn:
        conn.execute("UPDATE pages SET sessions = 999 WHERE url_path = '/about/history'")  # as a re-import would
        conn.commit()
    assert undo(imported, hdr).status_code == 200
    row = page_by_path(imported, "/about/history")
    assert row["notes"] == "" and row["sessions"] == 999


def test_unrecorded_change_blocks_undo(imported, hdr, tmp_path):
    patch = patcher(imported, hdr["X-CSRF-Token"])
    patch(page_by_path(imported, "/about/history"), notes="first")
    with closing(db(tmp_path)) as conn:
        conn.execute("UPDATE pages SET notes = 'changed elsewhere' WHERE url_path = '/about/history'")
        conn.commit()
    before = state(tmp_path)
    r = undo(imported, hdr)
    assert r.status_code == 409 and "changed since" in r.json()["detail"]
    assert state(tmp_path) == before


def test_failed_edit_records_nothing(imported, hdr):
    patch = patcher(imported, hdr["X-CSRF-Token"])
    assert patch(page_by_path(imported, "/admissions/contact"), decision="combine").status_code == 400
    assert imported.get("/api/p/example/history").json()["actions"] == []


def test_history_needs_login_and_csrf(imported, hdr):
    assert imported.post("/api/p/example/history/undo", headers={"X-CSRF-Token": "forged"}).status_code == 403
    imported.post("/logout", data={"csrf": hdr["X-CSRF-Token"]})
    assert imported.get("/api/p/example/history").status_code == 401


def test_only_recent_actions_are_kept(tmp_path):
    from app.db import Store
    store = Store(str(tmp_path / "data"))
    store.create_project("example", "Example")
    with closing(store.project("example")) as conn:
        conn.execute("INSERT INTO pages(url_path, label, updated_at) VALUES ('/a', 'A', 0)")
        for i in range(205):
            with history.record(conn, "noah", f"edit {i}"):
                conn.execute("UPDATE pages SET notes = ?", (str(i),))
        labels = [r[0] for r in conn.execute("SELECT label FROM actions ORDER BY id")]
        assert len(labels) == 200 and labels[0] == "edit 5" and labels[-1] == "edit 204"
        assert conn.execute("SELECT count(*) FROM action_rows").fetchone()[0] == 200
