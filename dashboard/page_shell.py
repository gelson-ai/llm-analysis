"""Injects the shared page shell into a dashboard template.

The shell (``dashboard/web_assets/shell.js``) is hand-written once and spliced
into every dashboard template at build time, so the two pages cannot drift into
different-looking products. It replaced a theme-toggle script that used to be
byte-identical copy-paste in both templates, and it also renders the tab bar
from ``nav_tabs.NAV_TABS``.

The tab list is injected as data *with* the code, rather than read from the page
payload, because the templates do not order their scripts the same way: the
image page's shell runs before its payload script on purpose, so the theme is
applied before first paint. A payload read would work on one page and silently
render nothing on the other.

Nothing here is a module-level ``*_PATH`` constant: the image builder's tests
assert its exact set of path constants, and this module is shared by both
builders.
"""

from __future__ import annotations

import json
from pathlib import Path

import nav_tabs

DASHBOARD_DIR = Path(__file__).resolve().parent
SHELL_JS_SOURCE = DASHBOARD_DIR / "web_assets" / "shell.js"
PLACEHOLDER = "__SHELL_JS__"


def read_shell_js() -> str:
    """The shared shell source, read fresh so an edit needs no re-import."""
    return SHELL_JS_SOURCE.read_text(encoding="utf-8")


def inject(html: str, tabs: list[dict[str, str]] | None = None) -> str:
    """Replace the shell placeholder with the tab data + the shell source.

    Raises if the placeholder is missing, or present more than once: a template
    that silently stops receiving the shell would lose its navigation without
    anything failing, which is exactly the kind of drift this module exists to
    prevent.
    """
    count = html.count(PLACEHOLDER)
    if count != 1:
        raise ValueError(
            f"expected exactly one {PLACEHOLDER} placeholder in the template, found {count}"
        )

    data = json.dumps(tabs if tabs is not None else nav_tabs.as_payload(),
                      separators=(",", ":"))
    payload = f"const NAV_TABS={data};\n" + read_shell_js()
    return html.replace(PLACEHOLDER, payload)
