import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "crawl-example.csv"
PASSWORD = "correct horse battery"


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "data"), secure_cookies=False, warm_delay=None)
    from app.db import Store
    auth.create_user(Store(str(tmp_path / "data")), "noah", PASSWORD)
    with TestClient(app) as c:
        yield c


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html)[1]


def login(client, password=PASSWORD):
    token = csrf_from(client.get("/login").text)
    return client.post("/login", data={"username": "noah", "password": password, "csrf": token},
                       follow_redirects=False)


@pytest.fixture
def logged_in(client):
    assert login(client).status_code == 303
    return client


def make_project(client, slug="example", name="Example University"):
    token = csrf_from(client.get("/").text)
    return client.post("/projects", data={"name": name, "slug": slug, "csrf": token}, follow_redirects=False)


@pytest.fixture
def imported(logged_in):
    make_project(logged_in)
    token = csrf_from(logged_in.get("/p/example/import").text)
    r = logged_in.post("/p/example/import", data={"csrf": token, "ga_date_range": "last 12 months"},
                       files={"file": ("crawl.csv", FIXTURE.read_bytes(), "text/csv")}, follow_redirects=False)
    assert r.status_code == 303
    return logged_in


def grid_csrf(client):
    return re.search(r'data-csrf="([^"]+)"', client.get("/p/example/pages").text)[1]


# ---- auth ------------------------------------------------------------------

