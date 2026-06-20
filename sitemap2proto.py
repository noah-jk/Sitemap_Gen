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
import json
import shutil
import argparse
import functools
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


# --------------------------------------------------------------------------
# MODEL  - the intermediate representation everything else agrees on
# --------------------------------------------------------------------------
@dataclass
class Node:
    title: str
    parent: "Node | None" = None
    children: List["Node"] = field(default_factory=list)
    slug: str = ""        # url-safe name for this page's folder
    out_path: str = ""    # where this page's index.html lands, relative to output root
    is_button: bool = False  # render as a button in the global nav

@dataclass
class FooterColumn:
    label: str
    links: List[str] = field(default_factory=list)


def _tree_to_dict(node: Node) -> Dict[str, object]:
    return {
        "title": node.title,
        "path": node.out_path,
        "children": [_tree_to_dict(c) for c in node.children],
    }


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


def parse_footer(text: str) -> List[FooterColumn]:
    """
    Parse the already-extracted footer block (text after the ## Footer heading).
    Returns up to 4 FooterColumns; top-level items are column headers, children are links.
    """
    columns: list[FooterColumn] = []
    for raw in text.splitlines():
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


def build_title_index(root: Node) -> Dict[str, Node]:
    """Flat map of title -> Node for resolving footer links."""
    index: dict[str, Node] = {}
    stack = list(root.children)
    while stack:
        n = stack.pop()
        index[n.title] = n
        stack.extend(n.children)
    return index

def resolve_footer_nodes(footer_cols: List[FooterColumn], title_index: Dict[str, Node], root: Node) -> List[Node]:
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

esc = functools.partial(html.escape, quote=True)

def _on_path_to(candidate: Node, target: Node) -> bool:
    """True if candidate is target or an ancestor of target."""
    n = target
    while n is not None:
        if n is candidate:
            return True
        n = n.parent
    return False

def _sidebar_items(nodes: List[Node], here: str, current: Node) -> str:
    parts = []
    for n in nodes:
        cls = ' class="active"' if n is current else ""
        link = f'<a{cls} href="{esc(rel(here, n.out_path))}">{esc(n.title)}</a>'
        if n.children:
            open_attr = " open" if _on_path_to(n, current) else ""
            sub = _sidebar_items(n.children, here, current)
            parts.append(f'<details{open_attr}><summary>{link}</summary>{sub}</details>')
        else:
            parts.append(f'<div class="sidebar-leaf">{link}</div>')
    return "".join(parts)

def build_section_sidebar(section_root: Node, here: str, current: Node) -> str:
    cls = "sidebar-section-title active" if section_root is current else "sidebar-section-title"
    title = f'<a class="{cls}" href="{esc(rel(here, section_root.out_path))}">{esc(section_root.title)}</a>'
    return f'<aside class="sidebar">{title}{_sidebar_items(section_root.children, here, current)}</aside>'

def nav_dropdown(children: List[Node], here: str, remaining_levels: int) -> str:
    """Recursively build <li> items for a dropdown or flyout panel."""
    items = []
    for c in children:
        link = f'<a href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
        if remaining_levels > 1 and c.children:
            sub = nav_dropdown(c.children, here, remaining_levels - 1)
            items.append(f'<li class="has-flyout">{link}<ul class="flyout">{sub}</ul></li>')
        else:
            items.append(f'<li>{link}</li>')
    return "".join(items)

def nav_mega_panel(top_node: Node, here: str) -> str:
    """Build the column-based mega-menu panel for one top-level nav item."""
    cols = []
    for col_node in top_node.children:
        head = f'<a class="mega-col-head" href="{esc(rel(here, col_node.out_path))}">{esc(col_node.title)}</a>'
        if col_node.children:
            links = "".join(
                f'<li><a href="{esc(rel(here, lc.out_path))}">{esc(lc.title)}</a></li>'
                for lc in col_node.children
            )
            cols.append(f'<div class="mega-col">{head}<ul>{links}</ul></div>')
        else:
            cols.append(f'<div class="mega-col">{head}</div>')
    return f'<div class="mega-panel">{"".join(cols)}</div>'

def _build_footer_inner(footer_cols: Sequence[FooterColumn], here: str, title_index: Dict[str, Node]) -> str:
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
        return (
            f'<div class="footer-cols">{"".join(col_htmls)}</div>'
            f'<div class="footer-path"><code>{esc(here)}</code></div>'
        )
    return f'<code>{esc(here)}</code>'

