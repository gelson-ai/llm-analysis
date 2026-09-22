"""Cross-file invariants for the second (image-generation) dashboard page.

Nothing here tests a single module in isolation. These lock down the promises
that only hold across files, and that a future edit could quietly break:

  * the image template's styling is a *verbatim copy* of the chat template's,
    so the two pages cannot drift apart into different-looking products;
  * navigation is rendered from dashboard/nav_tabs.py rather than hardcoded per
    template, and every tab is both served locally and staged on deploy, so a
    tab can never 404 and a third (video) dashboard is one entry in that file;
  * both pages receive the same injected shell, so their nav bar and theme
    toggle cannot drift;
  * serve.py's original routes still point at the original file;
  * the new build writes to a path no frozen chat-side surface is watching.

tests/test_media_snapshot_paths.py is the precedent for this style: import the
real modules and assert against them, rather than reimplementing their logic.
"""
import hashlib
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import build_dashboard  # noqa: E402  (imported read-only - never modified)
import build_image_dashboard  # noqa: E402
import nav_tabs  # noqa: E402  (the shared tab list - single source of truth)
import page_shell  # noqa: E402
import serve  # noqa: E402

CHAT_TEMPLATE_PATH = DASHBOARD_DIR / "template.html"
IMAGE_TEMPLATE_PATH = DASHBOARD_DIR / "image_template.html"
PUBLISH_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "publish.yml"

IMAGE_PAGE = "image_model_analysis.html"

# Fingerprint of the chat template's four <style> blocks, concatenated. This is
# a deliberate tripwire: the design system lives entirely in those blocks.
# Updated once so far - the shared nav-tab rules (.tabs/.tab) were added when
# navigation moved off the per-template link and onto the shared shell. That was
# an intentional design-system change, and the image template's copy moved with
# it (see test_image_template_styles_are_a_verbatim_copy_of_the_chat_template).
CHAT_STYLE_BLOCK_SHA256 = "a5efa541dac6ea5fbe2ae69db5d1a262eb94a30a465358c98e9230c555f1c412"

# The DOM ids the chat dashboard's minified script owns. They are the contract
# between that template's markup and its JavaScript; an edit that drops one
# breaks the page silently, so assert they all still exist.
CHAT_JS_OWNED_IDS = [
    "kpiRow", "snapshotLine", "pickTitle", "pickProvider", "pickScore", "pickMetric",
    "pickPrice", "pickValue", "pickContext", "pickNote", "comparisonSearch",
    "comparisonResults", "comparisonContent", "metricSeg", "searchInput", "scatterCount",
    "scatterChart", "scatterTitle", "scatterSub", "scatterCaption", "scoreLeaders",
    "valueLeaders", "valueCaption", "leaderCaption", "qualityList", "tableSearch",
    "tableCount", "tableBody", "dataTable", "tooltip",
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def style_blocks(html: str) -> list[str]:
    return re.findall(r"<style>.*?</style>", html, re.S)


def body_of(html: str) -> str:
    return html.split("</head>", 1)[1]


def cp_lines() -> list[tuple[str, str]]:
    """Every `cp <source> <dest>` in the publish workflow."""
    pairs = []
    for line in read(PUBLISH_WORKFLOW_PATH).splitlines():
        match = re.match(r"\s*cp\s+(\S+)\s+(\S+)\s*$", line)
        if match:
            pairs.append((match.group(1), match.group(2)))
    return pairs


# ---------------------------------------------------------------------------
# design parity: the styling is copied, not re-derived
# ---------------------------------------------------------------------------
def test_image_template_styles_are_a_verbatim_copy_of_the_chat_template():
    chat_blocks = style_blocks(read(CHAT_TEMPLATE_PATH))
    image_blocks = style_blocks(read(IMAGE_TEMPLATE_PATH))

    assert len(chat_blocks) == 4, "the chat template's style blocks moved; update this test deliberately"
    assert image_blocks == chat_blocks, (
        "the image template's <style> blocks must be an unmodified, same-order copy of "
        "the chat template's - the visual result depends on the whole cascade"
    )


def test_image_template_loads_the_fonts_its_tokens_reference():
    chat = read(CHAT_TEMPLATE_PATH)
    image = read(IMAGE_TEMPLATE_PATH)
    for link in re.findall(r'<link[^>]*fonts\.(?:googleapis|gstatic)\.com[^>]*>', chat):
        assert link in image, f"missing font link in the image template: {link}"
    assert "DM+Serif+Display" in image and "family=Inter" in image


def test_chat_template_style_blocks_are_unchanged():
    digest = hashlib.sha256("\n".join(style_blocks(read(CHAT_TEMPLATE_PATH))).encode("utf-8")).hexdigest()
    assert digest == CHAT_STYLE_BLOCK_SHA256, (
        "the chat dashboard's CSS changed. This phase promised the only chat-side change "
        "would be a nav link - if the CSS really did change on purpose, update this hash."
    )


# ---------------------------------------------------------------------------
# navigation comes from the shared tab list, not from per-template markup
# ---------------------------------------------------------------------------
def test_nav_is_rendered_from_the_shared_tab_list():
    """Both pages must carry an EMPTY tab container for the shared shell to
    fill. A hardcoded anchor in there is exactly how a one-way toggle starts,
    and how the two-way version drifted from a third tab being addable."""
    for path in (CHAT_TEMPLATE_PATH, IMAGE_TEMPLATE_PATH):
        html = read(path)
        match = re.search(r'<nav class="tabs" id="navTabs"[^>]*>(.*?)</nav>', html, re.S)
        assert match, f"{path.name} is missing the shared tab container"
        assert match.group(1).strip() == "", (
            f"{path.name} hardcodes tab markup; tabs must come from nav_tabs.NAV_TABS"
        )
        assert html.count("__SHELL_JS__") == 1, (
            f"{path.name} must receive the shared shell exactly once"
        )


def test_the_shared_shell_never_reads_the_page_payload():
    """Ordering guard, and the reason the tab data is injected with the code.

    The two templates deliberately order their scripts differently: the image
    page runs the shell BEFORE its payload script so the theme is applied before
    first paint. A shell that read PAYLOAD would therefore work on the chat page
    and silently render ZERO tabs on the image page - no error, just a missing
    nav. This asserts the dependency never comes back.
    """
    source = read(DASHBOARD_DIR / "web_assets" / "shell.js")
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)  # the docs may say anything
    assert "PAYLOAD" not in code, (
        "the shell must not read the payload - the templates do not agree on script order"
    )
    assert "NAV_TABS" in code, "the shell should render from the injected tab data"


