import csv
import io
import json
from contextlib import closing
from pathlib import Path

import pytest

from app import db, importer

FIXTURE = Path(__file__).parent / "fixtures" / "crawl-example.csv"


@pytest.fixture
def conn(tmp_path):
    store = db.Store(str(tmp_path / "data"))
    store.create_project("example", "Example")
    with closing(store.project("example")) as c:
        yield c


def run(conn, text=None):
    text = FIXTURE.read_text(encoding="utf-8") if text is None else text
    return importer.import_crawl(conn, io.StringIO(text, newline=""), "crawl.csv", "2025-06-01..2026-06-01")


def pages(conn):
    return {r["url_path"]: r for r in conn.execute("SELECT * FROM pages")}


def edit_fixture(drop=(), change=None):
    rows = list(csv.reader(io.StringIO(FIXTURE.read_text(encoding="utf-8"), newline="")))
    header, body = rows[0], rows[1:]
    body = [r for r in body if r[0] not in drop]
    for r in body:
        if change and r[0] in change:
            for col, val in change[r[0]].items():
                r[header.index(col)] = val
    out = io.StringIO()
    csv.writer(out).writerows([header] + body)
    return out.getvalue()


def test_routes_rows_and_merges_variants(conn):
    s = run(conn)
    p = pages(conn)
    assert sorted(p) == ["/", "/about/contact", "/about/history", "/admissions", "/admissions/apply",
                         "/admissions/apply/first-year", "/admissions/contact", "/campus-life", "/gone",
                         "/news", "/news/2024/story-one", "/news/story-two", "/old-page",
                         "/orphan/deep/page", "/restricted"]
    assert (s.pages_new, s.error_pages, s.redirects) == (15, 3, 5)
    assert dict(s.skipped) == {"non-HTML (application/pdf)": 1, "non-HTML (text/css)": 1,
                               "broken file link (404 to a document)": 1}
    assert p["/"]["current_url"] == "https://www.example.edu/"
    assert set(json.loads(p["/"]["variants"])) == {"https://www.example.edu/", "https://example.edu/",
                                                   "http://www.example.edu/"}


def test_ga4_counts_each_host_once_and_sums_hosts(conn):
    run(conn)
    p = pages(conn)
    assert p["/"]["sessions"] == 1100       # www 1000 (http:// repeats it) + bare 100
    assert p["/admissions"]["sessions"] == 520
    assert p["/admissions"]["views"] == 825


def test_detects_host_and_suffix_and_cleans_labels(conn):
    s = run(conn)
    assert (s.canonical_host, s.title_suffix) == ("www.example.edu", " | Example University")
    p = pages(conn)
    assert p["/"]["label"] == "A Great Place to Learn"
    assert p["/admissions/apply"]["label"] == "How to Apply"
    assert p["/about/history"]["label"] == "history"
    assert p["/campus-life"]["label"] == "Campus <Life>"
    assert p["/admissions/apply"]["title"] == "How to Apply | Example University"
    settings = conn.execute("SELECT canonical_host, title_suffix FROM settings").fetchone()
    assert tuple(settings) == ("www.example.edu", " | Example University")


def test_nav_flag_collections_and_tree_position(conn):
    run(conn)
    p = pages(conn)
    assert sorted(k for k, r in p.items() if r["in_current_nav"]) == ["/", "/admissions", "/news"]
    assert sorted(k for k, r in p.items() if r["collection"]) == ["/news/2024/story-one", "/news/story-two"]
    parent = {k: r["parent_id"] for k, r in p.items()}
    assert parent["/admissions/apply"] == p["/admissions"]["id"]
    assert parent["/admissions/apply/first-year"] == p["/admissions/apply"]["id"]
    assert parent["/news/2024/story-one"] == p["/news"]["id"]
    assert parent["/about/contact"] is None and parent["/orphan/deep/page"] is None


