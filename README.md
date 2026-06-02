# Sitemap_Gen

Turn a Markdown nested-list sitemap into a clickable static HTML prototype.

A sitemap is a static document, but information architecture is something you
*feel* by clicking. This tool generates a walkable prototype — real pages, real
URLs, derived navigation — so you can experience the menus and flow before any
design happens. Page content is intentionally low-fidelity wireframe placeholder.

## Usage

```sh
python3 sitemap2proto.py sitemap.md                     # -> ./prototype/
python3 sitemap2proto.py sitemap.md out_dir             # custom output folder
python3 sitemap2proto.py sitemap.md --nav-style sidebar # ->  sidebar accordion layout
python3 sitemap2proto.py sitemap.md --dropdown-depth 2  # ->  2-level flyout nav
python3 sitemap2proto.py --help                         # ->  full option list
```

Then open `prototype/index.html`

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

1. **Parse** — Markdown text into a tree of nodes (stack-based indent parser)
2. **Derive** — slugs and output paths from each node's position in the tree
3. **Render** — static HTML, with navigation derived from the tree:
   global nav from the root's children, breadcrumbs from the ancestor chain,
   child links from each node's own children

The renderer never knows the input was Markdown. A new input format (CSV, YAML)
means writing one new parser; stages 2 and 3 are untouched.

## Output

Each run wipes and rebuilds the output folder, so the prototype is always a pure
reflection of the sitemap. Don't hand-edit files inside `prototype/` — they're
overwritten on the next run. The sitemap is the source of truth.