def test_the_committed_pages_inject_tab_data_with_the_shell_code():
    """Self-contained by construction: the data sits immediately above the code
    in the built page, so the shell cannot depend on another script having run."""
    for built in (build_dashboard.OUTPUT_PATH, build_image_dashboard.OUTPUT_PATH):
        html = read(built)
        data_at = html.index("const NAV_TABS=[")
        uses_at = html.index('getElementById("navTabs")', data_at)
        assert data_at < uses_at, f"{built.name} uses NAV_TABS before injecting it"


def test_the_shell_placeholder_is_required_and_unique():
    """inject() must refuse a template that lost the placeholder: silently
    shipping a page with no navigation is worse than a failed build."""
    with pytest.raises(ValueError):
        page_shell.inject("<html><body>no placeholder here</body></html>")
    with pytest.raises(ValueError):
        page_shell.inject("<script>__SHELL_JS__</script><script>__SHELL_JS__</script>")

    injected = page_shell.inject(
        "<nav id=\"navTabs\"></nav><script>__SHELL_JS__</script>",
        tabs=[{"key": "x", "label": "X", "href": "x.html", "page": "x.html"}],
    )
    assert 'const NAV_TABS=[{"key":"x"' in injected
    # Assert the placeholder was CONSUMED at its injection point. A bare
    # `"__SHELL_JS__" not in injected` would match the shell's own doc comment,
    # which names the placeholder on purpose.
    assert "<script>const NAV_TABS=" in injected


def test_both_pages_receive_the_same_shell_source():
    """The nav bar and theme toggle exist in ONE file, injected at build time.
    If a builder stops using it, that page silently loses its navigation."""
    shell_path = DASHBOARD_DIR / "web_assets" / "shell.js"
    assert shell_path.exists(), "the shared shell asset is missing"
    source = read(shell_path)
    # Anchor on the ASSIGNMENT form: the shell's own comment explains the
    # injected constant by name, so a substring check would match the docs.
    assert not re.search(r"^const NAV_TABS=", source, re.M), (
        "the tab DATA is injected per page; only the CODE is shared"
    )
    for module in (build_dashboard, build_image_dashboard):
        assert module.page_shell.SHELL_JS_SOURCE == shell_path


