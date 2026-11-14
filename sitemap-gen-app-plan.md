# Sitemap_Gen App — Plan

Planning doc for turning `Sitemap_Gen` (a CLI that builds clickable prototypes from a Markdown sitemap) into a self-hosted content audit and IA design tool for large higher-ed sites. Written Nov 12, 2026; revised the same day to a database-first, fully self-hosted design.

## Goal

Take a real site (thousands of pages) from **crawl → content cleanup → designed navigation → clickable prototype → tree test → client-viewable prototype**, with everything running on the homelab (VM 101) and the final prototype uploaded manually to S3.

The content audit and the IA redesign are the same data, so one tool covers both.

## Decisions so far

- **Database is the source of truth.** Markdown stays as import/export and keeps the CLI working.
- **Separate data per client project** (one Postgres database or schema per project, plus a small `projects` table listing them). Chosen over a shared table with a `project_id` column because a forgotten filter leaking one client's pages into another's export is a quiet failure, while schema drift between project databases is a loud one.
- **Fully self-hosted.** Local AI model, own tree-test tool, JS libraries vendored into the app. No external APIs. The only outside piece is the client-facing prototype on S3, which is work infrastructure.
- **One application, many services.** An app shell with tabs and a project picker wraps NocoDB, the tree editor, the prototype preview, and exports.
- **AI classifier is the last phase.** The tool must be fully useful without it. The schema reserves `ai_*` columns now so adding it later doesn't disturb the human columns.

## The core idea: two trees, one table

Every page is a row with two kinds of columns:

- **As-is** (from the crawl): current URL, title, status code, word count, inlinks.
- **To-be** (decided by a person): disposition, where its content goes, and where it sits in the new navigation.

Two views work on that one table:

- **Spreadsheet grid** (NocoDB or similar) for the long tail: filter, sort, bulk-edit thousands of rows.
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
5. Footer and utility-nav parsing code is duplicated.
6. `build()` wipes and writes a directory; it can't return a zip or take a settings dict.
7. Naming is inconsistent across repo, README, and script.

## Design principles

- **Own the database, borrow the UI.** The grid tool is a window onto tables the app owns. Only the app changes the schema.
- **Keep the generator dependency-free.** The web app is a separate layer that imports it.
- **Human-designed top, imported bottom.** Levels 1-3 are curated; deeper levels come from the crawl.
- **AI suggests, a human decides.** Suggestions land in columns and are reviewed in the grid.
- **Relationships, not labels.** "Combine" in a notes field can't be checked or exported. A link to the absorbing page can.
- **Nothing leaves the network** except the finished prototype zip and tree-test answers from participants.

## Architecture

```
                 ┌──────────── app shell (sitemap.home) ────────────┐
                 │  [Project ▾]   Pages | Tree | Prototype | Exports │
                 └───┬──────────────┬──────────┬──────────┬─────────┘
                     │ iframe       │ native   │ iframe   │ native
                  NocoDB        tree editor   static     exports
                     │              │          files        │
Screaming Frog ──► importer ──► [ Postgres: one DB/schema per project ] ──► generator
                                        ▲
                          (last phase) local model (Ollama) ── batch classifier
```

- **Postgres** over SQLite, because the grid tool and the custom app both write.
- **NocoDB** is the leading grid candidate because it can sit on an existing database. Baserow and Grist are alternatives. **Verify** license terms, performance at real crawl sizes, and that it works against app-owned tables and a chosen Postgres schema.
- **App shell:** one page owns the top bar, tabs, and project picker. NocoDB and the prototype load in iframes; the tree editor and exports are native. The project picker must switch every tab at once (right NocoDB base, right tree, right prototype folder), usually by passing the project in the URL.
- **Local AI (later):** Ollama (or similar) behind a single configurable endpoint URL, so the model can move from the Beelink to a bigger box without code changes.
- Everything in Docker on VM 101, behind Nginx Proxy Manager with `.home` names under one parent domain (so iframe cookies work). LAN-only; bind published ports to the LAN IP, not `0.0.0.0`.
- VM 101 had roughly 3-4GB of RAM available when last checked. Measure NocoDB plus Postgres before committing. A local model, when it arrives, may need to run only during batch jobs.

