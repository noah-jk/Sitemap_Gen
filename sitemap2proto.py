#!/usr/bin/env python3
"""
Sitemap_Gen - turn a Markdown nested-list sitemap into a clickable static prototype.

Usage:
    python3 sitemap2proto.py sitemap.md                          # -> ./prototype/
    python3 sitemap2proto.py sitemap.md out_dir                  # custom output folder
    python3 sitemap2proto.py sitemap.md --nav-style sidebar      # sidebar accordion layout
    python3 sitemap2proto.py sitemap.md --dropdown-depth 2       # 2-level flyout nav
    python3 sitemap2proto.py sitemap.md --mega-menu              # full-width column mega menu
    python3 sitemap2proto.py --help                              # full option list

The whole program is three stages with a hard wall between them:

    PARSE   markdown text            -> a tree of Node objects
    DERIVE  tree                     -> slugs, file paths (still no HTML)
    RENDER  tree                     -> static HTML files on disk

The renderer never knows the input was Markdown. Swap in a CSV parser later
and stages 2 and 3 don't change a line. That separation is the whole point.
"""

import os
import re
import html
import shutil
import argparse
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
    is_button: bool = False  # render as a button in the global nav

@dataclass
class FooterColumn:
    label: str
    links: list = field(default_factory=list)  # page titles


# --------------------------------------------------------------------------
# 1. PARSE  - markdown nested list  ->  tree
# --------------------------------------------------------------------------
LIST_LINE = re.compile(r"^(\s*)[-*+]\s+(.*\S)\s*$")  # indent, then "- title"
BUTTON_TAG = re.compile(r"\s*\[button\]\s*$", re.IGNORECASE)  # trailing [button] annotation

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
        raw_title = m.group(2).strip()
        is_button = bool(BUTTON_TAG.search(raw_title))
        title = BUTTON_TAG.sub("", raw_title).strip()

        while stack[-1][0] >= indent:   # pop back to our parent
            stack.pop()
        parent = stack[-1][1]

        node = Node(title=title, parent=parent, is_button=is_button)
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
        if in_footer and re.match(r"^##", line):
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

def _on_path_to(candidate: Node, target: Node) -> bool:
    """True if candidate is target or an ancestor of target."""
    n = target
    while n is not None:
        if n is candidate:
            return True
        n = n.parent
    return False

def _sidebar_items(nodes: list, here: str, current: Node) -> str:
    out = ""
    for n in nodes:
        cls = ' class="active"' if n is current else ""
        link = f'<a{cls} href="{esc(rel(here, n.out_path))}">{esc(n.title)}</a>'
        if n.children:
            open_attr = " open" if _on_path_to(n, current) else ""
            sub = _sidebar_items(n.children, here, current)
            out += f'<details{open_attr}><summary>{link}</summary>{sub}</details>'
        else:
            out += f'<div class="sidebar-leaf">{link}</div>'
    return out

def build_section_sidebar(section_root: Node, here: str, current: Node) -> str:
    cls = "sidebar-section-title active" if section_root is current else "sidebar-section-title"
    title = f'<a class="{cls}" href="{esc(rel(here, section_root.out_path))}">{esc(section_root.title)}</a>'
    return f'<aside class="sidebar">{title}{_sidebar_items(section_root.children, here, current)}</aside>'

def nav_dropdown(children: list, here: str, remaining_levels: int) -> str:
    """Recursively build <li> items for a dropdown or flyout panel."""
    items = ""
    for c in children:
        link = f'<a href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
        if remaining_levels > 1 and c.children:
            sub = nav_dropdown(c.children, here, remaining_levels - 1)
            items += f'<li class="has-flyout">{link}<ul class="flyout">{sub}</ul></li>'
        else:
            items += f'<li>{link}</li>'
    return items

def nav_mega_panel(top_node: Node, here: str) -> str:
    """Build the column-based mega-menu panel for one top-level nav item."""
    cols = ""
    for col_node in top_node.children:
        head = f'<a class="mega-col-head" href="{esc(rel(here, col_node.out_path))}">{esc(col_node.title)}</a>'
        if col_node.children:
            links = "".join(
                f'<li><a href="{esc(rel(here, lc.out_path))}">{esc(lc.title)}</a></li>'
                for lc in col_node.children
            )
            cols += f'<div class="mega-col">{head}<ul>{links}</ul></div>'
        else:
            cols += f'<div class="mega-col">{head}</div>'
    return f'<div class="mega-panel">{cols}</div>'