def test_the_committed_pages_are_not_stale_relative_to_their_templates():
    """A template edit does NOT rebuild the published artifact, and the workflow
    stages the COMMITTED html rather than rebuilding it - so a stale artifact is
    how a nav change silently fails to appear on the live site."""
    for built in (build_dashboard.OUTPUT_PATH, build_image_dashboard.OUTPUT_PATH):
        html = read(built)
        assert "__DATA__" not in html, f"{built.name} still holds an unsubstituted payload"
        # The injected form is machine-generated JSON and therefore has no space
        # around the `=`, unlike the shell's own comment - which is why this can
        # assert on the injected marker without matching the documentation.
        assert "const NAV_TABS=[" in html, (
            f"{built.name} has no injected tab list - rebuild it from its template"
        )


def test_chat_page_still_has_the_structure_its_script_depends_on():
    chat = read(CHAT_TEMPLATE_PATH)
    assert chat.count("__DATA__") == 1, "the payload placeholder must appear exactly once"
    # One payload script + one shared shell script. The chat page used to carry
    # a third, page-local refresh script; that logic moved into the shell, which
    # is what lets the image page offer the same control without a copy.
    assert chat.count("<script>") == 2, "the chat page's two script blocks must be intact"
    for dom_id in CHAT_JS_OWNED_IDS:
        assert f'id="{dom_id}"' in chat, f"the chat dashboard's script expects #{dom_id} to exist"


def test_both_pages_offer_the_same_unified_refresh_control():
    """The button refreshes BOTH dashboards in one click, so the image page is
    entitled to offer it. What must never happen is a page-local COPY of the
    refresh logic: the control lives in the shared shell, so the two pages
    cannot disagree about what a refresh does or how it reports."""
    chat = read(CHAT_TEMPLATE_PATH)
    image = read(IMAGE_TEMPLATE_PATH)
    for name, html in (("chat", chat), ("image", image)):
        assert 'id="refreshBtn"' in html, f"the {name} page lost the shared refresh button"
        assert 'id="refreshStatus"' in html, f"the {name} page lost the shared refresh status"
        assert "Update All Latest AI Data" in html, (
            f"the {name} page's button must say it updates everything"
        )
        assert html.count("<script>") == 2, (
            f"the {name} page should have exactly one payload script and one shared shell script"
        )
    # The implementation itself is in the shell, not duplicated per template.
    for html in (chat, image):
        assert "REFRESH_API" not in html
        assert "llmDashboardRefreshSeenAt" not in html


def test_image_page_keeps_its_theme_toggle():
    image = read(IMAGE_TEMPLATE_PATH)
    assert 'id="themeToggle"' in image, "the theme toggle is self-contained and must stay"
    assert image.count("__DATA__") == 1


def test_every_nav_tab_is_both_served_and_staged():
    """A tab that 404s is worse than no tab. Tie the tab list to BOTH delivery
    paths - serve.py locally, the staging step on deploy - so adding the video
    dashboard fails loudly here until its route and its `cp` line exist."""
    staged = {Path(dest).name for _, dest in cp_lines()}
    for tab in nav_tabs.NAV_TABS:
        assert tab["href"] in staged, (
            f"the {tab['key']} tab links to {tab['href']}, which the workflow never stages"
        )
        assert f"/{tab['href']}" in serve.SERVABLE, (
            f"the {tab['key']} tab links to {tab['href']}, which serve.py does not route"
        )


def test_every_nav_tab_points_at_a_page_that_is_actually_built():
    """`page` is the built filename; it differs from `href` for the chat tab
    (built as price_performance_final.html, staged as index.html). The shell
    uses both so the active tab is right on the live site AND when the file is
    opened directly from disk."""
    for tab in nav_tabs.NAV_TABS:
        assert (DASHBOARD_DIR / tab["page"]).exists(), (
            f"the {tab['key']} tab points at {tab['page']}, which is not built"
        )

    chat_tab = next(tab for tab in nav_tabs.NAV_TABS if tab["key"] == "chat")
    assert chat_tab["page"] == build_dashboard.OUTPUT_PATH.name
    assert chat_tab["href"] == "index.html"
    assert ("dashboard/price_performance_final.html", "_site/index.html") in cp_lines()
    assert build_image_dashboard.OUTPUT_PATH.name in nav_tabs.pages()


# ---------------------------------------------------------------------------
# serving and staging
# ---------------------------------------------------------------------------
def test_servable_keeps_the_chat_routes_and_adds_one_image_route():
    assert serve.SERVABLE == {
        "/": build_dashboard.OUTPUT_PATH,
        "/index.html": build_dashboard.OUTPUT_PATH,
        "/dashboard.html": build_dashboard.OUTPUT_PATH,
        f"/{IMAGE_PAGE}": build_image_dashboard.OUTPUT_PATH,
    }


