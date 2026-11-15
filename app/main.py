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
from starlette.middleware.sessions import SessionMiddleware

import sitemap2proto as sp
from app import auth, config, exports, grid, importer, rules, tree
from app.db import DISPOSITIONS, SLUG_RE, Store, now_ms

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
PAGE_SIZE_MAX = 500

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
    preview_signer = URLSafeTimedSerializer(config.secret_key(store.data_dir), salt="prototype-preview")
    prototype_cache: Dict[str, tuple] = {}
    cache_lock = threading.Lock()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        preview = request.url.path.startswith("/preview/")
        response.headers["Content-Security-Policy"] = PREVIEW_CSP if preview else CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if preview else "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(NotLoggedIn)
    async def not_logged_in(request: Request, exc: NotLoggedIn):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "not logged in"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(FormExpired)
    async def form_expired(request: Request, exc: FormExpired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "This page has expired. Reload it and try again."}, status_code=403)
        if request.url.path == "/login":
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

    def row_dict(row) -> dict:
        d = dict(row)
        d["content_targets"] = json.loads(d["content_targets"] or "[]")
        return d

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
        return {"last_page": max(1, -(-total // size)), "last_row": total, "problem_count": problem_count,
                "data": [row_dict(r) for r in rows]}

    @app.patch("/api/p/{slug}/pages/{page_id}")
    def edit_page(request: Request, slug: str, page_id: int, edit: Edit):
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        with closing(open_project(slug)) as conn:
            try:
                _, warning = rules.apply_changes(conn, page_id, edit.changes, edit.updated_at)
            except KeyError:
                raise HTTPException(404, "That page no longer exists.")
            except rules.Conflict:
                return JSONResponse({"detail": "Someone else changed this row first. It now shows their version.",
                                     "row": row_dict(grid.one_row(conn, page_id))}, status_code=409)
            except rules.RuleError as e:
                return JSONResponse({"detail": str(e), "row": row_dict(grid.one_row(conn, page_id))},
                                    status_code=400)
            return {"row": row_dict(grid.one_row(conn, page_id)), "warning": warning}

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
            try:
                count, warning = rules.bulk_apply(conn, ids, bulk.changes)
            except rules.RuleError as e:
                return JSONResponse({"detail": str(e)}, status_code=400)
        return {"count": count, "warning": warning}

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
            roots = tree.load(conn)
        return {"roots": [r.to_dict() for r in roots]}

    def tree_write(request: Request, slug: str, action):
        require_user(request)
        check_csrf(request, request.headers.get("X-CSRF-Token"))
        with closing(open_project(slug)) as conn:
            try:
                return action(conn)
            except KeyError:
                raise HTTPException(404, "That page no longer exists.")
            except rules.RuleError as e:
                return JSONResponse({"detail": str(e)}, status_code=400)

    @app.post("/api/p/{slug}/tree/move")
    def tree_move(request: Request, slug: str, move: Move):
        return tree_write(request, slug, lambda conn: tree.move(conn, move.id, move.parent_id, move.index) or {"ok": True})

    @app.post("/api/p/{slug}/tree/pages")
    def tree_add(request: Request, slug: str, page: NewPage):
        return tree_write(request, slug, lambda conn: {"id": tree.create_page(conn, page.label, page.parent_id)})

    @app.delete("/api/p/{slug}/tree/pages/{page_id}")
    def tree_delete(request: Request, slug: str, page_id: int):
        return tree_write(request, slug, lambda conn: tree.delete_new_page(conn, page_id) or {"ok": True})

    # ---- prototype ---------------------------------------------------------

    def prototype_files(slug: str, conn) -> Dict[str, str]:
        current = tree.stamp(conn)
        with cache_lock:
            cached = prototype_cache.get(slug)
            if cached and cached[0] == current:
                return cached[1]
        files = tree.render_files(conn)
        with cache_lock:
            prototype_cache[slug] = (current, files)
        return files

    @app.get("/p/{slug}/prototype", response_class=HTMLResponse)
    def prototype_view(request: Request, slug: str, saved: str = "", error: str = ""):
        require_user(request)
        with closing(open_project(slug)) as conn:
            settings = tree.settings_row(conn)
            files = prototype_files(slug, conn)
            pages = sum(1 for name in files if name.endswith(".html")) - 1
            top_level = len(tree.load(conn))
        token = preview_signer.dumps(slug)
        return render(request, "prototype.html", slug=slug, project=project_name(slug), tab="prototype",
                      settings=settings, preview=f"/preview/{token}/index.html", pages=pages, top_level=top_level,
                      saved=bool(saved), error=error[:300])

    @app.post("/p/{slug}/prototype/settings")
    def prototype_settings(request: Request, slug: str, nav_style: str = Form("grid"),
                           menu: str = Form("flat"), dropdown_depth: int = Form(2),
                           sticky_header: str = Form(""), max_depth: int = Form(0),
                           version_label: str = Form("v1"), footer_md: str = Form(""),
                           utility_md: str = Form(""), csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        mega = menu == "mega"
        depth = max(1, min(dropdown_depth, 4)) if menu == "dropdown" else 0
        version = re.sub(r"[^A-Za-z0-9._-]", "", version_label)[:20] or "v1"
        try:
            sp.Settings(nav_style=nav_style, dropdown_depth=depth, mega_menu=mega)
        except ValueError as e:
            return RedirectResponse(f"/p/{slug}/prototype?error={quote(str(e))}", status_code=303)
        with closing(open_project(slug)) as conn:
            conn.execute(
                "UPDATE settings SET nav_style = ?, dropdown_depth = ?, mega_menu = ?, sticky_header = ?, "
                "max_depth = ?, version_label = ?, footer_md = ?, utility_md = ?",
                (nav_style, depth, int(mega), int(bool(sticky_header)), max(0, min(max_depth, 10)), version,
                 footer_md[:20000], utility_md[:20000]))
        return RedirectResponse(f"/p/{slug}/prototype?saved=1", status_code=303)

    @app.get("/p/{slug}/prototype/download")
    def prototype_download(request: Request, slug: str):
        require_user(request)
        with closing(open_project(slug)) as conn:
            files = prototype_files(slug, conn)
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
            files = prototype_files(slug, conn)
        path = path or "index.html"
        if path.endswith("/"):
            path += "index.html"
        if path not in files:
            raise HTTPException(404)
        kind = PREVIEW_TYPES.get(path.rsplit(".", 1)[-1], "text/plain")
        return Response(files[path].encode("utf-8"), media_type=f"{kind}; charset=utf-8")

    return app


def clean_view(definition: dict) -> dict:
    """Keep only known fields so a saved view can never carry anything else into a query."""
    filters = [{"field": f["field"], "value": str(f.get("value", ""))[:200]}
               for f in definition.get("filters", []) if isinstance(f, dict)
               and f.get("field") in grid.TEXT_FILTERS | grid.EXACT_FILTERS][:20]
    sorts = [{"column": s["column"], "dir": "desc" if s.get("dir") == "desc" else "asc"}
             for s in definition.get("sorts", []) if isinstance(s, dict) and s.get("column") in grid.FIELDS][:5]
    view = definition.get("view") if definition.get("view") in grid.PRESETS else ""
    return {"view": view, "filters": filters, "sorts": sorts}
