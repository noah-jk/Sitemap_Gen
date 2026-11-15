"""Web app: login, projects, crawl import, and the Pages grid.

Run with: uvicorn app.main:create_app --factory
"""
import csv
import hmac
import io
import json
import os
import re
import secrets
from contextlib import closing
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app import auth, config, importer
from app.db import DISPOSITIONS, SLUG_RE, Store, now_ms

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
PAGE_SIZE_MAX = 500

GRID_COLUMNS = ["id", "label", "url_path", "status_code", "sessions", "views", "inlinks", "in_current_nav",
                "collection", "recommendation", "decision", "status", "notes", "missing_from_crawl", "updated_at"]
TEXT_FILTERS = {"label", "url_path", "notes", "collection"}
EXACT_FILTERS = {"recommendation", "decision", "status", "status_code", "in_current_nav", "missing_from_crawl"}
EDITABLE = {"recommendation": DISPOSITIONS, "decision": DISPOSITIONS, "status": ("todo", "done"), "notes": None}
MAX_NOTES = 5000

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


class NotLoggedIn(Exception):
    pass


class Edit(BaseModel):
    field: str
    value: Optional[str]
    updated_at: int


def create_app(data_directory: Optional[str] = None, secure_cookies: Optional[bool] = None) -> FastAPI:
    store = Store(data_directory or config.data_dir())
    store.migrate_all()
    limiter = auth.LoginLimiter()
    templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware, secret_key=config.secret_key(store.data_dir), session_cookie="sitemap_session",
        max_age=14 * 24 * 3600, same_site="strict",
        https_only=config.secure_cookies() if secure_cookies is None else secure_cookies)
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(NotLoggedIn)
    async def not_logged_in(request: Request, exc: NotLoggedIn):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "not logged in"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    # ---- helpers -----------------------------------------------------------

    def csrf_token(request: Request) -> str:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        return request.session["csrf"]

    def check_csrf(request: Request, token: Optional[str]) -> None:
        expected = request.session.get("csrf")
        if not expected or not token or not hmac.compare_digest(expected, token):
            raise HTTPException(403, "invalid or missing CSRF token; reload the page and try again")

    def require_user(request: Request) -> int:
        uid = request.session.get("uid")
        if not uid or not auth.session_valid(store, uid, request.session.get("login_at", 0)):
            request.session.pop("uid", None)
            raise NotLoggedIn()
        return uid

    def open_project(slug: str):
        try:
            return store.project(slug)
        except (KeyError, ValueError):
            raise HTTPException(404, "no such project")

    def project_name(slug: str) -> str:
        return next((p["name"] for p in store.list_projects() if p["slug"] == slug), slug)

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        ctx.update(request=request, csrf=csrf_token(request), user=request.session.get("uid"))
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    # ---- auth --------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "login.html", error="")

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...), csrf: str = Form("")):
        check_csrf(request, csrf)
        client = request.client.host if request.client else "unknown"
        if limiter.blocked(client):
            return render(request, "login.html", status_code=429,
                          error="Too many failed attempts. Wait a few minutes and try again.")
        uid = auth.authenticate(store, username, password)
        if uid is None:
            limiter.failed(client)
            return render(request, "login.html", status_code=401, error="Wrong username or password.")
        limiter.reset(client)
        request.session.clear()
        request.session.update(uid=uid, login_at=now_ms(), csrf=secrets.token_urlsafe(32))
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ---- projects ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def projects(request: Request):
        require_user(request)
        return render(request, "projects.html", projects=store.list_projects(), error="", form={})

    @app.post("/projects")
    def create_project(request: Request, name: str = Form(...), slug: str = Form(...), csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        name, slug = name.strip(), slug.strip().lower()
        error = ""
        if not name or len(name) > 200:
            error = "Give the project a name (up to 200 characters)."
        elif not SLUG_RE.fullmatch(slug):
            error = "The short name can use lowercase letters, numbers, and hyphens (not at the start or end)."
        elif any(p["slug"] == slug for p in store.list_projects()):
            error = f"A project called {slug} already exists."
        if not error:
            try:
                store.create_project(slug, name)
            except FileExistsError:
                error = f"A database file for {slug} already exists in the data folder. Move it aside first."
        if error:
            return render(request, "projects.html", status_code=400, projects=store.list_projects(),
                          error=error, form={"name": name, "slug": slug})
        return RedirectResponse(f"/p/{slug}/import", status_code=303)

    @app.get("/p/{slug}")
    def project_home(request: Request, slug: str):
        require_user(request)
        return RedirectResponse(f"/p/{slug}/pages", status_code=303)

    # ---- import ------------------------------------------------------------

    def import_page(request: Request, slug: str, status_code: int = 200, error: str = ""):
        with closing(open_project(slug)) as conn:
            last = conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 1").fetchone()
            issues = conn.execute("SELECT kind, url, detail FROM site_issues ORDER BY kind, url").fetchall()
            settings = conn.execute("SELECT canonical_host, title_suffix FROM settings").fetchone()
        summary = json.loads(last["summary"]) if last else None
        return render(request, "import.html", status_code=status_code, slug=slug, project=project_name(slug),
                      tab="import", last=last, summary=summary, issues=issues, settings=settings, error=error)

    @app.get("/p/{slug}/import", response_class=HTMLResponse)
    def import_form(request: Request, slug: str):
        require_user(request)
        return import_page(request, slug)

    @app.post("/p/{slug}/import")
    def run_import(request: Request, slug: str, file: UploadFile = File(...),
                   ga_date_range: str = Form(""), csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        filename = os.path.basename(file.filename or "crawl.csv")[:200]
        if not filename.lower().endswith(".csv"):
            return import_page(request, slug, 400, "Upload the Screaming Frog export as a .csv file.")
        size = 0
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                return import_page(request, slug, 413, "That file is over 50 MB.")
        file.file.seek(0)
        text = io.TextIOWrapper(file.file, encoding="utf-8-sig", newline="")
        try:
            with closing(open_project(slug)) as conn:
                importer.import_crawl(conn, text, filename, ga_date_range.strip()[:100])
        except importer.CrawlFormatError as e:
            return import_page(request, slug, 400, str(e))
        except UnicodeDecodeError:
            return import_page(request, slug, 400, "The file isn't UTF-8 text. Export it from Screaming Frog as CSV.")
        except csv.Error as e:
            return import_page(request, slug, 400, f"Couldn't read the CSV: {e}")
        finally:
            text.detach()
        return RedirectResponse(f"/p/{slug}/import", status_code=303)

    # ---- pages grid --------------------------------------------------------

    @app.get("/p/{slug}/pages", response_class=HTMLResponse)
    def pages_view(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            count = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
        return render(request, "pages.html", slug=slug, project=project_name(slug), tab="pages",
                      count=count, dispositions=DISPOSITIONS)

    @app.get("/api/p/{slug}/pages")
    def pages_api(request: Request, slug: str, page: int = 1, size: int = 100):
        require_user(request)
        size = max(1, min(size, PAGE_SIZE_MAX))
        page = max(1, page)
        where, args = [], []
        for f in _bracket_params(request, "filter"):
            field, value = f.get("field"), f.get("value", "")
            if field in TEXT_FILTERS:
                where.append(f"{field} LIKE ? ESCAPE '\\'")
                args.append("%" + re.sub(r"([%_\\])", r"\\\1", value) + "%")
            elif field in EXACT_FILTERS:
                if value == "(blank)":
                    where.append(f"{field} IS NULL")
                else:
                    where.append(f"{field} = ?")
                    args.append(value)
        order = [f"{s['field']} {'DESC' if s.get('dir') == 'desc' else 'ASC'}"
                 for s in _bracket_params(request, "sort") if s.get("field") in GRID_COLUMNS]
        sql_where = f"WHERE {' AND '.join(where)}" if where else ""
        with closing(open_project(slug)) as conn:
            total = conn.execute(f"SELECT count(*) FROM pages {sql_where}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT {','.join(GRID_COLUMNS)} FROM pages {sql_where} "
                f"ORDER BY {', '.join(order + ['id'])} LIMIT ? OFFSET ?",
                args + [size, (page - 1) * size]).fetchall()
        return {"last_page": max(1, -(-total // size)), "last_row": total, "data": [dict(r) for r in rows]}

    @app.patch("/api/p/{slug}/pages/{page_id}")
    def edit_page(request: Request, slug: str, page_id: int, edit: Edit):
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        if edit.field not in EDITABLE:
            raise HTTPException(400, "that column can't be edited")
        allowed = EDITABLE[edit.field]
        if allowed is None:
            value = (edit.value or "")[:MAX_NOTES]
        else:
            value = edit.value or None
            if value is None and edit.field == "status":
                value = "todo"
            if value is not None and value not in allowed:
                raise HTTPException(400, f"{edit.field} must be one of: {', '.join(allowed)}")
        with closing(open_project(slug)) as conn:
            cur = conn.execute(
                f"UPDATE pages SET {edit.field} = ?, updated_at = ? WHERE id = ? AND updated_at = ?",
                (value, now_ms(), page_id, edit.updated_at))
            row = conn.execute(f"SELECT {','.join(GRID_COLUMNS)} FROM pages WHERE id = ?", (page_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such page")
        if cur.rowcount == 0:
            return JSONResponse({"detail": "changed by someone else", "row": dict(row)}, status_code=409)
        return dict(row)

    return app


def _bracket_params(request: Request, name: str) -> list:
    """Collect Tabulator's filter[0][field]=... style query parameters."""
    found = {}
    for key, value in request.query_params.multi_items():
        m = re.fullmatch(rf"{name}\[(\d{{1,3}})\]\[(field|type|value|dir)\]", key)
        if m:
            found.setdefault(int(m[1]), {})[m[2]] = value
    return [found[i] for i in sorted(found)]
