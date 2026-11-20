# Sitemap_Gen

A self-hosted tool for taking a large site from **crawl → content audit → new sitemap and menus →
clickable prototype**. It's two parts:

- **The web app** (`app/`): import a Screaming Frog crawl, decide what happens to every page, design
  the new sitemap and navigation, and preview or download a wireframe prototype.
- **The generator** (`sitemap2proto.py`): turns a sitemap into a static, clickable HTML prototype. It
  runs on its own from a Markdown file, and the app uses it as a library.

## The web app

Each client project is its own SQLite file. The tabs follow the workflow:

- **Import:** the Screaming Frog **"Internal: All"** export as CSV. HTML pages only; `www`, bare-host,
  `http`, and trailing-slash variants merge into one page, GA4 traffic is summed across hosts,
  existing redirects are recorded, and problems on the live site (malformed links, the site answering
  on two hosts) are listed. Turn on **Word Count** (Configuration → Spider → Extraction) and
  **Near Duplicates** in Screaming Frog to fill the Words column and the "Short pages" view; the tab
  says when a crawl left them empty. Re-importing keeps all decisions and structure.
- **Pages:** the audit grid. Set Recommendation, Final Decision, status, notes, and page type per page
  or in bulk (selected rows or everything matching the filters). **Combine** and **split** pick the
  page(s) the content goes to, and rules are checked on every save; anything left inconsistent shows
  in the **Problems** view. Built-in views cover the usual questions (undecided, no traffic, short
  pages, dropped from the navigation…), and filters, sort, and columns save as named views. Paths link
  to the live page. Click a label for the **detail panel**: every crawl column, traffic, where the page
  sits in the new sitemap, and editable decision fields; ↑/↓ (or J/K) step through rows.
  **Planning** fields (owner, writer, priority, suggested content, and a **New URL**) are in the panel
  and in optional grid columns; the "New pages" view gathers pages that aren't on the current site.
- **Rules:** if-then bulk updates, built like Data Studio filters: include (or exclude) pages where
  conditions match (all in a group; any group), then set fields (to a value, another field's value, or
  blank) or **Combine into** a page (its sitemap parent, its URL parent, or the page at a path), which
  sets the combine and the page to merge into together. Path patterns use `*` for one folder
  and `**` for any depth. Rules are saved to your account and reused across projects; preview shows what
  each would change, and applying is one undoable step. Values a rule set say which rule, until edited
  by hand. Date tests use each page's date from sources chosen per project: a date or year in the URL
  (including 8-digit dates like `fri-01062023`), any crawl column such as a custom extraction of the
  publish date, or Last Modified.
