# Sitemap_Gen

Turn a Markdown nested-list sitemap into a clickable static HTML prototype.

A sitemap is a static document, but information architecture is something you
*feel* by clicking. This tool generates a walkable prototype — real pages, real
URLs, derived navigation — so you can experience the menus and flow before any
design happens. Page content is intentionally low-fidelity wireframe placeholder.

## Getting started

```sh
cp sitemap-example.md sitemap.md   # start from the example
# edit sitemap.md with your own structure
python3 sitemap2proto.py sitemap.md
```

`sitemap.md` is gitignored so your content stays local and out of version control.

## Usage

```sh
python3 sitemap2proto.py sitemap.md                     # -> ./prototype/
python3 sitemap2proto.py sitemap.md out_dir             # custom output folder
python3 sitemap2proto.py sitemap.md --nav-style sidebar # sidebar accordion layout
python3 sitemap2proto.py sitemap.md --dropdown-depth 2  # 2-level flyout nav
python3 sitemap2proto.py sitemap.md --mega-menu         # full-width column mega menu
python3 sitemap2proto.py sitemap.md --sticky-header     # keep header fixed on scroll
python3 sitemap2proto.py --help                         # full option list
```

Then open `prototype/index.html`, or serve it locally:

```sh
python3 -m http.server -d prototype 8000
```

No dependencies — Python standard library only.

## Sitemap format

A nested bullet list. Indentation defines hierarchy; any consistent indent
width works (2 spaces, 4 spaces, or tabs). Top-level bullets become the global
navigation; `index.html` is a generated landing page.

```md
- About
  - Our Story
  - Team
- Products
  - Widgets
    - Blue Widget
  - Pricing
- Contact
```

Lines that aren't list items (headings, blank lines, `#` comments) are ignored,
so you can annotate the file freely.

### Utility Nav section

Add a `## Utility Nav` heading to define a secondary navigation bar — a thin strip above the primary nav, right-aligned, desktop-only. Each top-level item is a link; add children to create a simple one-level dropdown.

```md
## Utility Nav

- Help
  - Getting Started
  - FAQs
- Login
- Register
```

As with the footer, titles that match a page in the main sitemap link there. New titles get their own generated standalone pages.

### Footer section

Add a `## Footer` heading after the main sitemap to define a footer navigation
menu with up to 4 columns. Top-level bullets are column headers (not linked);
their children become links.

```md
## Footer

- Company
  - About
  - Team
- Legal
  - Privacy Policy
  - Terms of Service
```

If a link title matches a page in the main sitemap, it links there. If it's a
new title, a standalone page is generated for it (real URL, not in the global nav).

When the same title appears more than once in the sitemap (say, "Contact Us"
in several sections), footer and utility links go to the shallowest one, and to
the earliest in the file when they're at the same depth.

### Button links

Append `[button]` to any nav item to render it as a visually distinct button
in the global navigation bar:

```md
- About
- Products
- Contact [button]
```

The `[button]` tag is stripped from the page title — the generated page is still
called "Contact". The annotation works at any depth in the tree, but only items
that appear in the top-level nav (direct children of root) are rendered as
buttons; deeper items are plain links regardless.

## How it works

Three stages with a hard wall between them:

1. **Parse** — Markdown text into a tree of nodes (stack-based indent parser). Special sections (`## Utility Nav`, `## Footer`) are extracted first; they can appear in any order.
2. **Derive** — slugs and output paths from each node's position in the tree. Unknown titles from the Utility Nav and Footer sections get standalone nodes here.
3. **Render** — one static HTML file per node, with navigation derived from the tree: global nav, utility nav strip, breadcrumbs, child links or sidebar, footer columns. Also produces `sitemap.html` — a clickable canvas diagram of the full tree.

The renderer never knows the input was Markdown. A new input format (CSV, YAML)
means writing one new parser; stages 2 and 3 are untouched.

## Output

Each run wipes and rebuilds the output folder, so the prototype is always a pure
reflection of the sitemap. Don't hand-edit files inside `prototype/` — they're
overwritten on the next run. The sitemap is the source of truth. To protect
you from a typo, the script refuses to wipe a folder that has files in it but
doesn't look like a previous build.

Every build also generates **`prototype/sitemap.html`** — a visual sitemap
diagram rendered on an HTML canvas. Each node is a clickable box that links to
its generated page. It gives a bird's-eye view of the full information
architecture alongside the walkable prototype.

### Options

| Flag | Values | Default | Description |
|------|--------|---------|-------------|
| `--nav-style` | `grid`, `sidebar` | `grid` | How "In this section" renders: a card grid or a sticky sidebar accordion |
| `--dropdown-depth` | integer ≥ 0 | `0` | Levels of flyout menus in the global nav. `0` = flat links, `1` = single dropdown, `2+` = nested flyouts |
| `--mega-menu` | flag | off | Full-width mega menu panel with columns. Level 2 = column headers, level 3 = column links. Alternative to `--dropdown-depth` |
| `--sticky-header` | flag | off | Keep the header fixed at the top of the viewport on scroll |

All prototypes include a **responsive mobile menu**: on narrow screens the
global nav is replaced by a hamburger button that slides in a full-height panel
with an accordion for items that have children.

## Using it as a library

The script is also an importable module with no dependencies. Rendering never
touches disk, so a web app can serve or zip the result directly:

```python
import sitemap2proto as sp

site = sp.site_from_markdown(open("sitemap.md").read())   # or build a sp.Site from a database
files = sp.render_site(site, sp.Settings(mega_menu=True))  # {"index.html": "...", ...}
sp.write_dir(files, "prototype")                           # or: zip_bytes = sp.write_zip(files)
```

## Web app (content audit)

`app/` is a self-hosted web app for working a Screaming Frog crawl into a
content audit: import the crawl, then filter, sort, and set KWALL Reco / Final
Decision / notes in a grid. Each client project is its own SQLite file under
the data folder.

Run locally:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export SITEMAP_DATA_DIR=./data SITEMAP_INSECURE_COOKIES=1   # plain http on localhost only
.venv/bin/python -m app.manage create-user noah
.venv/bin/uvicorn app.main:create_app --factory --port 8000
```

Run on the homelab with Docker, behind Nginx Proxy Manager over HTTPS:

```sh
printf 'LAN_IP=192.168.1.50\nPROXY_IP=192.168.1.10\n' > .env   # your addresses
docker compose up -d --build
docker compose exec app python -m app.manage create-user noah
```

Admin commands (`python -m app.manage ...`): `create-user NAME`, `migrate`,
and `backup DEST_DIR`, which writes consistent copies of every database and
is safe while the app is running. Run the backup nightly from cron, e.g.
`docker compose exec -T app python -m app.manage backup /data/backups`.

Import the **"Internal: All"** export as CSV. The importer keeps HTML pages only,
merges `www`/bare-host/`http`/trailing-slash variants into one page, sums GA4
traffic across hosts, records existing redirects, and lists problems on the
live site (malformed links, the whole site answering on two hosts) under
"Site issues" on the Import tab.

## Tests

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

`tests/golden/` holds the expected output for a few sitemaps and flag
combinations, and the tests fail if any generated byte changes. After an
intended output change, run `.venv/bin/pytest --update-golden` and review the
diff in git before committing.
