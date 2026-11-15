import csv
import io
from contextlib import closing
from pathlib import Path

import pytest

import sitemap2proto as sp
from app import db, exports, importer, rules

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


def decide(conn, path, **changes):
    page = pid(conn, path)
    ts = conn.execute("SELECT updated_at FROM pages WHERE id = ?", (page,)).fetchone()[0]
    for key in ("redirect_to",):
        if isinstance(changes.get(key), str):
            changes[key] = pid(conn, changes[key])
    rules.apply_changes(conn, page, changes, ts)


def read_csv(text):
    assert text.startswith(exports.BOM)
    return list(csv.DictReader(io.StringIO(text[1:])))


def test_redirect_map_resolves_old_redirect_chains(conn):
    decide(conn, "/admissions", decision="combine", redirect_to="/admissions/apply")
    decide(conn, "/admissions/contact", decision="combine", redirect_to="/about/contact")
    decide(conn, "/old-page", decision="delete", redirect_to="/admissions/apply")
    decide(conn, "/gone", decision="delete")
    decide(conn, "/campus-life", recommendation="delete")
    m = exports.redirect_map(conn)
    assert m.ok and m.will_404 == 1 and m.undecided == 1
    rows = [(r["Type"], r["From URL"], r["To URL"]) for r in read_csv(m.csv())]
    apply = "https://www.example.edu/admissions/apply"
    assert rows[0] == ("host", "https://example.edu/*", "https://www.example.edu/*")
    assert ("page", "https://www.example.edu/admissions", apply) in rows
    assert ("page", "https://www.example.edu/admissions/contact", "https://www.example.edu/about/contact") in rows
    assert ("page", "https://www.example.edu/old-page", apply) in rows
    # Old redirects that pointed at /admissions, directly or through another old redirect, go straight to the end.
    assert ("existing", "https://www.example.edu/admissions/", apply) in rows
    assert ("existing", "https://www.example.edu/moved", apply) in rows
    assert ("existing", "https://www.example.edu/older", apply) in rows
    assert not any(r[1] == "https://www.example.edu/athletics" for r in rows)
    assert len(rows) == 7


def test_redirect_map_blocked_by_rule_problems(conn):
    decide(conn, "/admissions/contact", decision="combine", redirect_to="/about/contact")
    decide(conn, "/about/contact", decision="delete")
    m = exports.redirect_map(conn)
    assert not m.ok
    assert list(m.blocking_labels.values()) == ["Contact Us"]


def test_recommendation_alone_is_not_exported(conn):
    decide(conn, "/admissions/contact", recommendation="combine", redirect_to="/about/contact")
    rows = read_csv(exports.redirect_map(conn).csv())
    assert [r["Type"] for r in rows] == ["host"]


def test_existing_redirect_to_deleted_page_is_reported(conn):
    decide(conn, "/admissions", decision="delete")
    m = exports.redirect_map(conn)
    assert any("/moved redirects to" in line for line in m.unresolved)


def test_content_audit_layout_and_formula_guard(conn):
    decide(conn, "/admissions/contact", recommendation="combine", decision="keep",
           redirect_to="/about/contact", notes="=SUM(A1)")
    rows = read_csv(exports.content_audit_csv(conn))
    header = list(rows[0])
    assert header[:5] == ["Address", "Recommendation", "Final Decision", "Notes", "Redirect To"]
    assert "Content Goes To" not in header  # no splits yet, so the column is empty and left out
    assert "Word Count" not in header and "Text Ratio" in header
    by_url = {r["Address"]: r for r in rows}
    contact = by_url["https://www.example.edu/admissions/contact"]
    assert (contact["Recommendation"], contact["Final Decision"]) == ("combine", "keep")
    assert contact["Notes"] == "'=SUM(A1)"
    assert contact["Redirect To"] == "https://www.example.edu/about/contact"
    assert by_url["https://www.example.edu/news/story-two"]["Title"].startswith("'=HYPERLINK")
    assert by_url["https://www.example.edu/admissions"]["GA4 Sessions"] == "520"
    assert len(rows) == 15


def test_safe_cell():
    assert exports.safe_cell("-5 tips") == "'-5 tips"
    assert exports.safe_cell(-5) == "-5"
    assert exports.safe_cell(None) == ""
    assert exports.safe_cell("Plain") == "Plain"


def test_markdown_sitemap_round_trips_through_the_generator(conn):
    decide(conn, "/admissions", decision="combine", redirect_to="/admissions/apply")
    text = exports.markdown_sitemap(conn, "Example University", "2026-11-15")
    assert text.startswith("# Example University sitemap\n")
    assert "news (2 pages)" in text
    lines = [l for l in text.splitlines() if l.lstrip().startswith("- ")]
    assert "- How to Apply" in lines            # parent was combined away, so it moves up
    assert "  - First-Year Students" in lines
    assert not any("Admissions" == l.strip("- ") or "Story" in l for l in lines)
    root = sp.parse_markdown(text)
    count = sum(1 for _ in sp.all_pages(sp.Site(root))) - 1
    assert count == len(lines)
