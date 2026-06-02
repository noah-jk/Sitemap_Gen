#!/usr/bin/env python3
"""
Sitemap_Gen - turn a Markdown nested-list sitemap into a clickable static prototype.

Usage:
    python3 sitemap2proto.py sitemap.md            # -> ./prototype/
    python3 sitemap2proto.py sitemap.md out_dir     # -> ./out_dir/

The whole program is three stages with a hard wall between them:

    PARSE   markdown text            -> a tree of Node objects
    DERIVE  tree                     -> slugs, file paths (still no HTML)
    RENDER  tree                     -> static HTML files on disk

The renderer never knows the input was Markdown. Swap in a CSV parser later
and stages 2 and 3 don't change a line. That separation is the whole point.
"""

import os
import re
import sys
import html
import shutil
from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# MODEL  - the intermediate representation everything else agrees on
# --------------------------------------------------------------------------
@dataclass
class Node:
    title: str
    parent: "Node | None" = None
    children: list = field(default_factory=list)
    slug: str = ""        # url-safe name for this page's folder
    out_path: str = ""    # where this page's index.html lands, relative to output root

@dataclass
class FooterColumn:
    label: str
    links: list = field(default_factory=list)  # page titles


# --------------------------------------------------------------------------
# 1. PARSE  - markdown nested list  ->  tree
# --------------------------------------------------------------------------
LIST_LINE = re.compile(r"^(\s*)[-*+]\s+(.*\S)\s*$")  # indent, then "- title"

def parse_markdown(text: str) -> Node:
    """
    Build a tree from an indented bullet list. The trick is a stack of
    (indent, node) pairs. For each line we pop everything whose indent is
    >= ours; whatever is left on top is, by definition, our parent. This
    works for 2-space, 4-space, or tab indentation without hardcoding a
    width, because parenthood is decided by *relative* indentation only.
    """
    root = Node(title="Home")          # synthetic root -> becomes index.html
    stack = [(-1, root)]               # root sits "below" any real indent

    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = LIST_LINE.match(raw)
        if not m:
            continue                    # ignore headings, prose, blank lines
        indent = len(m.group(1).expandtabs(4))
        title = m.group(2).strip()

        while stack[-1][0] >= indent:   # pop back to our parent
            stack.pop()
        parent = stack[-1][1]

        node = Node(title=title, parent=parent)
        parent.children.append(node)
        stack.append((indent, node))

    return root


FOOTER_HEADING = re.compile(r"^##\s+footer\s*$", re.IGNORECASE)

def parse_footer(text: str) -> list:
    """
    Extracts the ## Footer section and returns up to 4 FooterColumns.
    Top-level list items are column headers; their immediate children are link titles.
    """
    lines = text.splitlines()
    in_footer = False
    footer_lines = []
    for line in lines:
        if FOOTER_HEADING.match(line.strip()):
            in_footer = True
            continue
        if in_footer and re.match(r"^#", line):
            break
        if in_footer:
            footer_lines.append(line)

    columns: list[FooterColumn] = []
    for raw in footer_lines:
        m = LIST_LINE.match(raw)
        if not m:
            continue
        indent = len(m.group(1).expandtabs(4))
        title = m.group(2).strip()
        if indent == 0:
            if len(columns) == 4:
                break
            columns.append(FooterColumn(label=title))
        elif columns:
            columns[-1].links.append(title)

    return columns


# --------------------------------------------------------------------------
# 2. DERIVE  - tree  ->  slugs + output paths
# --------------------------------------------------------------------------
def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "page"

def assign_paths(root: Node) -> None:
    """
    The folder layout mirrors the tree, so a page's URL *is* its position in
    the sitemap: /products/widgets/index.html. Duplicate sibling titles get a
    numeric suffix so they never collide; pages under different parents can
    share a name freely because they live in different folders.
    """
    root.out_path = "index.html"

    def walk(node: Node, dir_parts: list[str]) -> None:
        used: dict[str, int] = {}
        for child in node.children:
            base = slugify(child.title)
            n = used.get(base, 0)
            used[base] = n + 1
            child.slug = base if n == 0 else f"{base}-{n + 1}"
            parts = dir_parts + [child.slug]
            child.out_path = "/".join(parts + ["index.html"])
            walk(child, parts)

    walk(root, [])


def build_title_index(root: Node) -> dict:
    """Flat map of title -> Node for resolving footer links."""
    index: dict[str, Node] = {}
    stack = list(root.children)
    while stack:
        n = stack.pop()
        index[n.title] = n
        stack.extend(n.children)
    return index