### Embedding gotchas (check early)

- Apps often send `X-Frame-Options` or `Content-Security-Policy: frame-ancestors` headers that block iframes (clickjacking protection). NPM can strip and replace them per proxy host, allowing framing only from the shell's own origin. Check response headers before and after.
- Login inside an iframe can fail if the browser treats the frame as a different site. Keeping all services under the same `.home` parent usually avoids this. Test NocoDB login inside a frame first.
- Fallback if embedding fights back: same top bar on custom pages, NocoDB opens in its own browser tab. Bigger fallback: replace NocoDB with a JS grid library (Tabulator, AG Grid) inside the app.

## Data model

### `pages` (one row per page, existing or planned)

| Column | Purpose |
|---|---|
| `id` | Stable identity. Everything links by ID, never by title. |
| `current_url` | Empty for planned pages. |
| `is_new` | True for pages that don't exist yet (often the absorber in a combine). |
| crawl columns | Title, H1, status code, word count, inlinks, crawl depth. |
| `disposition` | `keep` / `delete` / `review` / `combine` / `split` |
| `redirect_to` | Link to one row: where the old URL sends visitors. |
| `content_goes_to` | Link to several rows. Only needed for `split`. |
| `status` | `proposed` / `approved` / `done`. Progress, kept separate from disposition. |
| `notes` | Free text, such as what content to carry over. |
| `parent_id`, `sort_order` | Position in the **new** navigation tree. |
| `path` | Stored path such as `/admissions/apply/`, so the grid can sort and indent. |
| `provenance` | `designed` / `imported` / `proposed` |
| `page_type` | Landing, listing, article, form. |
| `ai_page_type`, `ai_disposition`, `ai_target`, `ai_confidence` | Reserved for classifier suggestions (last phase), separate from the human columns. Empty until then. |
| `flags` | Button, hidden from nav. |

Project-level settings live in the `projects` table: nav style, dropdown depth, mega menu, sticky header, footer columns, utility nav, version label.

### How each disposition uses the links

| Disposition | `redirect_to` | `content_goes_to` | Meaning |
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
4. For `split`, `redirect_to` must be one of the `content_goes_to` rows.
5. If a target's disposition changes, flag every row pointing at it.

Grid tools enforce rules poorly. Use saved filtered views as a "problems" list, and run the same checks in the app before any export.

## Capabilities

### A. Crawl import
- Stdlib `csv`. Expected columns include Address, Status Code, Indexability, Title, H1, Word Count, Crawl Depth, Inlinks. **Verify against a real export.**
- Filter non-HTML, redirects, noindex, errors; normalize query strings and trailing slashes.
- Initial tree position comes from URL path segments. URL hierarchy often differs from navigation on higher-ed sites, so treat it as a starting guess.
- Re-importing a newer crawl should update rows by URL without losing decisions.
- Optional extra columns: analytics or Search Console traffic, last-modified dates, near-duplicate flags.
- Obvious cases (404s, redirects, clear sections) are handled by rules at import time, not AI.

### B. Content cleanup in the grid
- Bulk-assign dispositions, link combine and split targets, track status, add notes.
- Saved views: by section, by disposition, problems list.

### C. Visual tree editor
- Designed levels only, with roll-up counts of pages beneath each node and "absorbs N pages" on combine targets.
- Add, rename, drag to reorder or re-parent. Seeded from the existing canvas view.

### D. Prototype generation
- Generator reads the tree from the database. Pages with `combine`, `split`, or `delete` aren't generated as separate nodes.
- Depth cap so menus stop at the designed levels.
- Page types first; free-form layout blocks and Octopus-style thumbnails only if types prove too limiting.
- Prototype tab previews the generated files in an iframe, same pattern as `lab.home`.