def render_page(node: Node, root: Node, footer_cols: list = (), title_index: dict = {}, nav_style: str = "grid", dropdown_depth: int = 0, mega_menu: bool = False) -> str:
    here = node.out_path
    css = rel(here, "style.css")
    home_link = rel(here, root.out_path)

    # global nav = the root's direct children; mark the active section
    anc = ancestors(node)
    section_of_node = anc[1] if len(anc) > 1 else None
    if mega_menu:
        nav_items = ""
        for c in root.children:
            classes = " ".join(filter(None, [
                "active" if c is section_of_node else "",
                "nav-btn" if c.is_button else "",
            ]))
            link = f'<a class="{classes}" href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
            if c.children:
                panel = nav_mega_panel(c, here)
                nav_items += f'<div class="nav-item has-mega">{link}{panel}</div>'
            else:
                nav_items += link
    elif dropdown_depth > 0:
        nav_items = ""
        for c in root.children:
            classes = " ".join(filter(None, [
                "active" if c is section_of_node else "",
                "nav-btn" if c.is_button else "",
            ]))
            link = f'<a class="{classes}" href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
            if c.children:
                sub = nav_dropdown(c.children, here, dropdown_depth)
                nav_items += f'<div class="nav-item has-dropdown">{link}<ul class="dropdown">{sub}</ul></div>'
            else:
                nav_items += link
    else:
        nav_items = "".join(
            f'<a class="{" ".join(filter(None, ["active" if c is section_of_node else "", "nav-btn" if c.is_button else ""])) }" '
            f'href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
            for c in root.children
        )

    # mobile nav: always 2 levels deep, details/summary accordion for items with children
    mobile_items = ""
    for c in root.children:
        cls = " ".join(filter(None, [
            "active" if c is section_of_node else "",
            "nav-btn" if c.is_button else "",
        ]))
        link_cls = f' class="{cls}"' if cls else ""
        link = f'<a{link_cls} href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
        if c.children:
            open_attr = " open" if c is section_of_node else ""
            children_html = "".join(
                f'<div class="mob-child"><a href="{esc(rel(here, gc.out_path))}">{esc(gc.title)}</a></div>'
                for gc in c.children
            )
            mobile_items += f'<details{open_attr}><summary>{link}</summary>{children_html}</details>'
        else:
            mobile_items += link
    mobile_nav_html = f'<nav class="mob-nav">{mobile_items}</nav>'

    # breadcrumb = the ancestor chain, last one not a link
    crumbs = ancestors(node)
    crumb_html = " <span>/</span> ".join(
        esc(c.title) if c is node
        else f'<a href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
        for c in crumbs
    )

    # child links / sidebar
    sidebar_html = ""
    if node is root:
        children_block = ""
    elif nav_style == "sidebar" and len(anc) > 1:
        children_block = ""
        sidebar_html = build_section_sidebar(anc[1], here, node)
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

    layout_open  = '<div class="page-layout">' if sidebar_html else ""
    layout_close = "</div>"                    if sidebar_html else ""

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
  <button class="hamburger" aria-label="Open menu" aria-expanded="false" aria-controls="mobile-menu">
    <span></span><span></span><span></span>
  </button>
</header>
<div class="page-banner">
  <h1>{esc(node.title)}</h1>
</div>
<div class="breadcrumb">{crumb_html}</div>
{layout_open}
{sidebar_html}
<main>
  {children_block}
  <section class="content">
    {skeleton}
  </section>
</main>
{layout_close}
<footer>{footer_inner}</footer>
<div class="mobile-menu" id="mobile-menu" aria-hidden="true">
  <button class="mob-close" aria-label="Close menu">&#x2715;</button>
  {mobile_nav_html}
