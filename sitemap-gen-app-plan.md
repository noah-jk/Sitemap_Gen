# Sitemap_Gen App — Plan

Planning doc for turning `Sitemap_Gen` (a CLI that builds clickable prototypes from a Markdown sitemap) into a self-hosted content audit and IA design tool for large higher-ed sites. Written Nov 12, 2026. Revised Nov 14, 2026: one Python app with SQLite instead of NocoDB + Postgres, AI classifier removed, tree test isolated from the main app.

## Goal

Take a real site (thousands of pages) from **crawl → content cleanup → designed navigation → clickable prototype → tree test → client-viewable prototype**, with everything running on the homelab (VM 101) and the final prototype uploaded manually to S3.

The content audit and the IA redesign are the same data, so one tool covers both.

## Decisions so far

- **One application.** A single Python web app owns the grid, the tree editor, the prototype preview, and exports. No third-party grid service, no iframe embedding of other apps.
- **Users:** Noah now, possibly one coworker later. The design assumes a handful of users at most, never simultaneous editing by a large team.
- **SQLite, one database file per client project.** A `projects.db` lists the projects; each project is its own file (`projects/<slug>.db`). Isolation is physical: a forgotten filter can't leak one client's pages into another's export, and archiving or handing off a project is copying a file.
- **Database is the source of truth.** Markdown stays as import/export and keeps the CLI working.
- **Fully self-hosted.** JS libraries vendored into the app. No external APIs. Outside pieces: the client-facing prototype on S3 (work infrastructure) and the tree-test page, reached through a Cloudflare Tunnel.
- **No AI.** Import-time rules plus the extra columns Screaming Frog already provides (near-duplicates, analytics and Search Console traffic) do the sorting. People make every decision in the grid.

### Why not NocoDB + Postgres (the earlier design)

NocoDB would give saved views, link pickers, and multi-user editing for free, but it costs:

- **Security:** stripping clickjacking headers (`X-Frame-Options`, `frame-ancestors`) so it can be iframed, a second login system, and validation rules that can only be checked before export, not on write.
- **Resources:** a separate Node service plus Postgres on a VM with 3-4GB of RAM.
- **Complexity:** Postgres was chosen only because two systems would write to the database. With one app, that reason disappears.

For one or two users, building a grid with a library is less total work and a smaller attack surface. If the team ever grows to several people editing at once, the SQL is kept portable so the move to Postgres is a driver change plus a data copy.

## The core idea: two trees, one table

Every page is a row with two kinds of columns:

- **As-is** (from the crawl): current URL, title, status code, word count, inlinks, traffic.
- **To-be** (decided by a person): disposition, where its content goes, and where it sits in the new navigation.

Two views work on that one table:

- **Spreadsheet grid** for the long tail: filter, sort, bulk-edit thousands of rows.
- **Visual tree** for the designed top 2-3 levels, with a roll-up count on each node ("Admissions: 847 pages"). The visual editor never draws thousands of nodes.

Three outputs come from the same rows: the content audit (a client deliverable), the prototype, and the redirect map for launch.

## Current state of the generator (verified in the repo)

- Repo: `noah-jk/Sitemap_Gen` (public). Script: `sitemap2proto.py`, about 1,060 lines, Python stdlib only. About half is embedded CSS and HTML templates.
- Three stages with a hard wall: **parse** (Markdown → `Node` tree), **derive** (slugs, output paths), **render** (static HTML). The renderer never sees Markdown, so a database can replace the parser without touching the renderer.
- Input: nested bullet list, plus `## Footer` and `## Utility Nav` sections, plus a `[button]` tag.
- Output: one `index.html` per node. All links are **relative and end in `index.html`**, so a prototype works at any URL prefix.
- Also emits `sitemap.html`, a read-only canvas diagram.
- Settings (`--nav-style`, `--dropdown-depth`, `--mega-menu`, `--sticky-header`) are CLI flags only.

### Known limitations to fix