def test_error_pages_flagged_for_review_and_issues_recorded(conn):
    s = run(conn)
    p = pages(conn)
    assert p["/old-page"]["recommendation"] == "review"
    assert p["/old-page"]["notes"] == "HTTP 404 in crawl but still visited; likely needs a redirect"
    assert p["/gone"]["notes"] == "HTTP 404 in crawl"
    assert p["/restricted"]["status_code"] == 403
    assert p["/admissions"]["recommendation"] is None
    issues = {r["kind"]: r for r in conn.execute("SELECT * FROM site_issues")}
    assert issues["malformed_link"]["url"] == "https://www.example.edu/about/www.example.edu/visit"
    assert issues["duplicate_host"]["detail"].startswith("2 pages answer on more than one host")
    assert dict(s.site_issues) == {"malformed_link": 1, "duplicate_host": 1}


def test_existing_redirects_kept_with_targets(conn):
    run(conn)
    r = {x["source_url"]: x["target_url"] for x in conn.execute("SELECT * FROM existing_redirects")}
    assert r["https://www.example.edu/older"] == "http://www.example.edu/moved"
    assert r["https://www.example.edu/athletics"] == "https://www.examplesports.com/"
    assert len(r) == 5


def test_crawl_extra_keeps_unmapped_nonempty_columns_only(conn):
    run(conn)
    extra = json.loads(pages(conn)["/admissions"]["crawl_extra"])
    assert extra["Text Ratio"] == "6.1"
    assert not any(k.startswith("H2-") for k in extra)
    assert "Word Count" not in extra and "GA4 Sessions" not in extra


def test_reimport_updates_crawl_data_but_keeps_decisions(conn):
    run(conn)
    pid = pages(conn)["/admissions/apply"]["id"]
    conn.execute("UPDATE pages SET decision='combine', recommendation='keep', notes='merge into Admissions', "
                 "parent_id=NULL, redirect_to=(SELECT id FROM pages WHERE url_path='/admissions') WHERE id=?", (pid,))
    newer = edit_fixture(drop={"https://www.example.edu/about/history"},
                         change={"https://www.example.edu/admissions/apply": {"GA4 Sessions": "999"}})
    s = run(conn, newer)
    p = pages(conn)
    apply = p["/admissions/apply"]
    assert (s.pages_new, s.pages_updated, s.pages_missing) == (0, 14, 1)
    assert apply["id"] == pid and apply["sessions"] == 999
    assert (apply["decision"], apply["recommendation"], apply["notes"]) == ("combine", "keep", "merge into Admissions")
    assert apply["parent_id"] is None and apply["redirect_to"] == p["/admissions"]["id"]
    assert p["/about/history"]["missing_from_crawl"] == 1
    assert conn.execute("SELECT count(*) FROM imports").fetchone()[0] == 2


def test_rejects_files_that_are_not_crawl_exports(conn):
    with pytest.raises(importer.CrawlFormatError):
        run(conn, "Name,Email\nx,y\n")
    assert conn.execute("SELECT count(*) FROM imports").fetchone()[0] == 0


def test_failed_import_leaves_nothing_behind(conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(importer, "_place_new_pages", boom)
    with pytest.raises(RuntimeError):
        run(conn)
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM imports").fetchone()[0] == 0


@pytest.mark.parametrize("path,kind", [
    ("/about/www.example.edu/visit", "malformed_link"),
    ("/reslife/forms.gle/AbC123", "malformed_link"),
    ("/counseling/someone@example.edu", "malformed_link"),
    ("/policies/nondiscrimination.&nbsp", "malformed_link"),
    ('/policies/x/\\l%20"_30j0zll"%20\\h', "malformed_link"),
    ("/sites/default/files/form", "broken_file_link"),
    ("/media/brochure.PDF", "broken_file_link"),
    ("/library/index.html", None),
    ("/Academics/Program/Curriculum.aspx", None),
    ("/admission/admitted", None),
])
def test_error_url_classification(path, kind):
    assert importer._classify_error(path)[0] == kind


@pytest.mark.parametrize("url,key", [
    ("https://www.example.edu/a/b/", "/a/b"),
    ("http://example.edu/a/b", "/a/b"),
    ("https://www.example.edu/", "/"),
    ("https://www.example.edu/search?b=2&a=1", "/search?a=1&b=2"),
    ("https://admission.example.edu/visit", "//admission.example.edu/visit"),
    ("mailto:x@example.edu", None),
])
def test_page_key(url, key):
    assert importer.page_key(url, {"example.edu", "www.example.edu"}) == key