</div>
<div class="mobile-overlay" id="mobile-overlay"></div>
<script>
(function(){{
  var btn=document.querySelector('.hamburger');
  var menu=document.getElementById('mobile-menu');
  var overlay=document.getElementById('mobile-overlay');
  function openMenu(){{menu.classList.add('open');overlay.classList.add('open');document.body.classList.add('menu-open');btn.setAttribute('aria-expanded','true');menu.setAttribute('aria-hidden','false');}}
  function closeMenu(){{menu.classList.remove('open');overlay.classList.remove('open');document.body.classList.remove('menu-open');btn.setAttribute('aria-expanded','false');menu.setAttribute('aria-hidden','true');}}
  btn.addEventListener('click',openMenu);
  overlay.addEventListener('click',closeMenu);
  document.querySelector('.mob-close').addEventListener('click',closeMenu);
}})();
</script>
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
  padding:0 22px; background:var(--ink); color:#fff;
  position:sticky; top:0; z-index:10;
}
.brand{font-weight:700; letter-spacing:.12em; font-size:12px; opacity:.85; color:#fff}
.globalnav{display:flex; gap:4px; flex-wrap:wrap; align-self:stretch; align-items:stretch}
.globalnav>a{display:flex; align-items:center}
.globalnav a{
  color:#cbd5e6; padding:14px 12px; font-size:13px;
}
.globalnav a:hover{background:rgba(255,255,255,.1); color:#fff}
.globalnav a.active{border-bottom:4px solid var(--accent); color:#fff;}
.globalnav a.nav-btn{background:var(--accent); color:#fff; padding:5px 14px}
.globalnav a.nav-btn:hover{background:rgba(255,255,255,.15); border-color:rgba(255,255,255,.7)}
.globalnav a.nav-btn.active{border-bottom:2px solid var(--accent)}
.nav-item{position:relative; display:flex; align-items:center}
.nav-item>.dropdown,.has-flyout>.flyout{
  display:none; position:absolute;
  background:#0d1117; border:1px solid rgba(255,255,255,.12);
  border-radius:8px; padding:6px 0; min-width:180px;
  list-style:none; margin:0; z-index:200;
}
.nav-item>.dropdown{top:100%; left:0}
.has-flyout>.flyout{left:100%; top:-6px;}
.has-flyout{position: relative;}
.nav-item:hover>.dropdown,.has-flyout:hover>.flyout{display:block;}
.dropdown a,.flyout a{display:block; padding:8px 16px; border-radius:0; white-space:nowrap}
.has-flyout>a::after{content:" ›"; opacity:.5; font-size:11px}
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
.page-layout{display:flex; align-items:flex-start}
.page-layout main{flex:1; max-width:none; min-width:0}
.sidebar{
  width:240px; flex-shrink:0; position:sticky; top:52px;
  height:calc(100vh - 52px); overflow-y:auto;
  border-right:1px solid var(--line); background:#fff;
}
.sidebar-section-title{
  display:block; padding:14px 20px 10px; font-weight:700; font-size:11px;
  text-transform:uppercase; letter-spacing:.1em; color:var(--muted);
  border-bottom:1px solid var(--line);
}
.sidebar-section-title.active{color:var(--accent)}
.sidebar details{border-bottom:1px solid var(--line)}
.sidebar details summary{
  list-style:none; padding:9px 20px; cursor:pointer; font-size:13px;
}
.sidebar details summary::-webkit-details-marker{display:none}
.sidebar details summary::after{content:" ›"; float:right; color:var(--muted)}
.sidebar details[open]>summary::after{content:" ↓"}
.sidebar details summary a{color:var(--ink)}
.sidebar details summary a:hover{color:var(--accent)}
.sidebar details summary a.active{color:var(--accent); font-weight:600}
.sidebar .sidebar-leaf{border-bottom:1px solid var(--line)}
.sidebar .sidebar-leaf a{display:block; padding:9px 20px; font-size:13px; color:var(--ink)}
.sidebar details .sidebar-leaf a{padding-left:32px}
.sidebar .sidebar-leaf a:hover{color:var(--accent)}
.sidebar .sidebar-leaf a.active{color:var(--accent); font-weight:600}
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
.hamburger{
  display:none; flex-direction:column; justify-content:space-between;
  width:22px; height:16px; background:none; border:none;
  cursor:pointer; padding:0; margin-left:auto; flex-shrink:0;
  margin-top: 10px; margin-bottom: 10px;
}
.hamburger span{display:block; height:2px; background:#fff; border-radius:2px}
.mobile-menu{
  position:fixed; top:0; right:0; height:100%; width:280px;
  background:var(--ink); z-index:1000; overflow-y:auto;
  transform:translateX(100%); transition:transform .25s ease;
}
.mobile-overlay{
  display:none; position:fixed; inset:0;
  background:rgba(0,0,0,.45); z-index:999;
}
.mobile-overlay.open{display:block}
.mob-close{
  display:block; margin-left:auto; padding:16px 20px;
  background:none; border:none; color:rgba(255,255,255,.6);
  font-size:18px; cursor:pointer; line-height:1;
}
.mob-nav a{
  display:block; padding:13px 22px; color:#cbd5e6; font-size:14px;
  border-bottom:1px solid rgba(255,255,255,.07);
}
.mob-nav a:hover,.mob-nav a.active{color:#fff}
.mob-nav details{border-bottom:1px solid rgba(255,255,255,.07)}
.mob-nav details summary{
  list-style:none; padding:13px 22px; color:#cbd5e6;
  font-size:14px; cursor:pointer;
}
.mob-nav details summary::-webkit-details-marker{display:none}
.mob-nav details summary::after{content:" ›"; float:right; opacity:.4}
.mob-nav details[open]>summary::after{content:" ↓"}
.mob-nav details summary a{color:inherit; display:inline}
.mob-nav .mob-child a{
  display:block; padding:10px 22px 10px 36px;
  font-size:13px; color:rgba(255,255,255,.55);
  border-bottom:1px solid rgba(255,255,255,.05);
}
.mob-nav .mob-child a:hover{color:#fff}
@media(max-width:768px){
  .globalnav{display:none}
  .hamburger{display:flex}
  body{transition:transform .25s ease}
  body.menu-open{transform:translateX(-280px); overflow:hidden}
  .page-banner h1{font-size:22px}
  .sidebar{display:none}
  .page-layout{display:block}
}
.nav-item.has-mega{position:static}
.mega-panel{
  display:none; position:absolute; top:100%; left:0; right:0;
  flex-direction:row; flex-wrap:wrap; gap:40px;
  background:#0d1117; border-bottom:1px solid rgba(255,255,255,.12);
  padding:28px 28px; z-index:200;
}
.has-mega:hover>.mega-panel{display:flex}
.mega-col{min-width:140px}
.mega-col-head{
  display:block; font-size:11px; font-weight:700;
  text-transform:uppercase; letter-spacing:.1em;
  color:rgba(255,255,255,.45); margin-bottom:10px;
}
.mega-col-head:hover{color:#fff}
.mega-col ul{list-style:none; padding:0; margin:0}
.mega-col li{margin-bottom:6px}
.mega-col a{color:#cbd5e6; font-size:13px}
.mega-col a:hover{color:#fff}
@media(max-width:768px){
  .mega-panel{display:none!important}
}
"""


# --------------------------------------------------------------------------
# DRIVER
# --------------------------------------------------------------------------
def build(md_path: str, out_dir: str, nav_style: str = "grid", dropdown_depth: int = 0, mega_menu: bool = False) -> int:
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
            f.write(render_page(node, root, footer_cols, title_index, nav_style, dropdown_depth, mega_menu))
        count += 1
        stack.extend(node.children)

    for node in footer_only:
        dest = os.path.join(out_dir, node.out_path)
        os.makedirs(os.path.dirname(dest) or out_dir, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(render_page(node, root, footer_cols, title_index, nav_style, dropdown_depth, mega_menu))
        count += 1

    return count


def main() -> None:
    ap = argparse.ArgumentParser(description="Turn a Markdown sitemap into a clickable static prototype.")
    ap.add_argument("sitemap", help="path to sitemap .md file")
    ap.add_argument("out_dir", nargs="?", default="prototype", help="output directory (default: prototype)")
    ap.add_argument("--nav-style", choices=["grid", "sidebar"], default="grid",
                    help="render 'In this section' as a card grid (default) or sidebar accordion")
    ap.add_argument("--dropdown-depth", type=int, default=0, metavar="N",
                    help="levels of dropdown flyout menus in the global nav (0 = flat, default)")
    ap.add_argument("--mega-menu", action="store_true",
                    help="full-width mega menu with columns (alternative to --dropdown-depth)")
    args = ap.parse_args()
    n = build(args.sitemap, args.out_dir, args.nav_style, args.dropdown_depth, args.mega_menu)
    index = os.path.join(args.out_dir, "index.html")
    print(f"Built {n} pages -> {args.out_dir}/")
    print(f"Open: {os.path.abspath(index)}")


if __name__ == "__main__":
    main()
