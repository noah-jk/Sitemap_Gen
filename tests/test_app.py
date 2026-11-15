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
    app = create_app(str(tmp_path / "data"), secure_cookies=False)
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
    assert r.status_code == 403


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


def test_edit_saves_and_detects_conflicts(imported):
    token = grid_csrf(imported)
    row = imported.get("/api/p/example/pages", params={"size": 1}).json()["data"][0]
    patch = lambda field, value, ts, tok=token: imported.patch(
        f"/api/p/example/pages/{row['id']}", json={"field": field, "value": value, "updated_at": ts},
        headers={"X-CSRF-Token": tok})
    r = patch("decision", "keep", row["updated_at"])
    assert r.status_code == 200 and r.json()["decision"] == "keep"
    stale = patch("decision", "delete", row["updated_at"])
    assert stale.status_code == 409 and stale.json()["row"]["decision"] == "keep"
    assert patch("decision", "merge", r.json()["updated_at"]).status_code == 400
    assert patch("label", "renamed", r.json()["updated_at"]).status_code == 400
    assert patch("notes", "x", r.json()["updated_at"], tok="forged").status_code == 403
    cleared = patch("decision", None, r.json()["updated_at"])
    assert cleared.status_code == 200 and cleared.json()["decision"] is None