def test_servable_originals_are_still_bound_to_the_chat_dashboard():
    for route in ("/", "/index.html", "/dashboard.html"):
        assert serve.SERVABLE[route] == build_dashboard.OUTPUT_PATH
        assert serve.SERVABLE[route].name == "price_performance_final.html"


def test_publish_workflow_stages_every_page_the_nav_links_point_at():
    assert cp_lines() == [
        ("dashboard/price_performance_final.html", "_site/index.html"),
        ("dashboard/status.json", "_site/status.json"),
        (f"dashboard/{IMAGE_PAGE}", f"_site/{IMAGE_PAGE}"),
    ]


def test_the_staged_chat_page_name_is_exactly_the_name_its_tab_uses():
    """The nav link is relative, so it resolves to the same directory only if
    the basename matches what actually gets staged. This is the reason a tab
    carries both `href` and `page` rather than one of them."""
    chat_tab = next(tab for tab in nav_tabs.NAV_TABS if tab["key"] == "chat")
    staged = [dest for source, dest in cp_lines()
              if source == f"dashboard/{build_dashboard.OUTPUT_PATH.name}"]
    assert staged == ["_site/index.html"]
    assert Path(staged[0]).name == chat_tab["href"]

    image_tab = next(tab for tab in nav_tabs.NAV_TABS if tab["key"] == "image")
    assert image_tab["href"] == IMAGE_PAGE == build_image_dashboard.OUTPUT_PATH.name


# ---------------------------------------------------------------------------
# the new build cannot collide with a frozen chat-side surface
# ---------------------------------------------------------------------------
def test_image_build_output_is_not_a_chat_pipeline_filename():
    assert build_image_dashboard.OUTPUT_PATH != build_dashboard.OUTPUT_PATH
    assert build_image_dashboard.OUTPUT_PATH.name == IMAGE_PAGE
    assert build_image_dashboard.OUTPUT_PATH.parent == DASHBOARD_DIR


def test_image_build_writes_only_its_one_output_file():
    """No second artifact: no status sidecar, no snapshot directory. Anything
    else would also need staging and could be mistaken for a chat-side file."""
    assert not hasattr(build_image_dashboard, "STATUS_PATH")
    module_paths = {name for name in dir(build_image_dashboard) if name.endswith("_PATH")}
    assert module_paths == {
        "IMAGE_MODELS_PATH", "PROMPT_BENCHMARKS_PATH", "DESIGN_ARENA_PATH",
        "COVERAGE_PATH", "QUALITY_PATH", "TEMPLATE_PATH", "OUTPUT_PATH",
    }


def test_image_build_reads_only_media_pipeline_inputs():
    """The image page must not reach into the chat pipeline's data at all."""
    source = read(DASHBOARD_DIR / "build_image_dashboard.py")
    import_lines = [line.strip() for line in source.splitlines()
                    if re.match(r"\s*(import|from)\s", line)]
    assert any(line.startswith("from config import settings") for line in import_lines)
    assert all("weekly_picks" not in line for line in import_lines)
    assert all("build_dashboard" not in line for line in import_lines)

    # Every data path is the media pipeline's own, taken from settings.MEDIA_*.
    path_sources = re.findall(r"^(\w+_PATH)\s*=\s*(.+)$", source, re.M)
    assert path_sources, "expected module-level path constants"
    for name, value in path_sources:
        if name == "TEMPLATE_PATH":
            continue
        if name == "OUTPUT_PATH":
            continue
        assert value.startswith("settings.MEDIA_"), f"{name} must come from the media settings: {value}"

    # The chat pipeline's own filenames must not appear as literals anywhere.
    assert '"models.json"' not in source
    assert '"model_benchmarks.json"' not in source
    assert '"coverage_report.json"' not in source


def test_image_build_uses_the_media_snapshot_root_which_is_not_the_chat_one():
    from config import settings

    assert build_image_dashboard.SNAPSHOTS_DIR == settings.MEDIA_SNAPSHOTS_DIR
    assert build_image_dashboard.SNAPSHOTS_DIR.name == "media_snapshots"
    # data/media_snapshots/ cannot match refresh.py::snapshot_exists_for(), which
    # only looks at direct children of data/snapshots/ named <date>/<date>-N.
    assert build_image_dashboard.SNAPSHOTS_DIR != settings.SNAPSHOTS_DIR
    assert settings.SNAPSHOTS_DIR not in build_image_dashboard.SNAPSHOTS_DIR.parents
