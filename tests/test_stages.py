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


def resolved(md):
    root = sp.parse_markdown(md)
    sp.assign_paths(root)
    return root


def test_footer_titles_not_in_sitemap_get_standalone_pages():
    root = resolved("- Privacy\n")
    footer, _, standalone = sp.resolve_links([sp.FooterColumn("Legal", ["Privacy", "Terms"])], [], root)
    assert [(n.title, n.out_path) for n in standalone] == [("Terms", "terms/index.html")]
    assert footer[0].links[0] is root.children[0]
    assert footer[0].node is None


def test_standalone_page_slug_avoids_top_level_folders():
    root = resolved("- Help Desk\n")
    _, _, standalone = sp.resolve_links([sp.FooterColumn("X", ["Help-Desk"])], [], root)
    assert standalone[0].out_path == "help-desk-2/index.html"


def test_footer_and_utility_standalone_pages_never_share_a_folder():
    root = resolved("- Home Page\n")
    _, _, standalone = sp.resolve_links([sp.FooterColumn("X", ["Help!"])], [sp.UtilityNavItem("Help")], root)
    assert sorted(n.out_path for n in standalone) == ["help-2/index.html", "help/index.html"]


def test_utility_label_and_children_resolve_to_pages():
    root = resolved("- Help\n")
    _, utility, standalone = sp.resolve_links([], [sp.UtilityNavItem("Help", ["FAQs"]), sp.UtilityNavItem("Login")], root)
    assert utility[0].node is root.children[0]
    assert [n.title for n in utility[0].links] == ["FAQs"]
    assert [n.title for n in standalone] == ["Login", "FAQs"]


def test_footer_link_to_duplicate_title_prefers_shallowest_page():
    root = resolved("- A\n  - X\n    - Contact\n- Contact\n")
    assert sp.build_title_index(root)["Contact"] is root.children[1]


def test_footer_link_to_duplicate_title_at_same_depth_prefers_document_order():
    root = resolved("- A\n  - Contact\n- B\n  - Contact\n")
    assert sp.build_title_index(root)["Contact"] is root.children[0].children[0]


def test_settings_validation():
    with pytest.raises(ValueError):
        sp.Settings(nav_style="tabs")
    with pytest.raises(ValueError):
        sp.Settings(dropdown_depth=-1)
    with pytest.raises(ValueError):
        sp.Settings(mega_menu=True, dropdown_depth=2)


# ---- render ----------------------------------------------------------------

def test_rel_links_are_relative_between_folders():
    assert sp.rel("a/b/index.html", "index.html") == "../../index.html"
    assert sp.rel("index.html", "a/index.html") == "a/index.html"
    assert sp.rel("a/b/index.html", "a/c/index.html") == "../c/index.html"


def test_render_escapes_titles():
    root = sp.parse_markdown('- <script>alert("x")</script>\n')
    sp.assign_paths(root)
    page = sp.render_page(root.children[0], sp.Site(root))
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page


def test_visual_sitemap_embeds_tree_json_safely():
    root = sp.parse_markdown("- </script><b>x\n")
    sp.assign_paths(root)
    html = sp.render_visual_sitemap(root)
    assert html.count("</script>") == 1  # only the page's own closing tag
    assert "<b>x" not in html


# ---- output ----------------------------------------------------------------

def test_render_site_lists_every_file():
    site = sp.site_from_markdown("- A\n  - B\n## Footer\n- Col\n  - Terms\n")
    files = sp.render_site(site)
    assert sorted(files) == ["a/b/index.html", "a/index.html", "index.html", "sitemap.html",
                             "style.css", "terms/index.html"]


def test_zip_is_deterministic_and_complete():
    import io, zipfile
    files = sp.render_site(sp.site_from_markdown("- A\n"))
    data = sp.write_zip(files)
    assert data == sp.write_zip(dict(reversed(list(files.items()))))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == sorted(files)
        assert zf.read("a/index.html").decode() == files["a/index.html"]


def test_write_dir_refuses_to_wipe_unrelated_folder(tmp_path):
    (tmp_path / "notes.txt").write_text("keep me")
    with pytest.raises(FileExistsError):
        sp.write_dir({"index.html": "x"}, str(tmp_path))
    assert (tmp_path / "notes.txt").read_text() == "keep me"


