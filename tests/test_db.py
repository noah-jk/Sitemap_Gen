import sqlite3
from contextlib import closing

import pytest

from app import db


@pytest.fixture
def store(tmp_path):
    return db.Store(str(tmp_path / "data"))


def add_page(conn, path, **cols):
    cols = {"url_path": path, "label": path, "updated_at": db.now_ms(), **cols}
    cur = conn.execute(f"INSERT INTO pages({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                       list(cols.values()))
    return cur.lastrowid


def test_new_project_gets_current_schema(store):
    store.create_project("whittier", "Whittier College")
    with closing(store.project("whittier")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.PROJECT_MIGRATIONS)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT nav_style FROM settings").fetchone()[0] == "grid"
    assert [r["slug"] for r in store.list_projects()] == ["whittier"]


@pytest.mark.parametrize("slug", ["../evil", "Upper", "a/b", "", "-x", "x-", "a" * 65, "x.db"])
def test_slugs_that_could_escape_the_folder_are_rejected(store, slug):
    with pytest.raises(ValueError):
        store.create_project(slug, "x")


def test_unregistered_project_cannot_be_opened(store, tmp_path):
    with pytest.raises(KeyError):
        store.project("ghost")


def test_duplicate_project_rejected(store):
    store.create_project("a", "A")
    with pytest.raises((sqlite3.IntegrityError, FileExistsError)):
        store.create_project("a", "A again")


def test_leftover_file_is_not_silently_reused(store):
    open(store.project_path("old"), "w").close()
    with pytest.raises(FileExistsError):
        store.create_project("old", "Old")
    assert store.list_projects() == []


def test_constraints_reject_bad_rows(store):
    store.create_project("p", "P")
    with closing(store.project("p")) as conn:
        a = add_page(conn, "/a")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE pages SET recommendation = 'merge' WHERE id = ?", (a,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE pages SET redirect_to = id WHERE id = ?", (a,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE pages SET redirect_to = 999 WHERE id = ?", (a,))
        with pytest.raises(sqlite3.IntegrityError):
            add_page(conn, "/a")
        with pytest.raises(sqlite3.IntegrityError):
            add_page(conn, None)  # only planned (is_new) pages may lack a URL
        add_page(conn, None, is_new=1, label="Planned page")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE settings SET mega_menu = 1, dropdown_depth = 2")


def test_deleting_a_target_clears_links(store):
    store.create_project("p", "P")
    with closing(store.project("p")) as conn:
        a, b = add_page(conn, "/a"), add_page(conn, "/b")
        conn.execute("UPDATE pages SET redirect_to = ? WHERE id = ?", (b, a))
        conn.execute("INSERT INTO page_content_targets VALUES (?, ?)", (a, b))
        conn.execute("DELETE FROM pages WHERE id = ?", (b,))
        assert conn.execute("SELECT redirect_to FROM pages WHERE id = ?", (a,)).fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM page_content_targets").fetchone()[0] == 0


def test_migrate_applies_only_new_steps_and_rolls_back_failures(tmp_path):
    with closing(db.connect(str(tmp_path / "t.db"))) as conn:
        steps = ["CREATE TABLE a(x);"]
        assert db.migrate(conn, steps) == 1
        assert db.migrate(conn, steps) == 1  # idempotent
        with pytest.raises(sqlite3.OperationalError):
            db.migrate(conn, steps + ["CREATE TABLE b(x); CREATE TABLE a(x);"])
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'b'").fetchone()[0] == 0
        with pytest.raises(RuntimeError):
            db.migrate(conn, [])  # database newer than code


def test_migrate_all_and_backup(store, tmp_path):
    store.create_project("a", "A")
    store.create_project("b", "B")
    with closing(store.project("a")) as conn:
        add_page(conn, "/x")
    store.migrate_all()
    written = store.backup_all(str(tmp_path / "backups"))
    assert len(written) == 3
    a_copy = next(p for p in written if "/a-" in p)
    with closing(sqlite3.connect(a_copy)) as conn:
        assert conn.execute("SELECT url_path FROM pages").fetchone()[0] == "/x"