- **Sitemap:** every kept page, nested as it will be in the new site, as a list or a **map** (top
  three levels as columns, with a page count under each). Drag to reorder and re-nest, add new pages,
  double-click a name to rename it, and click a page-type tag to change it. New pages get a planned URL
  from where they sit (parent's URL + their name) that follows them as they move, until one is typed;
  existing pages keep theirs unless given a new one.
- **Navigation:** the main menu, utility nav, and footer, built separately from the sitemap by adding
  what belongs (here, from the Sitemap tab, or in bulk from the grid, with sub-pages if wanted); a
  page that doesn't exist yet can be created right from **Add pages…**, and goes into the sitemap too. List
  or map view, drag within and between menus or use **Move to…**, shorter labels, headings, and
  link/button chips. **Remove** takes an entry and everything under it.
- **Prototype:** a sandboxed preview of the site built from the sitemap and menus, with menu style and
  depth settings that save as you change them. Every page type (landing, listing, list, article,
  form) has its own wireframe. The zip download is noindexed and ready to upload to S3.
- **Exports:** the redirect map (decided pages, plus pages moving to a new URL; redirects into new
  pages use their planned URL, and old redirects are re-pointed past removed or moved pages), the
  content audit CSV for the client, and a Markdown sitemap the generator can read.

Every edit can be undone (**Undo**/**Redo** in the top bar, Ctrl/⌘+Z, and a **History** list), so
removals and bulk edits don't ask first. Imports aren't part of the history. Light and dark themes
follow the device, or pick one in the top bar.

### Run it

On the homelab, with Docker behind Nginx Proxy Manager. A GitHub Action tests every push and publishes
`ghcr.io/noah-jk/sitemap_gen`: `latest` from `main`, plus a tag per branch (slashes become hyphens).

- **Portainer:** Stacks → Add stack, paste `compose.yaml`, and set `LAN_IP` (the server's LAN address;
  the app listens only there). Optional: `HOST_PORT` (default 8080), `PROXY_IP` (Nginx Proxy Manager's
  address, so failed-login limits count per visitor), and `IMAGE_TAG` to run a branch build. Update
  with **Pull and redeploy**. Create the first login from the container's Console:
  `python -m app.manage create-user noah`.
- **Command line:** put `LAN_IP=...` in `.env` next to `compose.yaml`, then
  `docker compose pull && docker compose up -d` and
  `docker compose exec app python -m app.manage create-user noah`.
- **Locally:**

  ```sh
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  SITEMAP_DATA_DIR=./data .venv/bin/python -m app.manage create-user noah
  SITEMAP_DATA_DIR=./data .venv/bin/uvicorn app.main:create_app --factory --port 8000
  ```

Data lives in the `sitemap_data` volume. Admin commands (`python -m app.manage …`): `create-user`,
`set-password`, `list-users`, `migrate`, and `backup DEST_DIR`, which is safe while the app runs. Run
it nightly, e.g. `docker exec <container> python -m app.manage backup /data/backups`, and copy the
backups off the volume.

### Security

- Logins: scrypt hashes, 5 failed tries per visitor per 5 minutes, a signed SameSite=Strict session
  cookie (14 days). Changing a password logs out other browsers. Set `SITEMAP_SECURE_COOKIES=1` if the
  app is only reached over HTTPS (plain http on the LAN works without it).
- Every change needs a CSRF token, pages carry a strict Content-Security-Policy, and request bodies
  are capped (1 MB; 51 MB for a crawl upload) before anything reads them.
- Prototype preview links work without logging in for 12 hours so they can be shared; the preview
  runs sandboxed, with no access to the app.
- `pip-audit -r requirements.txt` checks the pinned packages for known vulnerabilities.

## The generator

No dependencies, Python standard library only.

```sh
cp sitemap-example.md sitemap.md        # sitemap.md is gitignored
python3 sitemap2proto.py sitemap.md     # -> ./prototype/ (open prototype/index.html)
python3 sitemap2proto.py --help
```

| Flag | Default | |
|------|---------|---|
| `--nav-style grid\|sidebar` | `grid` | Sub-pages as a card grid or a sticky sidebar |
| `--dropdown-depth N` | `0` | Levels of flyout menus (0 = flat links) |
| `--mega-menu` | off | Full-width mega menu (level 2 headers, level 3 links) |
| `--sticky-header` | off | Keep the header fixed on scroll |

The sitemap is a nested bullet list; indentation sets the hierarchy, and anything that isn't a list
item is ignored. Optional `## Utility Nav` and `## Footer` sections (up to 4 columns) add those menus;
their titles link to matching pages, or get pages of their own. Append `[button]` to a top-level item
to show it as a button.

```md
- About
  - Our Story
- Products
  - Pricing
- Contact [button]

## Footer
- Company
  - About
```

Every build also writes `sitemap.html`, a clickable diagram of the whole tree, and every prototype has
a mobile menu. The output folder is rebuilt from scratch each run (the script refuses to wipe a
folder that doesn't look like a previous build), so don't hand-edit it.

It works in three stages (parse Markdown → derive slugs and paths → render), and rendering never
touches disk, which is how the app uses it:

```python
import sitemap2proto as sp
site = sp.site_from_markdown(open("sitemap.md").read())    # or build an sp.Site from a database
files = sp.render_site(site, sp.Settings(mega_menu=True))   # {"index.html": "...", ...}
sp.write_dir(files, "prototype")                            # or sp.write_zip(files); render_file() renders one page
```

## Tests

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

`tests/golden/` holds the expected generator output for a few sitemaps and flag combinations; any
changed byte fails the tests. After an intended change, run `.venv/bin/pytest --update-golden` and
review the diff before committing.