def resolve_footer_nodes(footer_cols: list, title_index: dict, root: Node) -> list:
    """
    For any footer link title not already in the sitemap, create a standalone
    Node with its own page. These pages are reachable from the footer but do
    not appear in the global nav.
    """
    used_slugs = {c.slug for c in root.children}
    extra: list[Node] = []
    for col in footer_cols:
        for lt in col.links:
            if lt in title_index:
                continue
            base = slugify(lt)
            slug, n = base, 1
            while slug in used_slugs:
                n += 1
                slug = f"{base}-{n}"
            used_slugs.add(slug)
            node = Node(title=lt, slug=slug, out_path=f"{slug}/index.html")
            extra.append(node)
            title_index[lt] = node
    return extra


# --------------------------------------------------------------------------
# 3. RENDER  - tree  ->  HTML on disk
# --------------------------------------------------------------------------
def ancestors(node: Node) -> list[Node]:
    chain, n = [], node
    while n is not None:
        chain.append(n)
        n = n.parent
    return list(reversed(chain))        # root ... node

def rel(from_path: str, to_path: str) -> str:
    """Relative link from one page file to another. os.path.relpath does the
    ../../ math for us so we never hand-build fragile paths."""
    from_dir = os.path.dirname(from_path) or "."
    return os.path.relpath(to_path, from_dir).replace(os.sep, "/")

def esc(s: str) -> str:
    return html.escape(s, quote=True)

