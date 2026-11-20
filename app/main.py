"""Web app: login, projects, crawl import, the Pages grid, and exports.

Run with: uvicorn app.main:create_app --factory
"""
import csv
import hmac
import re
import io
import json
import os
import secrets
import threading
from collections import OrderedDict
from contextlib import closing
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

import sitemap2proto as sp
from app import auth, automation, config, dates, exports, grid, history, importer, nav, rules, tree, urls
from app.db import DISPOSITIONS, SLUG_RE, Store, now_ms

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
PAGE_SIZE_MAX = 500
MAX_BODY_BYTES = 1024 * 1024                       # every request except a crawl upload
UPLOAD_PATH = re.compile(r"/p/[a-z0-9-]+/import")  # room for the file plus the form around it

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
# Generated prototype pages carry their own small inline script and style. They run sandboxed
# (an opaque origin, no cookies, no access to the app) whether framed or opened in a tab.
PREVIEW_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
               "style-src 'self' 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; "
               "form-action 'none'; frame-ancestors 'self'")
PREVIEW_TTL = 12 * 3600
PREVIEW_TYPES = {"html": "text/html", "css": "text/css", "txt": "text/plain"}


class NotLoggedIn(Exception):
    pass


class FormExpired(Exception):
    pass


class Edit(BaseModel):
    changes: Dict[str, Any]
    updated_at: int


class Bulk(BaseModel):
    changes: Dict[str, Any]
    ids: Optional[List[int]] = None
    match: Optional[Dict[str, Any]] = None


class ViewIn(BaseModel):
    name: str
    definition: Dict[str, Any]


class Move(BaseModel):
    id: int
    parent_id: Optional[int] = None
    index: int = 0


class NewPage(BaseModel):
    label: str
    parent_id: Optional[int] = None


class NavAdd(BaseModel):
    menu: str
    parent_id: Optional[int] = None
    page_ids: List[int] = []
    match: Optional[Dict[str, Any]] = None
    subpage_depth: int = 0


class SubPages(BaseModel):
    depth: int = 1


class NavHeading(BaseModel):
    menu: str
    parent_id: Optional[int] = None
    label: str


class NavNewPage(BaseModel):
    menu: str
    parent_id: Optional[int] = None
    label: str


class RuleIn(BaseModel):
    name: str
    definition: Dict[str, Any]


class RulesRun(BaseModel):
    rules: List[RuleIn]


class RuleOrder(BaseModel):
    ids: List[int]


class DateSources(BaseModel):
    sources: List[str]


class NavMove(BaseModel):
    id: int
    menu: str
    parent_id: Optional[int] = None
    index: int = 0


class NavEdit(BaseModel):
    label: Optional[str] = None
    is_button: Optional[bool] = None


class BodyLimit:
    """Refuse request bodies over the limit before anything reads them: forms and uploads are parsed
    before an endpoint can check the login, so without this anyone could make the app spool a huge
    upload to disk. Checks Content-Length up front and counts the bytes as they arrive."""

    def __init__(self, app, limit: int = MAX_BODY_BYTES, upload_limit: int = MAX_UPLOAD_BYTES + MAX_BODY_BYTES):
        self.app, self.limit, self.upload_limit = app, limit, upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        upload = scope["method"] == "POST" and UPLOAD_PATH.fullmatch(scope["path"])
        limit = self.upload_limit if upload else self.limit
        for name, value in scope["headers"]:
            if name == b"content-length":
                if not value.isdigit() or int(value) > limit:
                    return await _too_large(send)
        received = 0

        async def counted():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise StarletteHTTPException(413, "Request body too large.")
            return message

        await self.app(scope, counted, send)