def test_pages_require_login(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/p/example/pages").status_code == 401


def test_login_and_logout(client):
    assert login(client, "wrong password!!").status_code == 401
    assert login(client).status_code == 303
    assert client.get("/").status_code == 200
    token = csrf_from(client.get("/").text)
    client.post("/logout", data={"csrf": token})
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_requires_csrf_token(client):
    client.get("/login")
    r = client.post("/login", data={"username": "noah", "password": PASSWORD, "csrf": "forged"})
    assert r.status_code == 403 and "login page had expired" in r.text
    assert login(client).status_code == 303  # the re-rendered form works


def test_repeated_failed_logins_are_blocked(client):
    for _ in range(5):
        assert login(client, "nope nope nope").status_code == 401
    assert login(client).status_code == 429


def test_password_hashing():
    h = auth.hash_password("a long enough password")
    assert h.startswith("scrypt$") and "a long enough" not in h
    assert auth.verify_password("a long enough password", h)
    assert not auth.verify_password("a long enough passworD", h)
    assert not auth.verify_password("x", "garbage")
    with pytest.raises(ValueError):
        auth.create_user(None, "x", "short")


def test_security_headers(client):
    r = client.get("/login")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


# ---- projects and import ---------------------------------------------------

def test_project_creation_validates_slug(logged_in):
    assert make_project(logged_in, slug="../etc").status_code == 400
    assert make_project(logged_in, slug="Has Spaces").status_code == 400
    assert make_project(logged_in).status_code == 303
    assert make_project(logged_in).status_code == 400  # duplicate
    assert "Example University" in logged_in.get("/").text


def test_unknown_project_is_404(logged_in):
    assert logged_in.get("/p/nope/pages").status_code == 404
    assert logged_in.get("/api/p/nope/pages").status_code == 404


def test_import_shows_summary_and_site_issues(imported):
    html = imported.get("/p/example/import").text
    assert "crawl.csv" in html and "last 12 months" in html
    assert "Link is missing https://" in html
    assert "www.example.edu" in html


def test_import_rejects_wrong_files(logged_in):
    make_project(logged_in)
    token = csrf_from(logged_in.get("/p/example/import").text)
    post = lambda name, body: logged_in.post("/p/example/import", data={"csrf": token},
                                             files={"file": (name, body, "text/csv")})
    assert post("notes.txt", b"x").status_code == 400
    r = post("people.csv", b"Name,Email\nx,y\n")
    assert r.status_code == 400 and "not a Screaming Frog export" in r.text
    assert post("latin1.csv", "Address,Content Type,Status Code\nhttps://x.edu/caf\xe9,text/html,200\n".encode("latin-1")).status_code == 400


def test_import_escapes_crawl_text(imported):
    html = imported.get("/p/example/import").text
    assert "<Life>" not in html


# ---- grid API --------------------------------------------------------------

def test_grid_pages_sort_and_filter(imported):
    r = imported.get("/api/p/example/pages", params={"size": 3, "sort[0][field]": "sessions", "sort[0][dir]": "desc"})
    body = r.json()
    assert body["last_row"] == 15
    assert [p["url_path"] for p in body["data"]] == ["/", "/admissions", "/admissions/apply"]
    assert body["data"][1]["current_url"].startswith("https://") and body["data"][1]["current_url"].endswith("/admissions")
    r = imported.get("/api/p/example/pages", params={"filter[0][field]": "label", "filter[0][value]": "contact"})
    assert r.json()["last_row"] == 2
    r = imported.get("/api/p/example/pages", params={"filter[0][field]": "recommendation", "filter[0][value]": "review"})
    assert r.json()["last_row"] == 3
    r = imported.get("/api/p/example/pages", params={"filter[0][field]": "recommendation", "filter[0][value]": "(blank)"})
    assert r.json()["last_row"] == 12


def test_grid_ignores_unknown_sort_and_filter_fields(imported):
    r = imported.get("/api/p/example/pages", params={
        "sort[0][field]": "id; DROP TABLE pages", "filter[0][field]": "1=1 --", "filter[0][value]": "x"})
    assert r.status_code == 200 and r.json()["last_row"] == 15
    r = imported.get("/api/p/example/pages", params={"filter[0][field]": "label", "filter[0][value]": "%"})
    assert r.json()["last_row"] == 0


def page_by_path(client, path):
    rows = client.get("/api/p/example/pages", params={"size": 500}).json()["data"]
    return next(r for r in rows if r["url_path"] == path)


def patcher(client, token):
    def patch(row, tok=None, **changes):
        return client.patch(f"/api/p/example/pages/{row['id']}",
                            json={"changes": changes, "updated_at": row["updated_at"]},
                            headers={"X-CSRF-Token": tok or token})
    return patch


def test_edit_saves_and_detects_conflicts(imported):
    patch = patcher(imported, grid_csrf(imported))
    row = page_by_path(imported, "/admissions/contact")
    r = patch(row, decision="keep")
    assert r.status_code == 200 and r.json()["row"]["decision"] == "keep"
    stale = patch(row, decision="delete")
    assert stale.status_code == 409 and stale.json()["row"]["decision"] == "keep"
    fresh = r.json()["row"]
    assert patch(fresh, decision="merge").status_code == 400
    assert patch(fresh, url_path="/renamed").status_code == 400
    assert patch(fresh, tok="forged", notes="x").status_code == 403
    cleared = patch(fresh, decision=None)
    assert cleared.status_code == 200 and cleared.json()["row"]["decision"] is None


def test_combine_needs_target_and_saves_with_one(imported):
    patch = patcher(imported, grid_csrf(imported))
    row = page_by_path(imported, "/admissions/contact")
    target = page_by_path(imported, "/about/contact")
    r = patch(row, decision="combine")
    assert r.status_code == 400 and "Needs a page to redirect to" in r.json()["detail"]
    r = patch(row, decision="combine", redirect_to=target["id"])
    assert r.status_code == 200
    assert r.json()["row"]["redirect_label"] == "Contact Us" and r.json()["row"]["redirect_path"] == "/about/contact"
    r = patch(target, decision="delete")
    assert r.status_code == 200 and "1 other page points here" in r.json()["warning"]
    problems = imported.get("/api/p/example/pages", params={"view": "problems"}).json()
    assert [p["url_path"] for p in problems["data"]] == ["/admissions/contact"]
    assert problems["problem_count"] == 1


def test_target_search_skips_removed_and_error_pages(imported):
    patch = patcher(imported, grid_csrf(imported))
    patch(page_by_path(imported, "/about/contact"), decision="delete")
    found = imported.get("/api/p/example/targets", params={"q": "contact"}).json()
    assert [f["url_path"] for f in found] == ["/admissions/contact"]
    found = imported.get("/api/p/example/targets", params={"q": "gone"}).json()
    assert found == []
    me = page_by_path(imported, "/admissions")
    found = imported.get("/api/p/example/targets", params={"q": "admissions", "exclude": me["id"]}).json()
    assert me["id"] not in [f["id"] for f in found]


def test_bulk_edit_selected_and_matching(imported):
    token = grid_csrf(imported)
    post = lambda body, tok=token: imported.post("/api/p/example/pages/bulk", json=body,
                                                 headers={"X-CSRF-Token": tok})
    news = [page_by_path(imported, p)["id"] for p in ("/news/2024/story-one", "/news/story-two")]
    r = post({"ids": news, "changes": {"recommendation": "delete"}})
    assert r.json()["count"] == 2
    r = post({"match": {"filters": [{"field": "collection", "value": "news"}]}, "changes": {"decision": "keep"}})
    assert r.json()["count"] == 2
    target = page_by_path(imported, "/news")["id"]
    r = post({"match": {"filters": [{"field": "url_path", "value": "/news"}]},
              "changes": {"decision": "combine", "redirect_to": target}})
    assert r.status_code == 400 and "in the selection" in r.json()["detail"]
    assert post({"ids": news, "changes": {"decision": "keep"}}, tok="forged").status_code == 403
    assert post({"ids": news, "changes": {"notes": "x"}}).status_code == 400


def test_presets_and_saved_views(imported):
    token = grid_csrf(imported)
    gone = imported.get("/api/p/example/pages", params={"view": "gone_visited"}).json()
    assert sorted(r["url_path"] for r in gone["data"]) == ["/old-page", "/restricted"]
    nav = imported.get("/api/p/example/pages", params={"view": "current_nav"}).json()
    assert nav["last_row"] == 3
    body = {"name": "Admissions", "definition": {
        "view": "no_traffic", "filters": [{"field": "url_path", "value": "/admissions"}, {"field": "evil", "value": "x"}],
        "sorts": [{"column": "sessions", "dir": "desc"}, {"column": "x; drop", "dir": "asc"}]}}
    saved = imported.post("/api/p/example/views", json=body, headers={"X-CSRF-Token": token}).json()
    assert saved["definition"] == {"view": "no_traffic", "filters": [{"field": "url_path", "value": "/admissions"}],
                                   "sorts": [{"column": "sessions", "dir": "desc"}]}
    listed = imported.get("/api/p/example/views").json()
    assert [v["name"] for v in listed["saved"]] == ["Admissions"]
    assert "problems" in [p["key"] for p in listed["presets"]]
    imported.delete(f"/api/p/example/views/{saved['id']}", headers={"X-CSRF-Token": token})
    assert imported.get("/api/p/example/views").json()["saved"] == []


def test_exports_page_and_downloads(imported):
    patch = patcher(imported, grid_csrf(imported))
    target = page_by_path(imported, "/about/contact")
    patch(page_by_path(imported, "/admissions/contact"), decision="combine", redirect_to=target["id"])
    html = imported.get("/p/example/exports").text
    assert "Download redirect map" in html
    r = imported.get("/p/example/exports/redirect-map.csv")
    assert r.headers["content-disposition"].startswith('attachment; filename="example-redirect-map-')
    assert "https://www.example.edu/admissions/contact" in r.text
    assert imported.get("/p/example/exports/content-audit.csv").text.startswith("\ufeffAddress,Recommendation")
    assert "- Contact Us" in imported.get("/p/example/exports/sitemap.md").text
    patch(page_by_path(imported, "/about/contact"), decision="delete")
    html = imported.get("/p/example/exports").text
    assert "can't be exported yet" in html
    r = imported.get("/p/example/exports/redirect-map.csv", follow_redirects=False)
    assert r.status_code == 303


def test_password_change_logs_out_old_sessions(logged_in, tmp_path):
    from app.db import Store
    assert logged_in.get("/", follow_redirects=False).status_code == 200
    auth.set_password(Store(str(tmp_path / "data")), "noah", "a brand new password")
    assert logged_in.get("/", follow_redirects=False).status_code == 303
    assert login(logged_in).status_code == 401
    assert login(logged_in, "a brand new password").status_code == 303


def test_user_management_errors(tmp_path):
    from app.db import Store
    store = Store(str(tmp_path / "d"))
    auth.create_user(store, "noah", PASSWORD)
    with pytest.raises(ValueError, match="already exists"):
        auth.create_user(store, "noah", PASSWORD)
    with pytest.raises(ValueError, match="no user"):
        auth.set_password(store, "ghost", PASSWORD)
    with pytest.raises(ValueError, match="12 characters"):
        auth.set_password(store, "noah", "short")
    assert auth.list_usernames(store) == ["noah"]


def test_expired_form_shows_a_page_not_json(logged_in):
    token = csrf_from(logged_in.get("/").text)
    r = logged_in.post("/projects", data={"name": "X", "slug": "x", "csrf": "stale"})
    assert r.status_code == 403 and "This page expired" in r.text
    assert make_project(logged_in, slug="x").status_code == 303


@pytest.mark.parametrize("env,secure", [("", False), ("1", True)])
def test_secure_cookie_flag_is_opt_in(tmp_path, monkeypatch, env, secure):
    monkeypatch.setenv("SITEMAP_SECURE_COOKIES", env)
    with TestClient(create_app(str(tmp_path / "data"))) as c:
        cookie = c.get("/login").headers["set-cookie"].lower()
    assert ("secure" in cookie) == secure


def test_tree_routes(imported):
    token = grid_csrf(imported)
    hdr = {"X-CSRF-Token": token}
    roots = imported.get("/api/p/example/tree").json()["roots"]
    admissions = next(r for r in roots if r["label"] == "Admissions")
    assert admissions["descendants"] == 3
    new = imported.post("/api/p/example/tree/pages", json={"label": "Student Life", "parent_id": None}, headers=hdr)
    new_id = new.json()["id"]
    r = imported.post("/api/p/example/tree/move", json={"id": admissions["id"], "parent_id": new_id, "index": 0},
                      headers=hdr)
    assert r.status_code == 200
    roots = imported.get("/api/p/example/tree").json()["roots"]
    life = next(r for r in roots if r["label"] == "Student Life")
    assert life["is_new"] and [c["label"] for c in life["children"]] == ["Admissions"] and life["descendants"] == 4
    loop = imported.post("/api/p/example/tree/move", json={"id": new_id, "parent_id": admissions["id"]}, headers=hdr)
    assert loop.status_code == 400 and "under itself" in loop.json()["detail"]
    assert imported.post("/api/p/example/tree/move", json={"id": new_id}, headers={"X-CSRF-Token": "x"}).status_code == 403
    assert imported.delete(f"/api/p/example/tree/pages/{admissions['id']}", headers=hdr).status_code == 400
    assert imported.delete(f"/api/p/example/tree/pages/{new_id}", headers=hdr).status_code == 200
    assert "<h1>Sitemap</h1>" in imported.get("/p/example/tree").text


def test_bulk_move_under_a_page(imported):
    token = grid_csrf(imported)
    hdr = {"X-CSRF-Token": token}
    section = imported.post("/api/p/example/tree/pages", json={"label": "About Us"}, headers=hdr).json()["id"]
    r = imported.post("/api/p/example/pages/bulk", headers=hdr, json={
        "match": {"filters": [{"field": "url_path", "value": "/about/"}]}, "changes": {"parent_id": section}})
    assert r.json()["count"] == 2
    rows = imported.get("/api/p/example/pages", params={"filter[0][field]": "parent_label", "filter[0][value]": "About Us"}).json()
    assert sorted(x["url_path"] for x in rows["data"]) == ["/about/contact", "/about/history"]
    bad = imported.post("/api/p/example/pages/bulk", headers=hdr, json={"ids": [section], "changes": {"parent_id": section}})
    assert bad.status_code == 400


def test_prototype_preview_settings_and_zip(imported, tmp_path):
    import io, zipfile
    html = imported.get("/p/example/prototype").text
    preview = re.search(r'<iframe data-src="([^"]+)"', html)[1]
    assert 'sandbox="allow-scripts"' in html
    with TestClient(imported.app) as anonymous:  # the preview needs no login, only the signed link
        r = anonymous.get(preview)
        assert r.status_code == 200
        assert anonymous.get(preview.replace("index.html", "admissions/index.html")).status_code == 200
        assert r.headers["content-security-policy"].startswith("sandbox allow-scripts")
        assert r.headers["x-frame-options"] == "SAMEORIGIN"
        css = anonymous.get(preview.replace("index.html", "style.css"))
        assert css.headers["content-type"].startswith("text/css")
        assert anonymous.get(preview.replace("index.html", "../../etc/passwd")).status_code == 404
        assert anonymous.get("/preview/forged-token/index.html").status_code == 404
    token = csrf_from(html)
    r = imported.post("/p/example/prototype/settings", data={
        "csrf": token, "menu": "mega", "nav_style": "sidebar", "max_depth": "1", "version_label": "v3 final!",
        "sticky_header": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("saved=1")
    hdr = {"X-CSRF-Token": grid_csrf(imported)}
    apply_id = page_by_path(imported, "/news")["id"]  # top level, so it is built even with depth 1
    imported.post("/api/p/example/nav/pages", json={"menu": "utility", "page_ids": [apply_id]}, headers=hdr)
    entry = imported.get("/api/p/example/nav").json()["menus"]["utility"][0]
    imported.patch(f"/api/p/example/nav/{entry['id']}", json={"label": "Apply now"}, headers=hdr)
    page = imported.get(preview).text
    assert "is-sticky" in page and "Apply now" in page and "Plus 3 more pages" in imported.get(
        preview.replace("index.html", "admissions/index.html")).text
    z = imported.get("/p/example/prototype/download")
    assert z.headers["content-disposition"] == 'attachment; filename="example-sitemap-v3final.zip"'
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert "robots.txt" in names and "index.html" in names and "sitemap.html" in names


def test_nav_routes(imported):
    hdr = {"X-CSRF-Token": grid_csrf(imported)}
    post = lambda path, body, h=hdr: imported.post(f"/api/p/example/nav/{path}", json=body, headers=h)
    assert "Navigation" in imported.get("/p/example/nav").text
    adm = page_by_path(imported, "/admissions")["id"]
    assert post("pages", {"menu": "main", "page_ids": [adm]}).json() == {"added": 1}
    r = post("pages", {"menu": "main", "match": {"filters": [{"field": "url_path", "value": "/admissions/"}]}})
    assert r.json() == {"added": 3}
    assert post("headings", {"menu": "footer", "label": "Help"}).status_code == 200
    menus = imported.get("/api/p/example/nav").json()["menus"]
    assert [i["display"] for i in menus["main"]][:1] == ["Admissions"] and len(menus["main"]) == 4
    first, second = menus["main"][0]["id"], menus["main"][1]["id"]
    assert post("move", {"id": second, "menu": "main", "parent_id": first, "index": 0}).status_code == 200
    bad = post("move", {"id": first, "menu": "main", "parent_id": second})
    assert bad.status_code == 400 and "inside itself" in bad.json()["detail"]
    assert imported.patch(f"/api/p/example/nav/{first}", json={"is_button": True}, headers=hdr).status_code == 200
    assert imported.delete(f"/api/p/example/nav/{first}", headers=hdr).status_code == 200
    assert post("headings", {"menu": "main", "label": "X"}, h={"X-CSRF-Token": "forged"}).status_code == 403
    assert post("pages", {"menu": "nope", "page_ids": [adm]}).status_code == 400


def test_menu_views_and_tree_tags(imported):
    hdr = {"X-CSRF-Token": grid_csrf(imported)}
    dropped = imported.get("/api/p/example/pages", params={"view": "nav_dropped"}).json()
    assert sorted(r["url_path"] for r in dropped["data"]) == ["/admissions", "/news"]
    adm = page_by_path(imported, "/admissions")["id"]
    imported.post("/api/p/example/nav/pages", json={"menu": "footer", "page_ids": [adm]}, headers=hdr)
    dropped = imported.get("/api/p/example/pages", params={"view": "nav_dropped"}).json()
    assert [r["url_path"] for r in dropped["data"]] == ["/news"]
    assert page_by_path(imported, "/admissions")["menus"] == "footer"
    roots = imported.get("/api/p/example/tree").json()["roots"]
    assert next(r for r in roots if r["id"] == adm)["menus"] == ["footer"]
    unplaced = imported.get("/api/p/example/pages", params={"view": "not_in_menu"}).json()
    paths = [r["url_path"] for r in unplaced["data"]]
    assert "/admissions" not in paths and "/" not in paths and "/gone" not in paths and "/news/story-two" not in paths


def test_nav_sub_pages_routes(imported):
    hdr = {"X-CSRF-Token": grid_csrf(imported)}
    adm = page_by_path(imported, "/admissions")["id"]
    r = imported.post("/api/p/example/nav/pages", json={"menu": "main", "page_ids": [adm], "subpage_depth": 9},
                      headers=hdr)
    assert r.json() == {"added": 4}                  # clamped to 3 levels; the sitemap only has 2 below
    entry = imported.get("/api/p/example/nav").json()["menus"]["main"][0]
    assert [c["display"] for c in entry["children"]] == ["Contact Us", "How to Apply"]
    r = imported.post(f"/api/p/example/nav/{entry['id']}/subpages", json={"depth": 1}, headers=hdr)
    assert r.json() == {"added": 0}
    bad = imported.post(f"/api/p/example/nav/{entry['id']}/subpages", json={"depth": 1}, headers={"X-CSRF-Token": "x"})
    assert bad.status_code == 403


def test_prototype_builds_in_background_once_per_burst(tmp_path, monkeypatch):
    import threading, time
    from app import tree as tree_mod
    from app.db import Store
    builds = []
    real = tree_mod.render_files
    monkeypatch.setattr(tree_mod, "render_files", lambda conn: builds.append(1) or real(conn))
    app = create_app(str(tmp_path / "data"), secure_cookies=False, warm_delay=0.2)
    auth.create_user(Store(str(tmp_path / "data")), "noah", PASSWORD)
    with TestClient(app) as c:
        login(c)
        make_project(c)
        token = csrf_from(c.get("/p/example/import").text)
        c.post("/p/example/import", data={"csrf": token},
               files={"file": ("crawl.csv", FIXTURE.read_bytes(), "text/csv")})
        hdr = {"X-CSRF-Token": grid_csrf(c)}
        for name in ("One", "Two", "Three"):          # a burst of edits
            c.post("/api/p/example/tree/pages", json={"label": name}, headers=hdr)
        html = c.get("/p/example/prototype").text      # opens without building
        assert builds == []
        deadline = time.time() + 5
        while not builds and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        assert len(builds) == 1                        # one background build for the whole burst
        preview = re.search(r'<iframe data-src="([^"]+)"', html)[1]
        results = []
        threads = [threading.Thread(target=lambda: results.append(c.get(preview).status_code)) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert results == [200, 200, 200] and len(builds) == 1   # served from the finished build


def test_page_types_in_grid_bulk_and_audit(imported):
    patch = patcher(imported, grid_csrf(imported))
    hdr = {"X-CSRF-Token": grid_csrf(imported)}
    campus = page_by_path(imported, "/campus-life")
    assert patch(campus, page_type="form").json()["row"]["page_type"] == "form"
    assert patch(page_by_path(imported, "/campus-life"), page_type="blog").status_code == 400
    auto = imported.get("/api/p/example/pages", params={"filter[0][field]": "page_type", "filter[0][value]": "(blank)"}).json()
    assert auto["last_row"] == 14
    r = imported.post("/api/p/example/pages/bulk", headers=hdr,
                      json={"match": {"filters": [{"field": "url_path", "value": "/admissions"}]}, "changes": {"page_type": "list"}})
    assert r.json()["count"] == 4
    audit = imported.get("/p/example/exports/content-audit.csv").text
    rows = {line.split(",")[0]: line for line in audit.splitlines()[1:]}
    assert ",list," in rows["https://www.example.edu/admissions"]
    assert ",form," in rows["https://www.example.edu/campus-life"]
    assert ",listing," in rows["https://www.example.edu/news"]          # auto, worked out from the sitemap
