"""Cross-file invariants for the second (image-generation) dashboard page.

Nothing here tests a single module in isolation. These lock down the promises
that only hold across files, and that a future edit could quietly break:

  * the image template's styling is a *verbatim copy* of the chat template's,
    so the two pages cannot drift apart into different-looking products;
  * the chat page gained exactly one link and nothing else;
  * serve.py's original routes still point at the original file;
  * the workflow stages the new page at the exact path the nav link uses, so
    the link cannot 404 on the live site;
  * the new build writes to a path no frozen chat-side surface is watching.

tests/test_media_snapshot_paths.py is the precedent for this style: import the
real modules and assert against them, rather than reimplementing their logic.
"""
import hashlib
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import build_dashboard  # noqa: E402  (imported read-only - never modified)
import build_image_dashboard  # noqa: E402
import serve  # noqa: E402

CHAT_TEMPLATE_PATH = DASHBOARD_DIR / "template.html"
IMAGE_TEMPLATE_PATH = DASHBOARD_DIR / "image_template.html"
PUBLISH_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "publish.yml"

IMAGE_PAGE = "image_model_analysis.html"

# Fingerprint of the chat template's four <style> blocks, concatenated. This is
# a deliberate tripwire: the design system lives entirely in those blocks, and
# this phase promised not to touch them. If you are *intentionally* changing
# the design system, update this hash in the same commit - and remember the
# image template is then out of date too.
CHAT_STYLE_BLOCK_SHA256 = "cd2bde2a75ffbe0dc69f93812e8ba489203e852c2652bd74b75d129529c97658"

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
# the chat page gained one link and nothing else
# ---------------------------------------------------------------------------
def test_chat_page_gained_exactly_one_link():
    body = body_of(read(CHAT_TEMPLATE_PATH))
    links = re.findall(r"<a\s[^>]*>", body)
    assert len(links) == 1, f"expected exactly one link on the chat page, found {len(links)}"
    assert f'href="{IMAGE_PAGE}"' in links[0]


def test_chat_page_still_has_the_structure_its_script_depends_on():
    chat = read(CHAT_TEMPLATE_PATH)
    assert chat.count("__DATA__") == 1, "the payload placeholder must appear exactly once"
    assert chat.count("<script>") == 3, "the chat page's three script blocks must be intact"
    for dom_id in CHAT_JS_OWNED_IDS:
        assert f'id="{dom_id}"' in chat, f"the chat dashboard's script expects #{dom_id} to exist"


def test_image_page_does_not_offer_a_refresh_button_it_cannot_honour():
    """Refreshing runs dashboard/refresh.py, which rebuilds only the chat page.
    A refresh button here would silently do nothing for this page."""
    image = read(IMAGE_TEMPLATE_PATH)
    assert "refreshBtn" not in image
    assert "refreshStatus" not in image
    assert 'id="themeToggle"' in image, "the theme toggle is self-contained and must stay"
    assert image.count("__DATA__") == 1
    assert image.count("<script>") == 2


def test_the_two_pages_link_to_each_other():
    image = read(IMAGE_TEMPLATE_PATH)
    assert f'href="{IMAGE_PAGE}"' in read(CHAT_TEMPLATE_PATH)
    assert 'href="index.html"' in image, "the image page needs a way back to the chat page"


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


def test_the_staged_image_page_name_is_exactly_the_name_the_link_uses():
    staged = [dest for source, dest in cp_lines() if source == f"dashboard/{IMAGE_PAGE}"]
    assert staged == [f"_site/{IMAGE_PAGE}"]

    # The link in the chat page is relative, so it resolves against the same
    # directory only if the basename matches what gets staged.
    href = re.search(r'<a[^>]*href="([^"]+)"[^>]*>', body_of(read(CHAT_TEMPLATE_PATH))).group(1)
    assert href == IMAGE_PAGE == build_image_dashboard.OUTPUT_PATH.name
    assert Path(staged[0]).name == href


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