def test_write_dir_replaces_previous_build(tmp_path):
    out = tmp_path / "proto"
    sp.write_dir(sp.render_site(sp.site_from_markdown("- Old\n")), str(out))
    sp.write_dir(sp.render_site(sp.site_from_markdown("- New\n")), str(out))
    assert (out / "new/index.html").exists() and not (out / "old").exists()


def test_noindex_marks_every_page_and_adds_robots_txt():
    site = sp.site_from_markdown("- A\n")
    plain = sp.render_site(site)
    hidden = sp.render_site(site, sp.Settings(noindex=True))
    assert "robots.txt" not in plain and 'name="robots"' not in plain["a/index.html"]
    assert hidden["robots.txt"] == "User-agent: *\nDisallow: /\n"
    for name in ("index.html", "a/index.html", "sitemap.html"):
        assert '<meta name="robots" content="noindex, nofollow">' in hidden[name]


def test_more_below_note_on_parent_page():
    site = sp.site_from_markdown("- News\n  - Latest\n- Events\n")
    news, events = site.root.children
    news.more_below, events.more_below = 392, 1
    files = sp.render_site(site)
    assert "Plus 392 more pages below this one" in files[news.out_path]
    assert "Plus 1 more page below this one" in files[events.out_path]
    assert "Leaf page" not in files[events.out_path]


def test_separate_main_menu_with_overrides_and_headings():
    site = sp.site_from_markdown("- Admissions\n  - How to Apply\n- About\n  - Contact Us\n- Landing Page\n")
    admissions, about, landing = site.root.children
    apply = admissions.children[0]
    nav = sp.Node(title="")
    apply_entry = sp.Node(title="Apply", parent=nav, out_path=apply.out_path, is_button=True)
    group = sp.Node(title="Our College", parent=nav, out_path="")
    group.children = [sp.Node(title="About", parent=group, out_path=about.out_path)]
    nav.children = [apply_entry, group]
    site.nav_root = nav
    files = sp.render_site(site, sp.Settings(dropdown_depth=1))
    page = files[apply.out_path]
    assert '<a class="active nav-btn" href="index.html">Apply</a>' in page   # active on its own page
    assert '<a class="">Our College</a>' in page                           # heading: no link
    assert "Landing Page</a>" not in page.split('<nav class="globalnav">')[1].split("</nav>")[0]
    assert "landing-page/index.html" in files                                # still a real page
    contact = files[about.children[0].out_path]
    assert '<a class="active">Our College</a>' not in contact and 'class="active"' not in contact.split("globalnav")[1][:400]


@pytest.mark.parametrize("page_type,expect,absent", [
    ("landing", ['<span class="page-type">Landing page</span>', 'class="card"', "wf-cta"], ["wf-rows"]),
    ("listing", ["wf-filters", "wf-rows", "wf-pager", "394 items", ">Spring &lt;Gala&gt;</a>"], ["In this section"]),
    ("list", ['<ul class="wf-links">', ">Spring &lt;Gala&gt;</a>"], ["wf-filters"]),
    ("article", ["wf-cols", "<h2>Related</h2>"], ["Leaf page"]),
    ("form", ["wf-form", "wf-field", ">Submit</span>"], ["wf-rows"]),
])
def test_page_type_wireframes(page_type, expect, absent):
    site = sp.site_from_markdown("- Events\n  - Spring <Gala>\n  - Fall Fair\n")
    events = site.root.children[0]
    events.page_type, events.more_below = page_type, 392
    page = sp.render_site(site)[events.out_path]
    for text in expect:
        assert text in page, text
    for text in absent:
        assert text not in page, text
    assert "<Gala>" not in page


def test_untyped_and_home_pages_keep_the_default_layout():
    site = sp.site_from_markdown("- A\n")
    site.root.page_type = "landing"           # the home page never takes a type
    files = sp.render_site(site)
    assert "page-type" not in files["index.html"].split("</style>")[-1].split("<main>")[0]
    assert "Leaf page" in files["a/index.html"] and 'class="page-type"' not in files["a/index.html"]
