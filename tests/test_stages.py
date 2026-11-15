import pytest

import sitemap2proto as sp


def titles(node):
    return [c.title for c in node.children]


# ---- parse -----------------------------------------------------------------

def test_parse_builds_hierarchy_from_relative_indent():
    root = sp.parse_markdown("- A\n  - A1\n    - A1a\n  - A2\n- B\n")
    assert titles(root) == ["A", "B"]
    assert titles(root.children[0]) == ["A1", "A2"]
    assert titles(root.children[0].children[0]) == ["A1a"]
    assert root.children[0].children[0].children[0].parent.title == "A1"


@pytest.mark.parametrize("indent", ["  ", "    ", "\t"])
def test_parse_accepts_any_consistent_indent(indent):
    root = sp.parse_markdown(f"- A\n{indent}- B\n{indent}{indent}- C\n")
    assert root.children[0].children[0].children[0].title == "C"


def test_parse_ignores_non_list_lines():
    root = sp.parse_markdown("# heading\nsome prose\n\n- A\n<!-- comment -->\n- B\n")
    assert titles(root) == ["A", "B"]


def test_parse_strips_button_tag_case_insensitively():
    root = sp.parse_markdown("- Give [Button]\n- Plain\n")
    assert [(c.title, c.is_button) for c in root.children] == [("Give", True), ("Plain", False)]


def test_extract_section_removes_it_in_any_order():
    src = "- A\n## utility nav\n- Help\n## Footer\n- Col\n  - Link\n"
    rest, util = sp._extract_section(src, "Utility Nav")
    rest, footer = sp._extract_section(rest, "Footer")
    assert "Help" in util and "Help" not in rest
    assert "Link" in footer and "Link" not in rest
    assert sp._extract_section("- A\n", "Footer") == ("- A\n", "")


def test_footer_keeps_at_most_four_columns():
    cols = sp.parse_footer("".join(f"- C{i}\n  - L{i}\n" for i in range(6)))
    assert [c.label for c in cols] == ["C0", "C1", "C2", "C3"]
    assert cols[0].links == ["L0"]


def test_utility_nav_children():
    items = sp.parse_utility_nav("- Help\n  - FAQs\n- Login\n")
    assert [(i.label, i.children) for i in items] == [("Help", ["FAQs"]), ("Login", [])]


# ---- derive ----------------------------------------------------------------

def test_paths_mirror_tree_and_suffix_duplicate_siblings():
    root = sp.parse_markdown("- About Us\n  - Team\n  - Team\n- Other\n  - Team\n")
    sp.assign_paths(root)
    about, other = root.children
    assert root.out_path == "index.html"
    assert [c.out_path for c in about.children] == ["about-us/team/index.html", "about-us/team-2/index.html"]
    assert other.children[0].out_path == "other/team/index.html"


def test_slugify_falls_back_for_symbol_only_titles():
    assert sp.slugify("Majors & Minors!") == "majors-minors"
    assert sp.slugify("&&&") == "page"


def test_footer_titles_not_in_sitemap_get_standalone_pages():
    root = sp.parse_markdown("- Privacy\n")
    sp.assign_paths(root)
    index = sp.build_title_index(root)
    extra = sp.resolve_footer_nodes([sp.FooterColumn("Legal", ["Privacy", "Terms"])], index, root)
    assert [(n.title, n.out_path) for n in extra] == [("Terms", "terms/index.html")]
    assert index["Privacy"] is root.children[0]


def test_standalone_page_slug_avoids_top_level_folders():
    root = sp.parse_markdown("- Help Desk\n")
    sp.assign_paths(root)
    extra = sp.resolve_footer_nodes([sp.FooterColumn("X", ["Help-Desk"])], sp.build_title_index(root), root)
    assert extra[0].out_path == "help-desk-2/index.html"


@pytest.mark.xfail(strict=True, reason="title index lets a deeper duplicate overwrite a shallower one")
def test_footer_link_to_duplicate_title_prefers_shallowest_page():
    root = sp.parse_markdown("- A\n  - X\n    - Contact\n- Contact\n")
    sp.assign_paths(root)
    index = sp.build_title_index(root)
    assert index["Contact"] is root.children[1]


def test_footer_link_to_duplicate_title_at_same_depth_prefers_document_order():
    root = sp.parse_markdown("- A\n  - Contact\n- B\n  - Contact\n")
    sp.assign_paths(root)
    index = sp.build_title_index(root)
    assert index["Contact"] is root.children[0].children[0]


# ---- render ----------------------------------------------------------------

def test_rel_links_are_relative_between_folders():
    assert sp.rel("a/b/index.html", "index.html") == "../../index.html"
    assert sp.rel("index.html", "a/index.html") == "a/index.html"
    assert sp.rel("a/b/index.html", "a/c/index.html") == "../c/index.html"


def test_render_escapes_titles():
    root = sp.parse_markdown('- <script>alert("x")</script>\n')
    sp.assign_paths(root)
    page = sp.render_page(root.children[0], root)
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page


def test_visual_sitemap_embeds_tree_json_safely():
    root = sp.parse_markdown("- </script><b>x\n")
    sp.assign_paths(root)
    html = sp.render_visual_sitemap(root)
    assert html.count("</script>") == 2  # only the page's own closing tags