def _build_mobile_nav(root: Node, here: str, section_of_node: Optional[Node]) -> str:
    mob_parts = []
    for c in root.children:
        cls = _nav_classes(c, section_of_node)
        link_cls = f' class="{cls}"' if cls else ""
        link = f'<a{link_cls} href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
        if c.children:
            open_attr = " open" if c is section_of_node else ""
            children_html = "".join(
                f'<div class="mob-child"><a href="{esc(rel(here, gc.out_path))}">{esc(gc.title)}</a></div>'
                for gc in c.children
            )
            mob_parts.append(f'<details{open_attr}><summary>{link}</summary>{children_html}</details>')
        else:
            mob_parts.append(link)
    return f'<nav class="mob-nav">{"".join(mob_parts)}</nav>'

def _build_children_block(node: Node, root: Node, here: str, anc: List[Node], nav_style: str) -> Tuple[str, str, str, str]:
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
    return sidebar_html, children_block, layout_open, layout_close

def _build_global_nav(root: Node, here: str, section_of_node: Optional[Node], mega_menu: bool, dropdown_depth: int) -> str:
    if mega_menu:
        nav_parts = []
        for c in root.children:
            classes = _nav_classes(c, section_of_node)
            link = f'<a class="{classes}" href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
            if c.children:
                panel = nav_mega_panel(c, here)
                nav_parts.append(f'<div class="nav-item has-mega">{link}{panel}</div>')
            else:
                nav_parts.append(link)
        return "".join(nav_parts)
    elif dropdown_depth > 0:
        nav_parts = []
        for c in root.children:
            classes = _nav_classes(c, section_of_node)
            link = f'<a class="{classes}" href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
            if c.children:
                sub = nav_dropdown(c.children, here, dropdown_depth)
                nav_parts.append(f'<div class="nav-item has-dropdown">{link}<ul class="dropdown">{sub}</ul></div>')
            else:
                nav_parts.append(link)
        return "".join(nav_parts)
    else:
        return "".join(
            f'<a class="{_nav_classes(c, section_of_node)}" '
            f'href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
            for c in root.children
        )

def _nav_classes(node: Node, active_node: Optional[Node]) -> str:
    return " ".join(filter(None, [
        "active" if node is active_node else "",
        "nav-btn" if node.is_button else "",
    ]))

def render_page(node: Node, root: Node, footer_cols: Sequence[FooterColumn] = (), title_index: Optional[Dict[str, Node]] = None, nav_style: str = "grid", dropdown_depth: int = 0, mega_menu: bool = False) -> str:
    title_index = title_index if title_index is not None else {}
    here = node.out_path
    css = rel(here, "style.css")
    home_link = rel(here, root.out_path)

    anc = ancestors(node)
    section_of_node = anc[1] if len(anc) > 1 else None
    nav_items = _build_global_nav(root, here, section_of_node, mega_menu, dropdown_depth)

    mobile_nav_html = _build_mobile_nav(root, here, section_of_node)

    # breadcrumb = the ancestor chain, last one not a link
    crumbs = anc
    crumb_html = " <span>/</span> ".join(
        esc(c.title) if c is node
        else f'<a href="{esc(rel(here, c.out_path))}">{esc(c.title)}</a>'
        for c in crumbs
    )

    sidebar_html, children_block, layout_open, layout_close = _build_children_block(node, root, here, anc, nav_style)

    footer_inner = _build_footer_inner(footer_cols, here, title_index)

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