1. `Node` carries no metadata.
2. Settings aren't stored with the sitemap.
3. Every page renders as the same wireframe placeholder.
4. `build_title_index` is keyed by title, so duplicate titles in different sections collide.
5. Footer and utility-nav parsing code is duplicated (and nav-building has three near-identical branches, #5).
6. `build()` wipes and writes a directory; it can't return a zip or take a settings dict.
7. Naming is inconsistent across repo, README, and script.
8. No tests (#6). Snapshot tests come before the refactor.

## Design principles

- **Keep the generator dependency-free.** The web app is a separate layer that imports it.
- **Human-designed top, imported bottom.** Levels 1-3 are curated; deeper levels come from the crawl.
- **Validate on write, not just before export.** Every change goes through the app, so the rules run in the same transaction as the edit.
- **Relationships, not labels.** "Combine" in a notes field can't be checked or exported. A link to the absorbing page can.
- **Crawl data is untrusted input.** Titles and H1s come from a website someone else controls. Escape everywhere, render grid cells as text, guard spreadsheet exports against formula injection.
- **Nothing leaves the network** except the finished prototype zip and tree-test traffic.

## Architecture

```
                 ┌──────────────── app (sitemap.home, LAN only) ────────────────┐
                 │  [Project ▾]   Pages | Tree | Prototype | Exports | Tree test │
                 │  FastAPI + Jinja templates, Tabulator grid (vendored)         │
                 └──────┬─────────────────────────────┬──────────────────────────┘
                        │                             │ imports
Screaming Frog CSV ──► importer ──► [ SQLite: one file per project ] ──► generator ──► zip
                                                      │
                  app ──► publish labels-only snapshot ──► tree-test admin port (LAN only)
                  app ◄── pull results ◄────────────────── tree-test admin port (LAN only)
                                                      │
                 ┌─────────── tree-test service (separate container) ───────────┐
                 │  own SQLite, no access to project files                      │
                 └──────────────────────────┬───────────────────────────────────┘
                                            │ public routes only
                                   cloudflared tunnel ──► participants
```

- **Stack:** FastAPI (Pydantic validation at every input boundary, which matters most for the public tree-test endpoint) with server-rendered Jinja pages and small JSON endpoints for the grid. The generator is imported as a library, so everything is one language.
- **Grid:** Tabulator (MIT), vendored. Remote pagination, sorting, and filtering, so the browser only holds one page of rows. Cells use the plain-text formatter, never HTML.
- **SQLite settings:** WAL mode, `foreign_keys=ON`, `busy_timeout` set. Migrations versioned with `PRAGMA user_version` and run across every project file on startup.
- **Auth:** single login with a session cookie (`HttpOnly`, `SameSite=Strict`, `Secure` behind the proxy), CSRF token on forms. A `users` table from day one, so adding a coworker is a row, not a redesign.
- **Concurrent edits (for the coworker case):** each row carries `updated_at`; a save that doesn't match the stored value is rejected and the grid reloads the row. No silent last-write-wins.
- **Prototype preview:** served by the app itself and shown in an `<iframe sandbox="allow-scripts">` (no `allow-same-origin`), with a strict CSP on prototype responses. Generated pages can't touch the app's session even if escaping ever slipped.
- **Deployment:** Docker on VM 101: `app`, `treetest`, `cloudflared`. The app sits behind Nginx Proxy Manager with a `.home` name, ports bound to the LAN IP, not `0.0.0.0`.
- **Backups:** nightly `VACUUM INTO` (or the `sqlite3 .backup` command) per project file. Never `cp` a live database in WAL mode.
- **Resources:** the app is one Python process. RAM is no longer a constraint on VM 101.

## Data model

### `pages` (one row per page, existing or planned)

| Column | Purpose |
|---|---|
| `id` | Stable identity. Everything links by ID, never by title. |
| `current_url` | Unique when set. Empty for planned pages. |
| `is_new` | True for pages that don't exist yet (often the absorber in a combine). |
| crawl columns | Title, H1, meta description, canonical, status code, crawl depth, inlinks, unique inlinks, % of total, hash. Indexability and word count when present. |
| `label` | Title with the site-wide suffix removed. Used in the grid, tree, and prototype. |
| `in_current_nav` | True when the page is linked from every page (derived from `% of Total`). |
| traffic columns | GA4 sessions, views, engaged sessions, engagement rate, key events; Search Console clicks/impressions if connected. Optional. |
| duplicate columns | Near-duplicate count and closest match, when Crawl Analysis ran. Optional. |
| `crawl_extra` | JSON of every unmapped crawl column. |
| `recommendation` | KWALL's recommended disposition: `keep` / `delete` / `review` / `combine` / `split` (`CHECK` constraint). Exported as "KWALL Reco". |
| `decision` | The client's final disposition, same values. Empty until decided. Exported as "Final Decision". |
| `redirect_to` | Foreign key to one row: where the old URL sends visitors. |
| `status` | `todo` / `done`. Implementation progress at launch, kept separate from the decision. |
| `notes` | Free text, such as what content to carry over. |
| `parent_id`, `sort_order` | Position in the **new** navigation tree. |
| `path` | Stored path such as `/admissions/apply/`, so the grid can sort and indent. |
| `provenance` | `designed` / `imported` / `proposed` |
| `page_type` | Landing, listing, article, form. |
| `flags` | Button, hidden from nav. |
| `updated_at` | Used to detect conflicting edits. |

### `page_content_targets` (join table, for `split`)

`(page_id, target_id)`, both foreign keys to `pages`. This is the "content goes to several pages" link, which a single column can't hold.

### Other tables

- **`settings`** (one row per project file): nav style, dropdown depth, mega menu, sticky header, footer columns, utility nav, version label.
- **`views`**: saved grid views as filter and sort JSON (by section, by disposition, problems, near-duplicates).
- **`existing_redirects`**: redirects already live on the site (source, target, type), from the crawl's 3xx rows. The redirect map export resolves against these so a new redirect never points into an old chain. The sample already had one: `/admission/our/get` → `http://www.whittier.edu/admission/our/visit` → `admission.whittier.edu/portal/visit`.
- **`imports`**: one row per crawl upload (file name, crawl timestamp, GA4 date range).
- **`projects.db`**: project list (slug, client name, created) and `users`.

### How each disposition uses the links

"Disposition" below means the effective value: `decision` when set, otherwise `recommendation`. Keeping both preserves what KWALL recommended when the client overrides it.

| Disposition | `redirect_to` | content targets | Meaning |
|---|---|---|---|
| keep | empty | empty | Page survives. |
| delete | optional | empty | With a target: redirect. Without: let it 404. |
| combine | required | empty | Content merges into the target. Many rows can share a target. |
| split | required | two or more | Content divides among several pages. The old URL can only redirect to one, so `redirect_to` picks it. |
| review | empty | empty | Undecided. |

### Validation rules

1. `combine` and `split` must have a `redirect_to`.
2. A row can't target itself.
3. A target can't itself be deleted or combined away. That creates redirect chains or loops.
4. For `split`, `redirect_to` must be one of the content targets.
5. If a target's disposition changes, flag every row pointing at it.

Rules 1-4 reject the write in the same transaction. Rule 5 sets a flag that shows up in the "problems" view. The same checks run again across the whole table before any export. The redirect map uses only rows with a final `decision`; rows with a recommendation but no decision are listed separately as undecided.

## Capabilities

### A. Crawl import

Checked against a sample "Internal: All" export (79 rows, 81 columns, UTF-8, GA4 integration on) on Nov 15, 2026.

- Stdlib `csv`, reading the header by name (Screaming Frog's column order changes between versions). Mapped columns: Address, Content Type, Status Code, Title 1, H1-1 (fall back to H1-2), Meta Description 1, Canonical Link Element 1, Crawl Depth, Inlinks, Unique Inlinks, % of Total, Hash, Redirect URL, Redirect Type, GA4 Sessions / Views / Engaged sessions / Engagement rate / Key events. Every other column goes into a `crawl_extra` JSON field so nothing is lost.
- **Columns that were empty in the sample:** Indexability, Word Count, Link Score, and the near-duplicate columns. Link Score and near-duplicates only fill in after Crawl Analysis runs, and near-duplicates must be turned on in the crawl config. The importer treats all of these as optional.
- **What gets imported:** the export mixes pages, redirects, errors, and files (the sample had 69 × 200, 8 × 301, 1 × 302, 1 × 403, 1 PDF).
  - HTML 200s become `pages` rows.
  - 3xx rows go into `existing_redirects` (source, target, type), not `pages`.
  - 4xx/5xx rows become pages with disposition `review` and a reason.
  - PDFs, CSS, images, and other non-HTML files are dropped. This is an IA tool, so only HTML pages are in scope. The import summary reports how many rows were skipped and why.
- **Normalization and variant merging:** Screaming Frog often lists `https://whittier.edu`, `https://www.whittier.edu`, and `https://www.whittier.edu/` as separate rows. The importer reduces every address to a normalized key (one canonical host per project, trailing slash stripped except on the root, fragments dropped, query strings sorted or stripped by rule) and merges rows that share a key into one page:
  - The 200 response on the canonical host supplies the crawl data.
  - Variants that redirect are recorded in `existing_redirects`.
  - GA4 numbers take the highest value among the variants, not the sum, because the GA4 integration can attach the same path's numbers to several variants.
  - The merged variant addresses are kept on the page so nothing is silently lost.
- Redirect targets may be off-site (`admission.whittier.edu`, `wcpoets.com`) and are kept as-is.
- **Non-HTML rows:** CSS, images, and documents come through the export even with those turned off in the crawl config. The importer filters by `Content Type`, so the crawl settings don't need to be perfect.
- **Labels:** titles carry a site-wide suffix (" | Whittier College" on 67 of 69). Detect the most common suffix and store a cleaned `label` for the grid, tree, and prototype; keep the raw title.
- **Current-nav flag:** `% of Total` splits cleanly into ~52-54% (linked from every page: header, footer, or utility nav) and ~0-2%. Pages above a threshold get `in_current_nav = true`, which shows the existing global navigation in the grid for free.
- **Ignore:** H2s (the sample's H2-1/H2-2 are nav labels on every page), Last Modified (the server returns the crawl time, not the content date), GA4 Total revenue, and the semantic-similarity columns (they need an external AI provider).
- **Initial tree position:** URL path segments are a weak guess here. Student support pages sit at `/counseling`, `/studenthealth`, `/accessibility`, `/caas`, and `/campuslife/studentsupport`. If the site has breadcrumbs, a Screaming Frog Custom Extraction of the breadcrumb trail gives a better starting tree; use URL segments only as a fallback.
- **Collections:** high-volume patterns such as `/calendar/*` (events) and `/news/*` (posts) roll up into one collection node with a count in the tree. Every row still appears in the grid.
- **Exact duplicates:** rows with the same `Hash` are exact duplicates with no near-duplicate setting needed.
- **Import metadata:** an `imports` row per upload records the file name, crawl timestamp, and GA4 date range (the CSV doesn't contain the range, so the import form asks for it).
- Re-importing a newer crawl updates rows by URL without losing decisions. Pages missing from the new crawl are flagged, not deleted.
- Upload limits on the import form: size cap, CSV only, parsed in a background task so a big crawl doesn't block the app.
- Crawl files never go in the repo (it's public); `*.csv` is gitignored.

**Screaming Frog settings for real crawls:** connect GA4 (and Search Console if available), turn on near-duplicates, add a Custom Extraction for breadcrumbs if the site has them, run Crawl Analysis before exporting, export "Internal: All".

### B. Content cleanup in the grid
- Bulk-assign recommendations, decisions, and status from a selection action bar.
- Views for "recommended but undecided" and "client overrode the recommendation".
- Link picker for `redirect_to` and content targets: type-ahead search on title and URL.
- Saved views: by section, by disposition, problems list, near-duplicate clusters sorted by traffic and inlinks (the strongest page is the natural absorber).

### C. Visual tree editor
- Designed levels only, with roll-up counts of pages beneath each node and "absorbs N pages" on combine targets.
- Add, rename, drag to reorder or re-parent. Seeded from the existing canvas view.

### D. Prototype generation
- Generator reads the tree from the database. Pages with `combine`, `split`, or `delete` aren't generated as separate nodes.
- Depth cap so menus stop at the designed levels.
- Page types first; free-form layout blocks and Octopus-style thumbnails only if types prove too limiting.
- Prototype tab previews the generated files in a sandboxed iframe.

### E. Exports
- **Prototype zip**, built in memory, for manual S3 upload: `robots.txt` disallowing everything, `noindex` meta tags, filename with project and version (`clientname-sitemap-v3.zip`). No AWS credentials on the homelab.
- **Redirect map**: every combine, split, and delete-with-target row as `old URL → final URL`, with chains resolved, including chains through `existing_redirects`.
- **Content audit** as CSV for the client, in the same layout as the current hand-groomed sheet: Address, KWALL Reco, Final Decision, Notes, then the crawl and GA4 columns, with empty columns dropped. Cells starting with `=`, `+`, `-`, `@`, tab, or carriage return are prefixed with `'` so Excel doesn't run them as formulas.
- **Markdown sitemap**, for the existing CLI.

### F. Tree testing (self-built, isolated)
- Labels-only tree, tasks with correct answers, path recording, success and directness metrics.
- **Separate container with its own SQLite.** It never mounts the project files and holds no credentials for the main app.
- The main app **publishes** a snapshot to it: labels and tasks only, no URLs, notes, or crawl data. Results are **pulled** back by the main app.
- Two ports: public routes (test page, submit results) exposed through the Cloudflare Tunnel; admin routes (publish, fetch results) bound to the LAN only and never routed through the tunnel.
- **Cloudflare Tunnel instead of a port forward:** an outbound-only connection, no open inbound port on the home network, and Cloudflare's rate limiting in front.
- Treat submissions as untrusted: Pydantic schemas, request-size limits, per-IP rate limiting, unguessable test IDs, tests close after an end date. Collect nothing but answers and timings: no names, emails, or cookies.
- Gets its own security review before going live.
- Optional interim: export the tree in a format kardsort.com can import (**verify** the format).

## Phases

| Phase | Work | Done when |
|---|---|---|
| 0 | Dry run by hand: take a real crawl export into a spreadsheet, add disposition and target columns, work a section. Grid spike: load the same crawl into SQLite and drive it from Tabulator with remote filter/sort | The workflow feels right; the grid stays responsive at real crawl size |
| 1 | Tests, then generator refactor (library, settings dict, ID-based links, zip output, consolidated parsing, naming). SQLite schema and migrations, crawl importer, app skeleton with login, project list, read/edit Pages grid | A real crawl imports and inline edits in the grid are responsive |
| 2 | Dispositions, link picker, validation on write, bulk edit, saved views and problems list, redirect map and audit exports | A cleaned-up section exports a correct redirect map |
| 3 | Visual tree editor with roll-ups, prototype from the database, sandboxed preview, zip download | Pick a project, design top levels, download a working prototype, all from one screen |
| 4 | Page types and layout wireframes | Prototype pages differ by type |
| 5 | Isolated tree-test service behind a Cloudflare Tunnel | A client participant completes a test from outside the LAN, and the results land back in the project |

## Not planned yet

- **Importing client decisions back.** The client may fill in "Final Decision" in the exported CSV. Reading that sheet back and matching decisions by URL would save re-keying, but the CSV export is enough for a first deliverable. Revisit after the first real project.

## Open questions

- Is the prototype internal-only or client-presentable?
- Is the Octopus-style page-thumbnail look essential, or is a clean tree enough?
- Existing S3 bucket and CloudFront setup for client prototypes?
- A domain on Cloudflare for the tunnel (needed by phase 5).
- When the coworker joins, do they need their own login only, or also per-project permissions?

## Risks

- Building grid features NocoDB would have provided (link picker, saved views, bulk edit). Keep them minimal and driven by the real workflow from phase 0.
- SQLite allows one writer at a time. Fine for one or two people; if the team grows to many simultaneous editors, move to Postgres (SQL kept portable for this).
- URL structure misleads on sites with legacy URLs (confirmed in the sample crawl). Breadcrumb extraction is the mitigation.
- Inlink counts are inflated by sitewide navigation (2,800+ for every nav-linked page in the sample), so they barely separate pages. Traffic is the better signal for choosing which page absorbs a combine.
- The public tree-test endpoint is the first internet-facing homelab service. It's isolated by design but still needs its own review.
- Scope: custom code should stay limited to the importer, the grid and its target model, the visual top levels, generation, exports, and the tree test.
