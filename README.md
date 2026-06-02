# sitemap2proto

Turn a Markdown nested-list sitemap into a clickable static HTML prototype.

A sitemap is a static document, but information architecture is something you
*feel* by clicking. This tool generates a walkable prototype — real pages, real
URLs, derived navigation — so you can experience the menus and flow before any
design happens. Page content is intentionally low-fidelity wireframe placeholder.

## Usage

```sh
python3 sitemap2proto.py sitemap.md            # -> ./prototype/
python3 sitemap2proto.py sitemap.md out_dir    # custom output folder
```

Then open `prototype/index.html`, or serve it for a more realistic feel:

```sh
python3 -m http.server -d prototype 8000       # visit localhost:8000
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

## Roadmap

- CSV path-based parser (same tree, proves the architecture)
- Per-page template hints for varied wireframe bodies
- Annotation notes rendered as stakeholder review comments
- Optional watch-and-rebuild mode