VISUAL_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Visual Sitemap</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f4f6f9;font:13px/1.4 ui-monospace,"SF Mono",Menlo,Consolas,monospace;color:#1d2430}
.toolbar{display:flex;align-items:center;gap:20px;padding:0 24px;height:48px;background:#1d2430;color:#fff;position:sticky;top:0;z-index:10}
.toolbar h1{margin:0;font-size:12px;font-weight:700;letter-spacing:.12em;color:#fff}
.toolbar a{color:#8fa3c8;text-decoration:none;font-size:12px}
.toolbar a:hover{color:#fff}
.canvas-wrap{overflow:auto;padding:28px 32px;min-height:calc(100vh - 48px)}
canvas{display:block}
</style>
</head>
<body>
<div class="toolbar">
  <h1>VISUAL SITEMAP</h1>
  <a href="index.html">&#8592; Back to prototype</a>
</div>
<div class="canvas-wrap"><canvas id="sitemap-canvas"></canvas></div>
<script>
const TREE = __TREE_JSON__;

const NW = 150, NH = 34, RADIUS = 6;
const INDENT = 28, ROW_GAP = 10, COL_GAP = 60;
const HOME_TO_L1 = 52;
const PAD = 32;

const PALETTE = [
  {bg:'#1d2430',border:'#1d2430',text:'#ffffff'},
  {bg:'#2f5fff',border:'#1a3fd4',text:'#ffffff'},
  {bg:'#e8edff',border:'#99b0f0',text:'#1d2430'},
  {bg:'#f0f4ff',border:'#c0cef0',text:'#1d2430'},
];

function colorFor(depth) {
  if (depth < 0)  return PALETTE[0];
  if (depth === 0) return PALETTE[1];
  if (depth === 1) return PALETTE[2];
  return PALETTE[3];
}

function flatten(node, depth, out) {
  out.push({node, depth});
  for (const child of node.children) flatten(child, depth + 1, out);
}

function computeLayout(tree) {
  const l1list = tree.children;
  const colData = l1list.map(l1 => {
    const rows = [];
    flatten(l1, 0, rows);
    const maxD = rows.reduce((m, r) => Math.max(m, r.depth), 0);
    return {rows, colW: NW + maxD * INDENT};
  });

  let cx = PAD;
  const colXs = colData.map(cd => { const x = cx; cx += cd.colW + COL_GAP; return x; });
  const totalColsW = cx - COL_GAP;

  const homeX = Math.round((PAD + totalColsW) / 2 - NW / 2);
  const homeY = PAD;
  const L1_Y = homeY + NH + HOME_TO_L1;

  const nodes = [];
  nodes.push({node: tree, x: homeX, y: homeY, depth: -1, isRoot: true});

  for (let i = 0; i < colData.length; i++) {
    let y = L1_Y;
    for (const row of colData[i].rows) {
      nodes.push({node: row.node, x: colXs[i] + row.depth * INDENT, y, depth: row.depth});
      y += NH + ROW_GAP;
    }
  }

  const totalW = Math.max(totalColsW, homeX + NW) + PAD;
  const totalH = Math.max(...nodes.map(n => n.y + NH)) + PAD;
  return {nodes, totalW, totalH};
}

function rrect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x+r,y); ctx.lineTo(x+w-r,y); ctx.arcTo(x+w,y,x+w,y+r,r);
  ctx.lineTo(x+w,y+h-r); ctx.arcTo(x+w,y+h,x+w-r,y+h,r);
  ctx.lineTo(x+r,y+h); ctx.arcTo(x,y+h,x,y+h-r,r);
  ctx.lineTo(x,y+r); ctx.arcTo(x,y,x+r,y,r);
  ctx.closePath();
}

const HIT = [];

function render() {
  const {nodes, totalW, totalH} = computeLayout(TREE);
  const dpr = window.devicePixelRatio || 1;
  const canvas = document.getElementById('sitemap-canvas');
  canvas.width = totalW * dpr; canvas.height = totalH * dpr;
  canvas.style.width = totalW + 'px'; canvas.style.height = totalH + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, totalW, totalH);

  const byNode = new Map(nodes.map(n => [n.node, n]));

  // draw connectors first (below boxes)
  ctx.strokeStyle = '#9bafc8'; ctx.lineWidth = 1.5; ctx.lineCap = 'round';

  for (const item of nodes) {
    if (item.isRoot) {
      const l1items = item.node.children.map(ch => byNode.get(ch)).filter(Boolean);
      if (!l1items.length) continue;
      const hcx = item.x + NW / 2;
      const hby = item.y + NH;
      const busY = hby + HOME_TO_L1 / 2;
      // vertical stem from home down to bus
      ctx.beginPath(); ctx.moveTo(hcx, hby); ctx.lineTo(hcx, busY); ctx.stroke();
      if (l1items.length > 1) {
        // horizontal bus spanning all L1 columns
        const firstCX = l1items[0].x + NW / 2;
        const lastCX  = l1items[l1items.length - 1].x + NW / 2;
        ctx.beginPath(); ctx.moveTo(firstCX, busY); ctx.lineTo(lastCX, busY); ctx.stroke();
        // vertical drops to each L1
        for (const l1 of l1items) {
          ctx.beginPath(); ctx.moveTo(l1.x + NW / 2, busY); ctx.lineTo(l1.x + NW / 2, l1.y); ctx.stroke();
        }
      } else {
        ctx.beginPath(); ctx.moveTo(hcx, busY); ctx.lineTo(l1items[0].x + NW / 2, l1items[0].y); ctx.stroke();
      }
      continue;
    }

    if (!item.node.children.length) continue;
    const children = item.node.children.map(ch => byNode.get(ch)).filter(Boolean);
    if (!children.length) continue;

    // folder-tree connector: vertical stem + horizontal stubs
    const stemX = item.x + 12;
    const lastChild = children[children.length - 1];
    ctx.beginPath(); ctx.moveTo(stemX, item.y + NH); ctx.lineTo(stemX, lastChild.y + NH / 2); ctx.stroke();
    for (const ch of children) {
      ctx.beginPath(); ctx.moveTo(stemX, ch.y + NH / 2); ctx.lineTo(ch.x, ch.y + NH / 2); ctx.stroke();
    }
  }

  // draw node boxes
  ctx.textBaseline = 'middle';
  ctx.font = '12px ui-monospace,"SF Mono",Menlo,Consolas,monospace';
  HIT.length = 0;

  for (const item of nodes) {
    const col = colorFor(item.depth);
    rrect(ctx, item.x, item.y, NW, NH, RADIUS);
    ctx.fillStyle = col.bg; ctx.fill();
    ctx.strokeStyle = col.border; ctx.lineWidth = 1.5; ctx.stroke();

    ctx.fillStyle = col.text;
    const maxTextW = NW - 20;
    let lbl = item.node.title;
    if (ctx.measureText(lbl).width > maxTextW) {
      while (lbl.length > 1 && ctx.measureText(lbl + '…').width > maxTextW) lbl = lbl.slice(0, -1);
      lbl += '…';
    }
    ctx.fillText(lbl, item.x + 10, item.y + NH / 2);
    if (item.node.path) HIT.push({x: item.x, y: item.y, path: item.node.path});
  }

  canvas.onclick = function(e) {
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    for (const h of HIT) {
      if (mx >= h.x && mx <= h.x + NW && my >= h.y && my <= h.y + NH) {
        window.location.href = h.path; return;
      }
    }
  };
  canvas.onmousemove = function(e) {
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    canvas.style.cursor = HIT.some(h => mx >= h.x && mx <= h.x + NW && my >= h.y && my <= h.y + NH)
      ? 'pointer' : 'default';
  };
}

render();
</script>
</body>
</html>
"""


def render_visual_sitemap(root: Node) -> str:
    return VISUAL_HTML.replace("__TREE_JSON__", json.dumps(_tree_to_dict(root), ensure_ascii=False))


# --------------------------------------------------------------------------
# DRIVER
# --------------------------------------------------------------------------
def build(md_path: str, out_dir: str, nav_style: str = "grid", dropdown_depth: int = 0, mega_menu: bool = False) -> int:
    with open(md_path, encoding="utf-8") as f:
        src = f.read()

    parts = re.split(r"^##\s+footer\s*$", src, maxsplit=1, flags=re.IGNORECASE | re.MULTILINE)
    sitemap_src = parts[0]
    footer_src = parts[1] if len(parts) > 1 else ""
    root = parse_markdown(sitemap_src)
    footer_cols = parse_footer(footer_src)
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

    with open(os.path.join(out_dir, "sitemap.html"), "w", encoding="utf-8") as f:
        f.write(render_visual_sitemap(root))

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
    if args.mega_menu and args.dropdown_depth > 0:
        ap.error("--mega-menu and --dropdown-depth are mutually exclusive")
    n = build(args.sitemap, args.out_dir, args.nav_style, args.dropdown_depth, args.mega_menu)
    index = os.path.join(args.out_dir, "index.html")
    visual = os.path.join(args.out_dir, "sitemap.html")
    print(f"Built {n} pages -> {args.out_dir}/")
    print(f"Open:   {os.path.abspath(index)}")
    print(f"Visual: {os.path.abspath(visual)}")


if __name__ == "__main__":
    main()