def render_page(node: Node, root: Node, footer_cols: list = (), title_index: dict = {}) -> str:
    here = node.out_path
    css = rel(here, "style.css")
    home_link = rel(here, root.out_path)

    # global nav = the root's direct children; mark the active section
    anc = ancestors(node)
    section_of_node = anc[1] if len(anc) > 1 else None
    nav_items = "".join(
        f'<a class="{"active" if c is section_of_node else ""}" '
        f'href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
        for c in root.children
    )

    # breadcrumb = the ancestor chain, last one not a link
    crumbs = ancestors(node)
    crumb_html = " <span>/</span> ".join(
        esc(c.title) if c is node
        else f'<a href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
        for c in crumbs
    )

    # child links = this page's own children, shown as cards
    if node is root:
        children_block = ""
    elif node.children:
        cards = "".join(
            f'<a class="card" href="{esc(rel(here, c.out_path))}">'
            f'<span class="card-title">{esc(c.title)}</span>'
            f'<span class="card-arrow">&rarr;</span></a>'
            for c in node.children
        )
        children_block = f'<section class="children"><h2>In this section</h2><div class="grid">{cards}</div></section>'
    else:
        children_block = '<section class="children leaf"><h2>Leaf page</h2><p>No sub-pages. This is a destination.</p></section>'

    # footer columns: resolve link titles to URLs
    if footer_cols:
        col_htmls = []
        for col in footer_cols:
            items = "".join(
                f'<li><a href="{esc(rel(here, n.out_path) if (n := title_index.get(lt)) else "#")}">'
                f'{esc(lt)}</a></li>'
                for lt in col.links
            )
            col_htmls.append(
                f'<div class="footer-col"><h3>{esc(col.label)}</h3><ul>{items}</ul></div>'
            )
        footer_inner = (
            f'<div class="footer-cols">{"".join(col_htmls)}</div>'
            f'<div class="footer-path"><code>{esc(here)}</code></div>'
        )
    else:
        footer_inner = f'<code>{esc(here)}</code>'

    # deliberately low-fidelity placeholder body
    skeleton = (
        '<div class="wire wire-title"></div>'
        + ''.join('<div class="wire wire-line"></div>' for _ in range(4))
        + '<div class="wire wire-block"></div>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(node.title)} - prototype</title>
<link rel="stylesheet" href="{esc(css)}">
</head>
<body>
<header class="topbar">
  <a class="brand" href="{esc(home_link)}">SITEMAP PROTO</a>
  <nav class="globalnav">{nav_items}</nav>
</header>
<div class="page-banner">
  <h1>{esc(node.title)}</h1>
</div>
<div class="breadcrumb">{crumb_html}</div>
<main>
  {children_block}
  <section class="content">
    {skeleton}
  </section>
</main>
<footer>{footer_inner}</footer>
</body>
</html>
"""

STYLE = """
:root{
  --ink:#1d2430; --muted:#7a8699; --line:#cfd6e0;
  --paper:#f4f6f9; --card:#ffffff; --accent:#2f5fff; --wire:#dde3ec;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font:15px/1.5 ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
a{color:var(--accent); text-decoration:none}
.topbar{
  display:flex; align-items:center; gap:24px;
  padding:14px 22px; background:var(--ink); color:#fff;
  position:sticky; top:0; z-index:10;
}
.brand{font-weight:700; letter-spacing:.12em; font-size:12px; opacity:.85; color:#fff}
.globalnav{display:flex; gap:4px; flex-wrap:wrap}
.globalnav a{
  color:#cbd5e6; padding:6px 12px; border-radius:6px; font-size:13px;
}
.globalnav a:hover{background:rgba(255,255,255,.1); color:#fff}
.globalnav a.active{background:var(--accent); color:#fff}
.breadcrumb{
  padding:10px 22px; color:var(--muted); font-size:12px;
  border-bottom:1px solid var(--line); background:#fff;
}
.breadcrumb span{opacity:.5; margin:0 2px}
.page-banner{
  height:250px; display:flex; align-items:center;
  padding:0 22px; background:#141920;
  border-bottom:1px solid var(--line);
}
.page-banner h1{
  max-width:880px; width:100%; margin:0 auto;
  font-size:32px; color:#fff;
}
main{max-width:880px; margin:0 auto; padding:32px 22px}
h2{font-size:13px; text-transform:uppercase; letter-spacing:.1em; color:var(--muted); margin:0 0 14px}
.children{margin-bottom:40px}
.grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:12px}
.card{
  display:flex; justify-content:space-between; align-items:center;
  padding:16px 18px; background:var(--card); border:1px solid var(--line);
  border-radius:10px; font-size:14px; transition:.12s;
}
.card:hover{border-color:var(--accent); transform:translateY(-1px)}
.card-arrow{color:var(--muted)}
.leaf p{color:var(--muted)}
.content{border-top:1px dashed var(--line); padding-top:28px}
.wire{background:var(--wire); border-radius:5px; margin-bottom:12px}
.wire-title{height:26px; width:45%}
.wire-line{height:13px}
.wire-line:nth-child(3){width:92%}
.wire-line:nth-child(4){width:97%}
.wire-line:nth-child(5){width:70%}
.wire-block{height:160px; margin-top:18px}
footer{background:var(--ink); color:#fff; padding:48px 22px 32px}
.footer-cols{
  max-width:880px; margin:0 auto;
  display:grid; gap:32px;
  grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
}
.footer-col h3{
  font-size:11px; text-transform:uppercase; letter-spacing:.1em;
  color:rgba(255,255,255,.4); margin:0 0 12px;
}
.footer-col ul{list-style:none; padding:0; margin:0}
.footer-col li{margin-bottom:8px}
.footer-col a{color:#cbd5e6; font-size:13px}
.footer-col a:hover{color:#fff}
.footer-path{
  max-width:880px; margin:24px auto 0; padding-top:16px;
  border-top:1px solid rgba(255,255,255,.1);
  color:rgba(255,255,255,.3); font-size:11px;
}
"""


# --------------------------------------------------------------------------
# DRIVER
# --------------------------------------------------------------------------
def build(md_path: str, out_dir: str) -> int:
    with open(md_path, encoding="utf-8") as f:
        src = f.read()

    sitemap_src = re.split(r"^##\s+footer\s*$", src, maxsplit=1, flags=re.IGNORECASE | re.MULTILINE)[0]
    root = parse_markdown(sitemap_src)
    footer_cols = parse_footer(src)
    assign_paths(root)
    title_index = build_title_index(root)
    footer_only = resolve_footer_nodes(footer_cols, title_index, root)

    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)

    with open(os.path.join(out_dir, "style.css"), "w", encoding="utf-8") as f:
        f.write(STYLE)

    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        dest = os.path.join(out_dir, node.out_path)
        os.makedirs(os.path.dirname(dest) or out_dir, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(render_page(node, root, footer_cols, title_index))
        count += 1
        stack.extend(node.children)

    for node in footer_only:
        dest = os.path.join(out_dir, node.out_path)
        os.makedirs(os.path.dirname(dest) or out_dir, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(render_page(node, root, footer_cols, title_index))
        count += 1

    return count


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    md_path = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "prototype"
    n = build(md_path, out_dir)
    index = os.path.join(out_dir, "index.html")
    print(f"Built {n} pages -> {out_dir}/")
    print(f"Open: {os.path.abspath(index)}")


if __name__ == "__main__":
    main()
