from contextlib import closing
from pathlib import Path

import pytest

from app import db, importer, rules, tree

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


def shape(roots):
    return {n.label: [c.label for c in n.children] for n, _, _ in tree.walk(roots)}


def decide(conn, path, **changes):
    page = pid(conn, path)
    ts = conn.execute("SELECT updated_at FROM pages WHERE id = ?", (page,)).fetchone()[0]
    rules.apply_changes(conn, page, changes, ts)


def test_tree_hides_home_gone_pages_and_collection_items(conn):
    roots = tree.load(conn)
    labels = [n.label for n in roots]
    assert "A Great Place to Learn" not in labels                    # "/" is the prototype's Home
    assert "old-page" not in labels and "gone" not in labels         # 404s
    assert shape(roots)["Admissions"] == ["Contact Us", "How to Apply"]  # untouched pages sort by label
    news = next(n for n in roots if n.label == "News")
    assert news.children == [] and news.collection_count == 2 and news.descendants == 2
    admissions = next(n for n in roots if n.label == "Admissions")
    assert admissions.descendants == 3


def test_removed_page_children_move_up_and_absorbs_counted(conn):
    apply_id = pid(conn, "/admissions/apply")
    decide(conn, "/admissions/contact", decision="combine", redirect_to=apply_id)
    decide(conn, "/about/contact", recommendation="combine", redirect_to=apply_id)
    decide(conn, "/admissions", decision="combine", redirect_to=pid(conn, "/campus-life"))
    roots = tree.load(conn)
    labels = [n.label for n in roots]
    assert "Admissions" not in labels and "How to Apply" in labels
    apply = next(n for n in roots if n.label == "How to Apply")
    assert apply.absorbs == 2 and [c.label for c in apply.children] == ["First-Year Students"]


def test_move_reorders_and_reparents(conn):
    admissions, campus = pid(conn, "/admissions"), pid(conn, "/campus-life")
    tree.move(conn, campus, admissions, 0)
    assert shape(tree.load(conn))["Admissions"] == ["Campus <Life>", "Contact Us", "How to Apply"]
    tree.move(conn, campus, admissions, 99)
    assert shape(tree.load(conn))["Admissions"] == ["Contact Us", "How to Apply", "Campus <Life>"]
    tree.move(conn, campus, None, 0)
    assert tree.load(conn)[0].label == "Campus <Life>"


def test_move_rejects_loops_and_removed_parents(conn):
    admissions, first_year = pid(conn, "/admissions"), pid(conn, "/admissions/apply/first-year")
    with pytest.raises(rules.RuleError, match="under itself"):
        tree.move(conn, admissions, first_year, 0)
    with pytest.raises(rules.RuleError, match="under itself"):
        tree.move(conn, admissions, admissions, 0)
    decide(conn, "/campus-life", decision="delete")
    with pytest.raises(rules.RuleError, match="being removed"):
        tree.move(conn, admissions, pid(conn, "/campus-life"), 0)
    assert conn.execute("SELECT parent_id FROM pages WHERE id = ?", (admissions,)).fetchone()[0] is None


def test_new_pages_can_be_added_named_and_removed(conn):
    section = tree.create_page(conn, "  Student   Life ", None)
    child = tree.create_page(conn, "Clubs", section)
    tree.move(conn, pid(conn, "/campus-life"), section, 0)
    assert shape(tree.load(conn))["Student Life"] == ["Campus <Life>", "Clubs"]
    with pytest.raises(rules.RuleError, match="Give the page a name"):
        tree.create_page(conn, "   ", None)
    decide(conn, "/about/history", decision="combine", redirect_to=child)
    with pytest.raises(rules.RuleError, match="redirect to \"Clubs\""):
        tree.delete_new_page(conn, child)
    with pytest.raises(rules.RuleError, match="from the crawl"):
        tree.delete_new_page(conn, pid(conn, "/campus-life"))
    tree.delete_new_page(conn, section)
    labels = [n.label for n in tree.load(conn)]
    assert "Student Life" not in labels and "Campus <Life>" in labels and "Clubs" in labels


def test_prototype_from_database(conn):
    conn.execute("UPDATE settings SET footer_md = ?, utility_md = ?",
                 ("- Help\n  - Contact Us\n  - Accessibility\n", "- Apply now\n"))
    files = tree.render_files(conn)
    assert "admissions/how-to-apply/first-year-students/index.html" in files
    assert files["robots.txt"].startswith("User-agent: *")
    assert '<meta name="robots" content="noindex, nofollow">' in files["index.html"]
    assert "Plus 2 more pages below this one" in files["news/index.html"]
    assert "accessibility/index.html" in files and "apply-now/index.html" in files
    assert "&lt;Life&gt;" in files["campus-life/index.html"] and "<Life>" not in files["campus-life/index.html"]
    assert not any(p.startswith(("old-page", "gone")) for p in files)


def test_depth_cap_folds_deeper_pages_into_a_note(conn):
    conn.execute("UPDATE settings SET max_depth = 1")
    files = tree.render_files(conn)
    assert "admissions/how-to-apply/index.html" not in files
    assert "Plus 3 more pages below this one" in files["admissions/index.html"]


def test_stamp_changes_with_tree_and_settings(conn):
    before = tree.stamp(conn)
    tree.move(conn, pid(conn, "/campus-life"), None, 0)
    after_move = tree.stamp(conn)
    conn.execute("UPDATE settings SET nav_style = 'sidebar'")
    assert len({before, after_move, tree.stamp(conn)}) == 3
