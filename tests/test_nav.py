from contextlib import closing
from pathlib import Path

import pytest

from app import db, importer, nav, rules, tree

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


def labels(items):
    return [(i["display"], labels(i["children"])) if i["children"] else i["display"] for i in items]


def item_id(conn, menu, display):
    def find(items):
        for i in items:
            if i["display"] == display:
                return i["id"]
            found = find(i["children"])
            if found:
                return found
    return find(nav.load(conn)[menu])


def test_menus_start_empty_and_pages_are_added(conn):
    assert nav.load(conn) == {"main": [], "utility": [], "footer": []}
    added = nav.add_pages(conn, "main", None, [pid(conn, "/admissions"), pid(conn, "/news")])
    assert added == 2
    assert nav.add_pages(conn, "main", None, [pid(conn, "/admissions")]) == 0  # already there
    adm = item_id(conn, "main", "Admissions")
    nav.add_pages(conn, "main", adm, [pid(conn, "/admissions/apply/first-year"), pid(conn, "/admissions/apply")])
    assert labels(nav.load(conn)["main"]) == [("Admissions", ["First-Year Students", "How to Apply"]), "News"]
    assert nav.page_menus(conn)[pid(conn, "/admissions")] == ["main"]


def test_depth_and_column_limits(conn):
    nav.add_heading(conn, "footer", None, "About")
    about = item_id(conn, "footer", "About")
    nav.add_pages(conn, "footer", about, [pid(conn, "/about/contact")])
    contact = item_id(conn, "footer", "Contact Us")
    with pytest.raises(rules.RuleError, match="2 levels deep"):
        nav.add_pages(conn, "footer", contact, [pid(conn, "/news")])
    for name in ("Study", "Life", "Connect"):
        nav.add_heading(conn, "footer", None, name)
    with pytest.raises(rules.RuleError, match="room for 4 columns"):
        nav.add_heading(conn, "footer", None, "Fifth")
    with pytest.raises(rules.RuleError, match="room for 4 columns"):
        nav.move(conn, contact, "footer", None, 0)
    with pytest.raises(rules.RuleError, match="Give the heading a name"):
        nav.add_heading(conn, "main", None, "   ")


def test_move_reorders_reparents_and_changes_menu(conn):
    nav.add_pages(conn, "main", None, [pid(conn, "/admissions"), pid(conn, "/news"), pid(conn, "/campus-life")])
    adm, news = item_id(conn, "main", "Admissions"), item_id(conn, "main", "News")
    nav.move(conn, news, "main", None, 0)
    assert labels(nav.load(conn)["main"]) == ["News", "Admissions", "Campus <Life>"]
    nav.move(conn, news, "main", adm, 0)
    assert labels(nav.load(conn)["main"]) == [("Admissions", ["News"]), "Campus <Life>"]
    with pytest.raises(rules.RuleError, match="inside itself"):
        nav.move(conn, adm, "main", news, 0)
    nav.update(conn, adm, is_button=True)
    nav.move(conn, adm, "utility", None, 0)
    menus = nav.load(conn)
    assert labels(menus["utility"]) == [("Admissions", ["News"])] and not menus["utility"][0]["is_button"]


def test_labels_buttons_and_removal(conn):
    nav.add_pages(conn, "main", None, [pid(conn, "/admissions/apply")])
    apply = item_id(conn, "main", "How to Apply")
    nav.update(conn, apply, label="  Apply  ")
    assert labels(nav.load(conn)["main"]) == ["Apply"]
    nav.update(conn, apply, label="")                  # back to the page's own label
    assert labels(nav.load(conn)["main"]) == ["How to Apply"]
    nav.update(conn, apply, is_button=True)
    nav.add_pages(conn, "main", apply, [pid(conn, "/admissions/apply/first-year")])
    child = item_id(conn, "main", "First-Year Students")
    with pytest.raises(rules.RuleError, match="Only top-level main menu entries"):
        nav.update(conn, child, is_button=True)
    nav.remove(conn, apply, keep_children=True)        # its child moves up into its place
    assert labels(nav.load(conn)["main"]) == ["First-Year Students"]


def test_remove_takes_sub_entries_with_it(conn):
    nav.add_pages(conn, "main", None, [pid(conn, "/admissions"), pid(conn, "/campus-life")], subpage_depth=2)
    adm = item_id(conn, "main", "Admissions")
    assert nav.count_below(conn, adm) == 3
    assert nav.remove(conn, adm) == 4                   # the entry and everything under it
    assert labels(nav.load(conn)["main"]) == ["Campus <Life>"]
    assert conn.execute("SELECT count(*) FROM nav_items").fetchone()[0] == 1
    # A footer column goes with its links too (keeping them needs them moved first).
    col = nav.add_heading(conn, "footer", None, "Help")
    nav.add_pages(conn, "footer", col, [pid(conn, "/about/contact")])
    with pytest.raises(rules.RuleError, match="footer column"):
        nav.remove(conn, col, keep_children=True)
    assert nav.remove(conn, col) == 2 and nav.load(conn)["footer"] == []