### E. Exports
- **Prototype zip** for manual S3 upload: `robots.txt` disallowing everything, `noindex` meta tags, filename with project and version (`clientname-sitemap-v3.zip`). No AWS credentials on the homelab.
- **Redirect map**: every combine, split, and delete-with-target row as `old URL → final URL`, with chains resolved.
- **Content audit** as CSV or spreadsheet for the client.
- **Markdown sitemap**, for the existing CLI.

### F. Tree testing (self-built)
- Labels-only tree, tasks with correct answers, path recording, success and directness metrics.
- Participants are outside the LAN, so **only the test page and its results endpoint** are exposed, as a separate small service with its own proxy host. The main app stays LAN-only.
- Treat it as untrusted public input: validate submissions, rate-limit, collect nothing but answers. This is the homelab's first internet-facing service, so it gets its own security review.
- Optional interim: export the tree in a format kardsort.com can import (**verify** the format).

### G. AI classifier (local, batch, last)
- Runs overnight as a queue against a local model. Speed doesn't matter; it's not interactive.
- Jobs: page type, suggested disposition, near-duplicate clusters, suggested combine target (strongest page by inlinks or traffic as the absorber), suggested labels.
- Writes only to the `ai_*` columns. Humans review in the grid (filter to AI-suggested, spot-check, bulk accept into the real columns).
- Send only ambiguous pages.
- Model lives behind one endpoint URL. The Beelink N100 handles small models slowly; a Strix Halo box, if bought for the rack, would run much larger ones.
- Postponed to the end so the rest of the tool proves itself first, and so the hardware decision can be made with real data on how much classification is needed.

## Phases

| Phase | Work | Done when |
|---|---|---|
| 0 | Dry run by hand: take a real crawl export into a spreadsheet, add disposition and target columns, work a section. Also: iframe-embed test of NocoDB on `lab.home` | The workflow feels right; we know whether iframes work |
| 1 | Generator refactor: library, settings dict, ID-based links, zip output, consolidated parsing, naming. Postgres schema, crawl importer, NocoDB on VM 101 | A real crawl loads and bulk edits are responsive |
| 2 | Dispositions, target links, validation views, redirect map and audit exports | A cleaned-up section exports a correct redirect map |
| 3 | App shell: tabs, project picker, embedded NocoDB and prototype. Visual tree editor with roll-ups; prototype from the database; zip download | Pick a project, design top levels, download a working prototype, all from one screen |
| 4 | Page types and layout wireframes | Prototype pages differ by type |
| 5 | Self-hosted tree test with a public endpoint | A client participant completes a test from outside the LAN |
| 6 | Local AI classifier: Ollama, batch queue, `ai_*` columns, review views in the grid | Overnight run on a crawl-sized table produces suggestions a person can bulk-accept |

## Open questions

- Who uses it: just Noah, or the KWALL team and clients? This decides auth, permissions, and whether multi-user grid features are required.
- Is the prototype internal-only or client-presentable?
- Is the Octopus-style page-thumbnail look essential, or is a clean tree enough?
- Postgres database per project, or schema per project within one database? (Depends partly on whether NocoDB can target a schema.)
- Existing S3 bucket and CloudFront setup for client prototypes?
- A sanitized Screaming Frog export to test against.
- How the tree-test service is exposed: port forward, reverse tunnel, or similar. Decide at phase 5.
- Which local model is good enough for page classification, and whether it fits on the Beelink or waits for bigger hardware. Decide at phase 6.

## Risks

- Grid tool performance at tens of thousands of rows.
- Schema drift across per-project databases; the app needs a migration step that loops over all of them.
- Iframe embedding may fight back (headers, cookies); fallback is a custom grid.
- URL structure may mislead on sites with legacy URLs.
- Public tree-test endpoint is the first internet-facing homelab service.
- Scope: this is now close to a content audit platform. Custom code should stay limited to the importer, the target model, the shell, the visual top levels, generation, exports, and the tree test.
