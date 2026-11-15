"""SQLite storage: a registry database plus one database file per client project."""
import os
import re
import sqlite3
import time
from contextlib import closing
from typing import Iterator, List

DISPOSITIONS = ("keep", "delete", "review", "combine", "split")
_DISP = "(" + ",".join(f"'{d}'" for d in DISPOSITIONS) + ")"
SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")

# Append only. Each entry runs once per database, tracked by PRAGMA user_version.
REGISTRY_MIGRATIONS: List[str] = [
    """
    CREATE TABLE users(
      id INTEGER PRIMARY KEY,
      username TEXT NOT NULL UNIQUE COLLATE NOCASE,
      password_hash TEXT NOT NULL,
      created_at INTEGER NOT NULL
    );
    CREATE TABLE projects(
      id INTEGER PRIMARY KEY,
      slug TEXT NOT NULL UNIQUE,
      name TEXT NOT NULL,
      created_at INTEGER NOT NULL
    );
    """,
    """
    ALTER TABLE users ADD COLUMN password_changed_at INTEGER NOT NULL DEFAULT 0;
    """,
]

PROJECT_MIGRATIONS: List[str] = [
    f"""
    CREATE TABLE imports(
      id INTEGER PRIMARY KEY,
      filename TEXT NOT NULL,
      crawl_timestamp TEXT,
      ga_date_range TEXT,
      imported_at INTEGER NOT NULL,
      summary TEXT NOT NULL DEFAULT '{{}}'
    );
    CREATE TABLE pages(
      id INTEGER PRIMARY KEY,
      url_path TEXT UNIQUE,
      current_url TEXT,
      is_new INTEGER NOT NULL DEFAULT 0,
      label TEXT NOT NULL,
      title TEXT NOT NULL DEFAULT '',
      h1 TEXT NOT NULL DEFAULT '',
      meta_description TEXT NOT NULL DEFAULT '',
      canonical TEXT NOT NULL DEFAULT '',
      status_code INTEGER,
      crawl_depth INTEGER,
      inlinks INTEGER,
      unique_inlinks INTEGER,
      pct_of_total REAL,
      hash TEXT NOT NULL DEFAULT '',
      indexability TEXT NOT NULL DEFAULT '',
      word_count INTEGER,
      near_duplicates INTEGER,
      closest_near_duplicate TEXT NOT NULL DEFAULT '',
      in_current_nav INTEGER NOT NULL DEFAULT 0,
      sessions INTEGER NOT NULL DEFAULT 0,
      views INTEGER NOT NULL DEFAULT 0,
      engaged_sessions INTEGER NOT NULL DEFAULT 0,
      engagement_rate REAL,
      key_events INTEGER NOT NULL DEFAULT 0,
      recommendation TEXT CHECK(recommendation IN {_DISP}),
      decision TEXT CHECK(decision IN {_DISP}),
      redirect_to INTEGER REFERENCES pages(id) ON DELETE SET NULL,
      status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','done')),
      notes TEXT NOT NULL DEFAULT '',
      attention TEXT NOT NULL DEFAULT '',
      parent_id INTEGER REFERENCES pages(id) ON DELETE SET NULL,
      sort_order INTEGER NOT NULL DEFAULT 0,
      provenance TEXT NOT NULL DEFAULT 'imported' CHECK(provenance IN ('designed','imported','proposed')),
      page_type TEXT NOT NULL DEFAULT '',
      collection TEXT NOT NULL DEFAULT '',
      is_button INTEGER NOT NULL DEFAULT 0,
      hidden INTEGER NOT NULL DEFAULT 0,
      missing_from_crawl INTEGER NOT NULL DEFAULT 0,
      variants TEXT NOT NULL DEFAULT '[]',
      crawl_extra TEXT NOT NULL DEFAULT '{{}}',
      import_id INTEGER REFERENCES imports(id),
      updated_at INTEGER NOT NULL,
      CHECK(redirect_to IS NULL OR redirect_to != id),
      CHECK(parent_id IS NULL OR parent_id != id),
      CHECK(is_new = 1 OR url_path IS NOT NULL)
    );
    CREATE INDEX pages_label ON pages(label);
    CREATE INDEX pages_sessions ON pages(sessions);
    CREATE INDEX pages_recommendation ON pages(recommendation);
    CREATE INDEX pages_decision ON pages(decision);
    CREATE INDEX pages_parent ON pages(parent_id, sort_order);
    CREATE INDEX pages_redirect_to ON pages(redirect_to);

    CREATE TABLE page_content_targets(
      page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
      target_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
      PRIMARY KEY(page_id, target_id),
      CHECK(page_id != target_id)
    );
    CREATE TABLE existing_redirects(
      source_url TEXT PRIMARY KEY,
      target_url TEXT NOT NULL,
      status_code INTEGER NOT NULL,
      import_id INTEGER REFERENCES imports(id)
    );
    CREATE TABLE site_issues(
      id INTEGER PRIMARY KEY,
      kind TEXT NOT NULL,
      url TEXT NOT NULL,
      detail TEXT NOT NULL DEFAULT '',
      import_id INTEGER REFERENCES imports(id),
      UNIQUE(kind, url)
    );
    CREATE TABLE settings(
      id INTEGER PRIMARY KEY CHECK(id = 1),
      canonical_host TEXT NOT NULL DEFAULT '',
      title_suffix TEXT NOT NULL DEFAULT '',
      nav_style TEXT NOT NULL DEFAULT 'grid' CHECK(nav_style IN ('grid','sidebar')),
      dropdown_depth INTEGER NOT NULL DEFAULT 0 CHECK(dropdown_depth >= 0),
      mega_menu INTEGER NOT NULL DEFAULT 0,
      sticky_header INTEGER NOT NULL DEFAULT 0,
      version_label TEXT NOT NULL DEFAULT 'v1',
      CHECK(NOT (mega_menu AND dropdown_depth > 0))
    );
    INSERT INTO settings(id) VALUES (1);
    CREATE TABLE views(
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      definition TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE settings ADD COLUMN footer_md TEXT NOT NULL DEFAULT '';
    ALTER TABLE settings ADD COLUMN utility_md TEXT NOT NULL DEFAULT '';
    ALTER TABLE settings ADD COLUMN max_depth INTEGER NOT NULL DEFAULT 0 CHECK(max_depth >= 0);
    """,
    """
    CREATE TABLE nav_items(
      id INTEGER PRIMARY KEY,
      menu TEXT NOT NULL CHECK(menu IN ('main','utility','footer')),
      parent_id INTEGER REFERENCES nav_items(id) ON DELETE CASCADE,
      page_id INTEGER REFERENCES pages(id) ON DELETE CASCADE,
      label TEXT NOT NULL DEFAULT '',
      sort_order INTEGER NOT NULL DEFAULT 0,
      is_button INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL DEFAULT 0,
      CHECK(page_id IS NOT NULL OR label <> ''),
      CHECK(parent_id IS NULL OR parent_id <> id)
    );
    CREATE INDEX nav_items_tree ON nav_items(menu, parent_id, sort_order);
    CREATE INDEX nav_items_page ON nav_items(page_id);
    """,
]


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection, migrations: List[str]) -> int:
    """Apply pending migrations, each in its own transaction. Returns the new version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(migrations):
        raise RuntimeError(f"database is at version {version}, newer than this code ({len(migrations)})")
    for i in range(version, len(migrations)):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _statements(migrations[i]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {i + 1}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(migrations)


def _statements(script: str) -> Iterator[str]:
    buf = ""
    for piece in script.split(";"):
        buf += piece + ";"
        if sqlite3.complete_statement(buf):
            if buf.strip() != ";":
                yield buf.strip()
            buf = ""
    buf = buf[:-1]
    if buf.strip():
        raise ValueError(f"incomplete SQL statement in migration: {buf.strip()[:80]}")


class Store:
    """Owns the data directory: registry.db plus projects/<slug>.db."""

    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.projects_dir = os.path.join(self.data_dir, "projects")
        os.makedirs(self.projects_dir, exist_ok=True)
        self.registry_path = os.path.join(self.data_dir, "registry.db")
        with closing(self.registry()) as conn:
            migrate(conn, REGISTRY_MIGRATIONS)

    def registry(self) -> sqlite3.Connection:
        return connect(self.registry_path)

    def project_path(self, slug: str) -> str:
        if not SLUG_RE.fullmatch(slug):
            raise ValueError(f"invalid project slug: {slug!r}")
        return os.path.join(self.projects_dir, f"{slug}.db")

    def create_project(self, slug: str, name: str) -> None:
        path = self.project_path(slug)
        if os.path.exists(path):
            raise FileExistsError(f"a database file for {slug!r} already exists")
        with closing(self.registry()) as reg:
            reg.execute("INSERT INTO projects(slug, name, created_at) VALUES (?,?,?)", (slug, name, now_ms()))
            try:
                with closing(connect(path)) as conn:
                    migrate(conn, PROJECT_MIGRATIONS)
            except Exception:
                reg.execute("DELETE FROM projects WHERE slug = ?", (slug,))
                for suffix in ("", "-wal", "-shm"):
                    if os.path.exists(path + suffix):
                        os.remove(path + suffix)
                raise

    def list_projects(self) -> List[sqlite3.Row]:
        with closing(self.registry()) as reg:
            return reg.execute("SELECT slug, name, created_at FROM projects ORDER BY name").fetchall()

    def project(self, slug: str) -> sqlite3.Connection:
        """Open a project database. Only projects in the registry can be opened."""
        path = self.project_path(slug)
        with closing(self.registry()) as reg:
            if reg.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone() is None:
                raise KeyError(slug)
        return connect(path)

    def migrate_all(self) -> None:
        for row in self.list_projects():
            with closing(connect(self.project_path(row["slug"]))) as conn:
                migrate(conn, PROJECT_MIGRATIONS)

    def backup_all(self, dest_dir: str) -> List[str]:
        """Consistent copies via VACUUM INTO (safe while the app is running)."""
        os.makedirs(dest_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        written = []
        sources = [("registry", self.registry_path)] + [
            (r["slug"], self.project_path(r["slug"])) for r in self.list_projects()]
        for name, src in sources:
            dest = os.path.join(dest_dir, f"{name}-{stamp}.db")
            with closing(connect(src)) as conn:
                conn.execute("VACUUM INTO ?", (dest,))
            written.append(dest)
        return written
