"""Single source of truth for the dashboard tab bar.

Both dashboards - and any future one, e.g. the video page on its own branch -
render their navigation from this list, which each page builder embeds in its
payload as ``nav_tabs``. Adding a dashboard is a one-line change here plus
whatever builds and serves it; it must never be a copy-paste of nav markup into
another template, because that is how two-way toggles drift apart.

Deliberately *not* a module-level ``*_PATH`` constant, and deliberately not
importing ``build_dashboard`` or ``build_image_dashboard``:
tests/test_image_dashboard_invariants.py asserts that the image builder reads
only media-pipeline inputs and exposes exactly one output path, and this module
is shared by both builders.

Fields per tab:

``key``
    Stable identifier, used for the active-tab decision and by tests.
``label``
    What the user sees.
``href``
    The deployed/staged URL basename, relative to the other pages.
``page``
    The built filename this tab points at. Kept separate from ``href`` because
    the chat page is built as ``price_performance_final.html`` but staged as
    ``index.html``; carrying both lets the active state work even when a page is
    opened straight from disk as a ``file://`` URL.
"""

from __future__ import annotations

NAV_TABS: list[dict[str, str]] = [
    {
        "key": "chat",
        "label": "Chat models",
        "href": "index.html",
        "page": "price_performance_final.html",
    },
    {
        "key": "image",
        "label": "Image models",
        "href": "image_model_analysis.html",
        "page": "image_model_analysis.html",
    },
    {
        "key": "video",
        "label": "Video models",
        "href": "video_model_analysis.html",
        "page": "video_model_analysis.html",
    },
]


def as_payload() -> list[dict[str, str]]:
    """Return the tab list to embed in a page payload (defensive copy)."""
    return [dict(tab) for tab in NAV_TABS]


def hrefs() -> set[str]:
    """Every URL basename a page must be reachable at."""
    return {tab["href"] for tab in NAV_TABS}


def pages() -> set[str]:
    """Every built filename the tabs point at."""
    return {tab["page"] for tab in NAV_TABS}