def test_entries_flag_pages_that_wont_exist(conn):
    nav.add_pages(conn, "main", None, [pid(conn, "/campus-life"), pid(conn, "/news/story-two"), pid(conn, "/gone")])
    page = pid(conn, "/campus-life")
    ts = conn.execute("SELECT updated_at FROM pages WHERE id = ?", (page,)).fetchone()[0]
    rules.apply_changes(conn, page, {"decision": "delete"}, ts)
    problems = [i["problem"] for i in nav.load(conn)["main"]]
    assert "marked delete" in problems[0] and "News items" in problems[1] and "HTTP 404" in problems[2]


def test_prototype_uses_menus(conn):
    nav.add_pages(conn, "main", None, [pid(conn, "/admissions"), pid(conn, "/campus-life")])
    adm = item_id(conn, "main", "Admissions")
    nav.update(conn, adm, label="Apply & Visit", is_button=True)
    nav.add_heading(conn, "main", adm, "Ways to apply")
    heading = item_id(conn, "main", "Ways to apply")
    nav.add_pages(conn, "main", heading, [pid(conn, "/admissions/apply")])
    nav.add_heading(conn, "footer", None, "Help")
    nav.add_pages(conn, "footer", item_id(conn, "footer", "Help"), [pid(conn, "/about/contact")])
    nav.add_pages(conn, "utility", None, [pid(conn, "/news")])
    page = pid(conn, "/campus-life")
    ts = conn.execute("SELECT updated_at FROM pages WHERE id = ?", (page,)).fetchone()[0]
    rules.apply_changes(conn, page, {"decision": "delete"}, ts)   # its menu entry is left out

    files = tree.render_files(conn)
    home = files["index.html"]
    menu = home.split('<nav class="globalnav">')[1].split("</nav>")[0]
    assert 'class="nav-btn" href="admissions/index.html">Apply &amp; Visit</a>' in menu
    assert "Campus" not in menu and "About" not in menu          # only what was added
    footer = home.split("<footer>")[1]
    assert "<h3>Help</h3>" in footer and ">Contact Us</a>" in footer
    assert '<nav class="utilnav"><a class="util-item" href="news/index.html">News</a></nav>' in home
    assert "contact-us/index.html" in files                      # pages still built from the sitemap


def test_old_footer_text_used_until_menus_exist(conn):
    conn.execute("UPDATE settings SET footer_md = '- Old\n  - Contact Us\n'")
    assert "<h3>Old</h3>" in tree.render_files(conn)["index.html"]
    nav.add_heading(conn, "footer", None, "New")
    home = tree.render_files(conn)["index.html"]
    assert "<h3>New</h3>" in home and "<h3>Old</h3>" not in home


def test_stamp_changes_with_menus(conn):
    before = tree.stamp(conn)
    nav.add_heading(conn, "main", None, "X")
    assert tree.stamp(conn) != before


def test_add_with_sub_pages_follows_the_sitemap(conn):
    adm = pid(conn, "/admissions")
    assert nav.add_pages(conn, "main", None, [adm], subpage_depth=1) == 3
    assert labels(nav.load(conn)["main"]) == [("Admissions", ["Contact Us", "How to Apply"])]
    assert nav.add_pages(conn, "main", None, [adm], subpage_depth=2) == 1   # only the new grandchild
    assert labels(nav.load(conn)["main"]) == [("Admissions", ["Contact Us", ("How to Apply", ["First-Year Students"])])]


def test_sub_pages_respect_menu_depth_and_skip_removed_pages(conn):
    contact = pid(conn, "/admissions/contact")
    ts = conn.execute("SELECT updated_at FROM pages WHERE id = ?", (contact,)).fetchone()[0]
    rules.apply_changes(conn, contact, {"decision": "delete"}, ts)
    assert nav.add_pages(conn, "footer", None, [pid(conn, "/admissions")], subpage_depth=3) == 2
    assert labels(nav.load(conn)["footer"]) == [("Admissions", ["How to Apply"])]   # footer: 2 levels
    assert nav.add_pages(conn, "main", None, [pid(conn, "/news")], subpage_depth=1) == 1  # news items left out


def test_pull_in_new_sub_pages(conn):
    adm = pid(conn, "/admissions")
    nav.add_pages(conn, "main", None, [adm], subpage_depth=1)
    entry = item_id(conn, "main", "Admissions")
    nav.remove(conn, item_id(conn, "main", "Contact Us"))
    tree.create_page(conn, "Visit Campus", adm)
    assert nav.add_subpages(conn, entry) == 2        # the removed one comes back, plus the new page
    assert labels(nav.load(conn)["main"]) == [("Admissions", ["How to Apply", "Contact Us", "Visit Campus"])]
    assert nav.add_subpages(conn, entry) == 0
    heading = nav.add_heading(conn, "main", None, "More")
    with pytest.raises(rules.RuleError, match="Headings aren't pages"):
        nav.add_subpages(conn, heading)