async def _too_large(send) -> None:
    body = b'{"detail":"Request body too large."}'
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class Prototype:
    """A project's prototype: the site structure, built once per change, and the most recently
    viewed preview pages. Pages render on request, so a big site never sits in memory whole."""
    KEEP = 200

    def __init__(self, stamp: str, site: sp.Site, settings: sp.Settings):
        self.stamp, self.site, self.settings = stamp, site, settings
        self.index = sp.file_index(site, settings)
        self.rendered: "OrderedDict[str, str]" = OrderedDict()
        self.lock = threading.Lock()

    def file(self, path: str) -> Optional[str]:
        with self.lock:
            if path in self.rendered:
                self.rendered.move_to_end(path)
                return self.rendered[path]
        content = sp.render_file(path, self.site, self.settings, self.index)  # outside the lock
        if content is not None:
            with self.lock:
                self.rendered[path] = content
                while len(self.rendered) > self.KEEP:
                    self.rendered.popitem(last=False)
        return content

    def all_files(self) -> Dict[str, str]:
        return sp.render_site(self.site, self.settings)


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
    app.add_middleware(BodyLimit)  # added last, so it runs first
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    preview_signer = URLSafeTimedSerializer(config.secret_key(store.data_dir), salt="prototype-preview")
    prototypes: Dict[str, Prototype] = {}
    cache_lock = threading.Lock()
    build_locks: Dict[str, threading.Lock] = {}

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.scope["path"]  # the routed path, not the URL rebuilt from the Host header
        preview = path.startswith("/preview/")
        response.headers["Content-Security-Policy"] = PREVIEW_CSP if preview else CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if preview else "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if not path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(NotLoggedIn)
    async def not_logged_in(request: Request, exc: NotLoggedIn):
        if request.scope["path"].startswith("/api/"):
            return JSONResponse({"detail": "not logged in"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(FormExpired)
    async def form_expired(request: Request, exc: FormExpired):
        if request.scope["path"].startswith("/api/"):
            return JSONResponse({"detail": "This page has expired. Reload it and try again."}, status_code=403)
        if request.scope["path"] == "/login":
            return render(request, "login.html", status_code=403,
                          error="The login page had expired. Enter your details again.")
        return render(request, "expired.html", status_code=403)

    # ---- helpers -----------------------------------------------------------

    def csrf_token(request: Request) -> str:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        return request.session["csrf"]

    def check_csrf(request: Request, token: Optional[str]) -> None:
        expected = request.session.get("csrf")
        if not expected or not token or not hmac.compare_digest(expected, token):
            raise FormExpired()

    def require_user(request: Request) -> int:
        uid = request.session.get("uid")
        if not uid or not auth.session_valid(store, uid, request.session.get("login_at", 0)):
            request.session.pop("uid", None)
            raise NotLoggedIn()
        return uid

    def user_name(request: Request) -> str:
        name = request.session.get("name")
        if not name:
            with closing(store.registry()) as reg:
                row = reg.execute("SELECT username FROM users WHERE id = ?", (request.session.get("uid"),)).fetchone()
            name = row["username"] if row else "someone"
            request.session["name"] = name
        return name

    def recorded(request: Request, conn, label: str):
        """Record the writes made in the block as one undoable action."""
        return history.record(conn, user_name(request), label[:300])

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
        request.session.update(uid=uid, name=username, login_at=now_ms(), csrf=secrets.token_urlsafe(32))
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

    # ---- exports -----------------------------------------------------------

    def download(content: str, slug: str, name: str, media_type: str) -> Response:
        stem, ext = name.rsplit(".", 1)
        filename = f"{slug}-{stem}-{date.today().isoformat()}.{ext}"
        return Response(content.encode("utf-8"), media_type=f"{media_type}; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/p/{slug}/exports", response_class=HTMLResponse)
    def exports_view(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            rmap = exports.redirect_map(conn)
            counts = conn.execute(
                "SELECT count(*) AS pages, sum(decision IS NOT NULL) AS decided, "
                "sum(recommendation IS NOT NULL) AS recommended FROM pages").fetchone()
        return render(request, "exports.html", slug=slug, project=project_name(slug), tab="exports",
                      rmap=rmap, counts=counts)

    @app.get("/p/{slug}/exports/redirect-map.csv")
    def export_redirects(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            rmap = exports.redirect_map(conn)
        if not rmap.ok:
            return RedirectResponse(f"/p/{slug}/exports", status_code=303)
        return download(rmap.csv(), slug, "redirect-map.csv", "text/csv")

    @app.get("/p/{slug}/exports/content-audit.csv")
    def export_audit(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            content = exports.content_audit_csv(conn)
        return download(content, slug, "content-audit.csv", "text/csv")

    @app.get("/p/{slug}/exports/sitemap.md")
    def export_markdown(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            content = exports.markdown_sitemap(conn, project_name(slug), date.today().isoformat())
        return download(content, slug, "sitemap.md", "text/markdown")

    # ---- pages grid --------------------------------------------------------

    def row_dict(row, planned: Optional[Dict[int, str]] = None) -> dict:
        d = dict(row)
        d["content_targets"] = json.loads(d["content_targets"] or "[]")
        if planned is not None:
            d["planned_url"] = planned.get(d["id"], "")
        return d

    def one(conn, page_id: int) -> dict:
        """A grid row, with its planned URL."""
        return row_dict(grid.one_row(conn, page_id), urls.planned(conn))

    @app.get("/p/{slug}/pages", response_class=HTMLResponse)
    def pages_view(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            count = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
        return render(request, "pages.html", slug=slug, project=project_name(slug), tab="pages",
                      count=count, dispositions=DISPOSITIONS)

    @app.get("/api/p/{slug}/pages")
    def pages_api(request: Request, slug: str, page: int = 1, size: int = 100, view: str = ""):
        require_user(request)
        size = max(1, min(size, PAGE_SIZE_MAX))
        items = request.query_params.multi_items()
        with closing(open_project(slug)) as conn:
            total, rows = grid.page_rows(conn, grid.bracket_params(items, "filter"),
                                         grid.bracket_params(items, "sort"), view, max(1, page), size)
            problem_count = len(rules.problems(conn))
            planned = urls.planned(conn)
        return {"last_page": max(1, -(-total // size)), "last_row": total, "problem_count": problem_count,
                "data": [row_dict(r, planned) for r in rows]}

    @app.patch("/api/p/{slug}/pages/{page_id}")
    def edit_page(request: Request, slug: str, page_id: int, edit: Edit):
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        with closing(open_project(slug)) as conn:
            try:
                with recorded(request, conn, history.describe_change(conn, page_id, edit.changes)):
                    _, warning = rules.apply_changes(conn, page_id, edit.changes, edit.updated_at)
            except KeyError:
                raise HTTPException(404, "That page no longer exists.")
            except rules.Conflict:
                return JSONResponse({"detail": "Someone else changed this row first. It now shows their version.",
                                     "row": one(conn, page_id)}, status_code=409)
            except rules.RuleError as e:
                return JSONResponse({"detail": str(e), "row": one(conn, page_id)},
                                    status_code=400)
            return {"row": one(conn, page_id), "warning": warning}

    @app.post("/api/p/{slug}/pages/bulk")
    def bulk_edit(request: Request, slug: str, bulk: Bulk):
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        with closing(open_project(slug)) as conn:
            if bulk.match is not None:
                filters = [f for f in bulk.match.get("filters", []) if isinstance(f, dict)]
                ids = grid.matching_ids(conn, filters, bulk.match.get("view"))
            else:
                ids = bulk.ids or []
            label = history.describe_bulk(conn, bulk.changes)
            try:
                with recorded(request, conn, label.replace("{count}", "pages")) as rec:
                    count, warning = rules.bulk_apply(conn, ids, bulk.changes)
                    rec.label = label.replace("{count}", f"{count} page{'' if count == 1 else 's'}")
            except rules.RuleError as e:
                return JSONResponse({"detail": str(e)}, status_code=400)
        return {"count": count, "warning": warning}

    @app.get("/api/p/{slug}/pages/{page_id}/detail")
    def page_detail(request: Request, slug: str, page_id: int):
        require_user(request)
        with closing(open_project(slug)) as conn:
            detail = grid.page_detail(conn, page_id)
            if detail is None:
                raise HTTPException(404, "That page no longer exists.")
            detail["row"] = one(conn, page_id)
            detail["planned_url"] = detail["row"]["planned_url"]
        return detail

    @app.get("/api/p/{slug}/pages/{page_id}/content-targets")
    def content_targets(request: Request, slug: str, page_id: int):
        require_user(request)
        with closing(open_project(slug)) as conn:
            rows = conn.execute(
                "SELECT p.id, p.label, p.url_path FROM page_content_targets c JOIN pages p ON p.id = c.target_id "
                "WHERE c.page_id = ? ORDER BY p.label", (page_id,)).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/p/{slug}/targets")
    def targets(request: Request, slug: str, q: str = "", exclude: Optional[int] = None):
        require_user(request)
        with closing(open_project(slug)) as conn:
            rows = grid.search_targets(conn, q[:200], exclude)
        return [dict(r) for r in rows]

    @app.get("/api/p/{slug}/views")
    def list_views(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            saved = [{"id": r["id"], "name": r["name"], "definition": json.loads(r["definition"])}
                     for r in conn.execute("SELECT id, name, definition FROM views ORDER BY name")]
        return {"presets": [{"key": k, "title": t} for k, (t, _) in grid.PRESETS.items()], "saved": saved}

    @app.post("/api/p/{slug}/views")
    def save_view(request: Request, slug: str, view: ViewIn):
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        name = view.name.strip()[:100]
        if not name:
            return JSONResponse({"detail": "Give the view a name."}, status_code=400)
        definition = clean_view(view.definition)
        with closing(open_project(slug)) as conn:
            conn.execute("INSERT INTO views(name, definition) VALUES (?, ?) ON CONFLICT(name) "
                         "DO UPDATE SET definition = excluded.definition", (name, json.dumps(definition)))
            view_id = conn.execute("SELECT id FROM views WHERE name = ?", (name,)).fetchone()[0]
        return {"id": view_id, "name": name, "definition": definition}

    @app.delete("/api/p/{slug}/views/{view_id}")
    def delete_view(request: Request, slug: str, view_id: int):
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        with closing(open_project(slug)) as conn:
            conn.execute("DELETE FROM views WHERE id = ?", (view_id,))
        return {"ok": True}

    # ---- tree --------------------------------------------------------------

    @app.get("/p/{slug}/tree", response_class=HTMLResponse)
    def tree_view(request: Request, slug: str):
        require_user(request)
        open_project(slug).close()
        return render(request, "tree.html", slug=slug, project=project_name(slug), tab="tree")

    @app.get("/api/p/{slug}/tree")
    def tree_api(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            roots = [r.to_dict() for r in tree.load(conn)]
            menus = nav.page_menus(conn)

        def tag(nodes):
            for n in nodes:
                n["menus"] = menus.get(n["id"], [])
                tag(n["children"])
        tag(roots)
        return {"roots": roots}

    def tree_write(request: Request, slug: str, action, describe):
        """`describe(conn)` names the edit before it happens; "{added}" in it becomes the count added."""
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        with closing(open_project(slug)) as conn:
            try:
                label = describe(conn)
                with recorded(request, conn, label.replace("{added}", "entries")) as rec:
                    result = action(conn)
                    if isinstance(result, dict) and "added" in result:
                        n = result["added"]
                        rec.label = label.replace("{added}", f"{n} entr{'y' if n == 1 else 'ies'}")
                return result
            except KeyError:
                raise HTTPException(404, "That page no longer exists.")
            except rules.RuleError as e:
                return JSONResponse({"detail": str(e)}, status_code=400)

    @app.post("/api/p/{slug}/tree/move")
    def tree_move(request: Request, slug: str, move: Move):
        return tree_write(request, slug, lambda conn: tree.move(conn, move.id, move.parent_id, move.index) or {"ok": True},
                          lambda conn: f'Moved "{history.page_label(conn, move.id)}"')

    @app.post("/api/p/{slug}/tree/pages")
    def tree_add(request: Request, slug: str, page: NewPage):
        return tree_write(request, slug, lambda conn: {"id": tree.create_page(conn, page.label, page.parent_id)},
                          lambda conn: f'Added page "{" ".join(page.label.split())}"')

    @app.delete("/api/p/{slug}/tree/pages/{page_id}")
    def tree_delete(request: Request, slug: str, page_id: int):
        return tree_write(request, slug, lambda conn: tree.delete_new_page(conn, page_id) or {"ok": True},
                          lambda conn: f'Deleted page "{history.page_label(conn, page_id)}"')

    # ---- navigation (menus) ------------------------------------------------

    @app.get("/p/{slug}/nav", response_class=HTMLResponse)
    def nav_view(request: Request, slug: str):
        require_user(request)
        open_project(slug).close()
        return render(request, "nav.html", slug=slug, project=project_name(slug), tab="nav",
                      menus=[(m, nav.MENU_NAMES[m], nav.MAX_LEVELS[m]) for m in nav.MENUS])

    @app.get("/api/p/{slug}/nav")
    def nav_api(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            return {"menus": nav.load(conn)}

    @app.post("/api/p/{slug}/nav/pages")
    def nav_add_pages(request: Request, slug: str, body: NavAdd):
        def run(conn):
            ids = body.page_ids
            if body.match is not None:
                filters = [f for f in body.match.get("filters", []) if isinstance(f, dict)]
                ids = grid.matching_ids(conn, filters, body.match.get("view"))
            if not ids:
                raise rules.RuleError(["No pages chosen."])
            depth = max(0, min(body.subpage_depth, 3))
            return {"added": nav.add_pages(conn, body.menu, body.parent_id, ids, depth)}
        return tree_write(request, slug, run,
                          lambda conn: f"Added {{added}} to the {history.MENU_NAMES.get(body.menu, 'menu')}")

    @app.post("/api/p/{slug}/nav/{item_id}/subpages")
    def nav_subpages(request: Request, slug: str, item_id: int, body: SubPages):
        depth = max(1, min(body.depth, 3))
        return tree_write(request, slug, lambda conn: {"added": nav.add_subpages(conn, item_id, depth)},
                          lambda conn: f'Added {{added}} under "{history.entry_label(conn, item_id)[0]}"')

    @app.post("/api/p/{slug}/nav/headings")
    def nav_add_heading(request: Request, slug: str, body: NavHeading):
        return tree_write(request, slug, lambda conn: {"id": nav.add_heading(conn, body.menu, body.parent_id, body.label)},
                          lambda conn: f'Added heading "{" ".join(body.label.split())}" to the '
                                       f'{history.MENU_NAMES.get(body.menu, "menu")}')

    @app.post("/api/p/{slug}/nav/new-page")
    def nav_new_page(request: Request, slug: str, body: NavNewPage):
        return tree_write(request, slug, lambda conn: nav.add_new_page(conn, body.menu, body.parent_id, body.label),
                          lambda conn: f'Added new page "{" ".join(body.label.split())}" to the '
                                       f'{history.MENU_NAMES.get(body.menu, "menu")}')

    @app.post("/api/p/{slug}/nav/move")
    def nav_move(request: Request, slug: str, body: NavMove):
        return tree_write(request, slug,
                          lambda conn: nav.move(conn, body.id, body.menu, body.parent_id, body.index) or {"ok": True},
                          lambda conn: 'Moved "{}" in the {}'.format(*history.entry_label(conn, body.id)))

    @app.patch("/api/p/{slug}/nav/{item_id}")
    def nav_edit(request: Request, slug: str, item_id: int, body: NavEdit):
        return tree_write(request, slug,
                          lambda conn: nav.update(conn, item_id, body.label, body.is_button) or {"ok": True},
                          lambda conn: describe_entry_edit(conn, item_id, body))

    @app.delete("/api/p/{slug}/nav/{item_id}")
    def nav_remove(request: Request, slug: str, item_id: int, keep_children: bool = False):
        def describe(conn) -> str:
            name, menu = history.entry_label(conn, item_id)
            below = 0 if keep_children else nav.count_below(conn, item_id)
            under = f" and {below} entr{'y' if below == 1 else 'ies'} under it" if below else ""
            kept = " (its sub-entries stay)" if keep_children and nav.count_below(conn, item_id) else ""
            return f'Removed "{name}"{under} from the {menu}{kept}'
        return tree_write(request, slug, lambda conn: {"removed": nav.remove(conn, item_id, keep_children)}, describe)

    def describe_entry_edit(conn, item_id: int, body) -> str:
        name, menu = history.entry_label(conn, item_id)
        if body.label is not None:
            new = " ".join(body.label.split())
            return f'Renamed "{name}" in the {menu} to "{new}"' if new else f'Reset the name of "{name}" in the {menu}'
        return f'Made "{name}" a button' if body.is_button else f'Made "{name}" a plain link'

    # ---- history (undo / redo) -----------------------------------------------

    @app.get("/api/p/{slug}/history")
    def history_api(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            return history.recent(conn)

    def history_step(request: Request, slug: str, step):
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        with closing(open_project(slug)) as conn:
            try:
                label = step(conn)
            except history.NothingToDo as e:
                return JSONResponse({"detail": str(e)}, status_code=400)
            except history.Stale as e:
                return JSONResponse({"detail": str(e)}, status_code=409)
            return {"label": label, **history.recent(conn)}

    @app.post("/api/p/{slug}/history/undo")
    def history_undo(request: Request, slug: str):
        return history_step(request, slug, history.undo)

    @app.post("/api/p/{slug}/history/redo")
    def history_redo(request: Request, slug: str):
        return history_step(request, slug, history.redo)

    # ---- rules (if-then bulk updates) ----------------------------------------
    # The library belongs to the person (registry); running rules happens in a project.

    def clean_rule(rule: RuleIn) -> tuple:
        name = " ".join(rule.name.split())[:100]
        if not name:
            raise automation.RuleDefinitionError("Give the rule a name.")
        return name, automation.clean_definition(rule.definition)

    def rule_error(e: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.get("/p/{slug}/rules", response_class=HTMLResponse)
    def rules_view(request: Request, slug: str):
        require_user(request)
        open_project(slug).close()
        return render(request, "rules.html", slug=slug, project=project_name(slug), tab="rules")

    @app.get("/api/rules")
    def rules_list(request: Request):
        uid = require_user(request)
        with closing(store.registry()) as reg:
            rows = reg.execute("SELECT id, name, definition FROM rules WHERE user_id = ? ORDER BY sort_order, id",
                               (uid,)).fetchall()
        return {"rules": [{"id": r["id"], "name": r["name"], "definition": json.loads(r["definition"])} for r in rows],
                "schema": automation.schema()}

    @app.post("/api/rules")
    def rules_create(request: Request, rule: RuleIn):
        uid = require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        try:
            name, definition = clean_rule(rule)
        except automation.RuleDefinitionError as e:
            return rule_error(e)
        with closing(store.registry()) as reg:
            if reg.execute("SELECT count(*) FROM rules WHERE user_id = ?", (uid,)).fetchone()[0] >= 200:
                return rule_error(ValueError("That's 200 rules already. Delete some first."))
            last = reg.execute("SELECT COALESCE(max(sort_order), 0) FROM rules WHERE user_id = ?", (uid,)).fetchone()[0]
            rule_id = reg.execute(
                "INSERT INTO rules(user_id, name, definition, sort_order, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (uid, name, json.dumps(definition), last + 10, now_ms(), now_ms())).lastrowid
        return {"id": rule_id, "name": name, "definition": definition}

    @app.put("/api/rules/{rule_id}")
    def rules_update(request: Request, rule_id: int, rule: RuleIn):
        uid = require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        try:
            name, definition = clean_rule(rule)
        except automation.RuleDefinitionError as e:
            return rule_error(e)
        with closing(store.registry()) as reg:
            cur = reg.execute("UPDATE rules SET name = ?, definition = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                              (name, json.dumps(definition), now_ms(), rule_id, uid))
        if not cur.rowcount:
            raise HTTPException(404, "No such rule.")
        return {"id": rule_id, "name": name, "definition": definition}

    @app.delete("/api/rules/{rule_id}")
    def rules_delete(request: Request, rule_id: int):
        uid = require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        with closing(store.registry()) as reg:
            reg.execute("DELETE FROM rules WHERE id = ? AND user_id = ?", (rule_id, uid))
        return {"ok": True}

    @app.post("/api/rules/order")
    def rules_order(request: Request, order: RuleOrder):
        uid = require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        with closing(store.registry()) as reg:
            reg.executemany("UPDATE rules SET sort_order = ? WHERE id = ? AND user_id = ?",
                            [((i + 1) * 10, rule_id, uid) for i, rule_id in enumerate(order.ids[:500])])
        return {"ok": True}

    @app.get("/api/p/{slug}/rules/context")
    def rules_context(request: Request, slug: str):
        """What this project offers rules: its crawl columns and where page dates come from."""
        require_user(request)
        with closing(open_project(slug)) as conn:
            return {"crawl_columns": dates.crawl_columns(conn), "date_sources": dates.sources(conn),
                    "source_names": dates.SOURCES, "coverage": dates.coverage(conn)}

    @app.post("/api/p/{slug}/dates")
    def date_sources_save(request: Request, slug: str, body: DateSources):
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        try:
            chosen = dates.clean_sources(body.sources)
        except ValueError as e:
            return rule_error(e)
        with closing(open_project(slug)) as conn, recorded(request, conn, "Changed where page dates come from"):
            conn.execute("UPDATE settings SET date_sources = ?", (json.dumps(chosen),))
        with closing(open_project(slug)) as conn:
            return {"date_sources": chosen, "coverage": dates.coverage(conn)}

    def named_rules(body: RulesRun) -> list:
        if not body.rules:
            raise automation.RuleDefinitionError("Choose at least one rule.")
        if len(body.rules) > 50:
            raise automation.RuleDefinitionError("Up to 50 rules at a time.")
        return [clean_rule(r) for r in body.rules]

    @app.post("/api/p/{slug}/rules/preview")
    def rules_preview(request: Request, slug: str, body: RulesRun):
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        try:
            named = named_rules(body)
        except automation.RuleDefinitionError as e:
            return rule_error(e)
        with closing(open_project(slug)) as conn:
            return {"rules": automation.report(conn, automation.run(conn, named, write=False))}

    @app.post("/api/p/{slug}/rules/apply")
    def rules_apply(request: Request, slug: str, body: RulesRun):
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        try:
            named = named_rules(body)
        except automation.RuleDefinitionError as e:
            return rule_error(e)
        names = ", ".join(n for n, _ in named)
        with closing(open_project(slug)) as conn:
            with recorded(request, conn, f"Ran rules: {names}") as rec:
                outcomes = automation.run(conn, named, write=True)
                changed = len({p for o in outcomes for p in o.changed})
                rec.label = (f"Ran {len(named)} rule{'s' if len(named) != 1 else ''} ({names}): "
                             f"changed {changed} page{'s' if changed != 1 else ''}")
            return {"changed": changed, "rules": automation.report(conn, outcomes)}

    # ---- prototype ---------------------------------------------------------

    def prototype(slug: str, conn) -> Prototype:
        """The project's prototype, rebuilt only when something it shows has changed. One build per
        project at a time: a request that arrives mid-build waits for it instead of starting another.
        Building is just the site structure (tens of milliseconds); pages render when asked for."""
        with cache_lock:
            lock = build_locks.setdefault(slug, threading.Lock())
        with lock:
            current = tree.stamp(conn)
            cached = prototypes.get(slug)
            if cached and cached.stamp == current:
                return cached
            site, settings = tree.build_site(conn)
            prototypes[slug] = Prototype(current, site, settings)
            return prototypes[slug]

    @app.get("/p/{slug}/prototype", response_class=HTMLResponse)
    def prototype_view(request: Request, slug: str, saved: str = "", error: str = ""):
        require_user(request)
        with closing(open_project(slug)) as conn:
            settings = tree.settings_row(conn)
            pages = sum(1 for _ in tree.walk(tree.load(conn)))
            menu_counts = {m: len(items) for m, items in nav.load(conn).items()}
        token = preview_signer.dumps(slug)
        return render(request, "prototype.html", slug=slug, project=project_name(slug), tab="prototype",
                      settings=settings, preview=f"/preview/{token}/index.html", pages=pages, menu_counts=menu_counts,
                      saved=bool(saved), error=error[:300])

    @app.post("/p/{slug}/prototype/settings")
    def prototype_settings(request: Request, slug: str, nav_style: str = Form("grid"),
                           menu: str = Form("flat"), dropdown_depth: int = Form(2),
                           sticky_header: str = Form(""), max_depth: int = Form(0),
                           version_label: str = Form("v1"), csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        as_json = request.headers.get("X-Requested-With") == "fetch"  # the page saves each change as it's made
        mega = menu == "mega"
        depth = max(1, min(dropdown_depth, 4)) if menu == "dropdown" else 0
        version = re.sub(r"[^A-Za-z0-9._-]", "", version_label)[:20] or "v1"
        try:
            sp.Settings(nav_style=nav_style, dropdown_depth=depth, mega_menu=mega)
        except ValueError as e:
            if as_json:
                return JSONResponse({"detail": str(e)}, status_code=400)
            return RedirectResponse(f"/p/{slug}/prototype?error={quote(str(e))}", status_code=303)
        with closing(open_project(slug)) as conn, recorded(request, conn, "Changed the prototype settings"):
            conn.execute(
                "UPDATE settings SET nav_style = ?, dropdown_depth = ?, mega_menu = ?, sticky_header = ?, "
                "max_depth = ?, version_label = ?",
                (nav_style, depth, int(mega), int(bool(sticky_header)), max(0, min(max_depth, 10)), version))
        if as_json:
            return {"ok": True, "version_label": version}
        return RedirectResponse(f"/p/{slug}/prototype?saved=1", status_code=303)

    @app.get("/p/{slug}/prototype/download")
    def prototype_download(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            files = prototype(slug, conn).all_files()  # the whole site, only when downloading
            version = tree.settings_row(conn)["version_label"]
        filename = f"{slug}-sitemap-{version}.zip"
        return Response(sp.write_zip(files), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/preview/{token}/{path:path}")
    def preview(token: str, path: str):
        try:
            slug = preview_signer.loads(token, max_age=PREVIEW_TTL)
        except BadSignature:
            raise HTTPException(404)
        with closing(open_project(slug)) as conn:
            proto = prototype(slug, conn)
        path = path or "index.html"
        if path.endswith("/"):
            path += "index.html"
        content = proto.file(path)
        if content is None:
            raise HTTPException(404)
        kind = PREVIEW_TYPES.get(path.rsplit(".", 1)[-1], "text/plain")
        return Response(content.encode("utf-8"), media_type=f"{kind}; charset=utf-8")

    return app


def clean_view(definition: dict) -> dict:
    """Keep only known fields so a saved view can never carry anything else into a query."""
    filters = [{"field": f["field"], "value": str(f.get("value", ""))[:200]}
               for f in definition.get("filters", []) if isinstance(f, dict)
               and f.get("field") in grid.TEXT_FILTERS | grid.EXACT_FILTERS][:20]
    sorts = [{"column": s["column"], "dir": "desc" if s.get("dir") == "desc" else "asc"}
             for s in definition.get("sorts", []) if isinstance(s, dict) and s.get("column") in grid.FIELDS][:5]
    view = definition.get("view") if definition.get("view") in grid.PRESETS else ""
    out = {"view": view, "filters": filters, "sorts": sorts}
    if isinstance(definition.get("hidden"), list):  # columns hidden in this view (older views don't say)
        out["hidden"] = sorted({c for c in definition["hidden"] if c in grid.HIDEABLE})
    return out
