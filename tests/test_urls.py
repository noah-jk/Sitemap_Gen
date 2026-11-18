from contextlib import closing
from pathlib import Path

import pytest

from app import db, importer, rules, tree, urls

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


def change(conn, page_id, **changes):
    ts = conn.execute("SELECT updated_at FROM pages WHERE id = ?", (page_id,)).fetchone()[0]
    return rules.apply_changes(conn, page_id, changes, ts)


def test_existing_pages_keep_their_url(conn):
    planned = urls.planned(conn)
    assert planned[pid(conn, "/admissions/apply")] == "/admissions/apply"
    assert planned[pid(conn, "/")] == "/"


def test_new_pages_follow_the_sitemap(conn):
    adm = pid(conn, "/admissions")
    visit = tree.create_page(conn, "Visit Campus", adm)
    assert urls.planned(conn)[visit] == "/admissions/visit-campus"
    change(conn, visit, label="Visit & Tour")                  # rename
    assert urls.planned(conn)[visit] == "/admissions/visit-tour"
    tree.move(conn, visit, pid(conn, "/about/history"), 0)       # move
    assert urls.planned(conn)[visit] == "/about/history/visit-tour"
    top = tree.create_page(conn, "Student Life", None)
    under_new = tree.create_page(conn, "Clubs", top)
    assert urls.planned(conn)[top] == "/student-life"
    assert urls.planned(conn)[under_new] == "/student-life/clubs"


def test_clashes_get_a_number_and_the_older_page_keeps_its_url(conn):
    adm = pid(conn, "/admissions")
    first = tree.create_page(conn, "Apply", adm)                 # /admissions/apply exists already
    second = tree.create_page(conn, "Apply", adm)
    planned = urls.planned(conn)
    assert planned[first] == "/admissions/apply-2" and planned[second] == "/admissions/apply-3"


def test_typed_urls_win_and_children_build_on_them(conn):
    adm = pid(conn, "/admissions")
    visit = tree.create_page(conn, "Visit", adm)
    change(conn, adm, new_url="https://www.example.edu/admission/")  # an existing page moving
    planned = urls.planned(conn)
    assert planned[adm] == "/admission" and planned[visit] == "/admission/visit"
    change(conn, visit, new_url="tours")
    assert urls.planned(conn)[visit] == "/tours"
    change(conn, visit, new_url="")                                  # back to following the sitemap
    assert urls.planned(conn)[visit] == "/admission/visit"


def test_a_parent_being_removed_is_skipped(conn):
    apply = pid(conn, "/admissions/apply")
    page = tree.create_page(conn, "Deadlines", apply)
    assert urls.planned(conn)[page] == "/admissions/apply/deadlines"
    change(conn, apply, decision="delete")                          # the sitemap now shows it under Admissions
    assert urls.planned(conn)[page] == "/admissions/deadlines"


def test_a_typed_url_must_be_free(conn):
    visit = tree.create_page(conn, "Visit", None)
    with pytest.raises(rules.RuleError, match='already used by "Contact Us"'):
        change(conn, visit, new_url="/about/contact")
    change(conn, pid(conn, "/about/contact"), decision="delete")    # a page being removed frees its URL
    change(conn, visit, new_url="/about/contact")
    with pytest.raises(rules.RuleError, match="spaces"):
        change(conn, visit, new_url="/a page")


def test_planning_fields_validate(conn):
    page = pid(conn, "/admissions")
    change(conn, page, owner="  Jane   Doe ", writer="Sam", priority="high", suggested_content="Hero, 3 cards")
    row = conn.execute("SELECT owner, writer, priority, suggested_content FROM pages WHERE id = ?", (page,)).fetchone()
    assert tuple(row) == ("Jane Doe", "Sam", "high", "Hero, 3 cards")
    with pytest.raises(rules.RuleError, match="Priority"):
        change(conn, page, priority="urgent")
    n, _ = rules.bulk_apply(conn, [page, pid(conn, "/campus-life")], {"owner": "Comms", "priority": "low"})
    assert n == 2
