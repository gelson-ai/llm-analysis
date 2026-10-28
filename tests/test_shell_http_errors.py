"""Regression tests for the shared shell's HTTP error handling.

`fetch()` RESOLVES for 4xx/5xx, and both refresh backends answer a failure with
a JSON body:

    Worker     500 {"ok": false, "error": "not_configured"}
               502 {"ok": false, "error": "status_unavailable", "message": ...}
    serve.py   404 {"ok": false, "error": "not_found"}

The shell used to do `fetch(...).then(r => r.json())` with no status check, so
an error body was rendered AS the status: `freshFor()` found no block, the
affected category read "status unavailable", the chat block read as stale,
`lastStatus` was poisoned with the error payload, and the idle path then ended
in `setStatus("")` - clearing the one line that was reporting the failure.

Two layers of coverage, matching the precedents already in this repo:

  * source invariants over `dashboard/web_assets/shell.js` (the style used by
    tests/test_image_dashboard_invariants.py), which always run;
  * a node-driven DOM stub that EXECUTES the shell and asserts on the rows it
    actually renders (the style of the `node --check` gate in
    tests/test_refresh_pipelines.py). Skipped when node is not installed.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import build_dashboard  # noqa: E402
import build_image_dashboard  # noqa: E402
import build_video_dashboard  # noqa: E402

SHELL_PATH = DASHBOARD_DIR / "web_assets" / "shell.js"
NODE = shutil.which("node")


def source() -> str:
    return SHELL_PATH.read_text(encoding="utf-8")


def code_without_comments() -> str:
    """The shell's comments describe the contract, so they may say anything.

    Both comment styles are removed: the shell's own prose names
    `setStatus("")` while explaining why the failure path never calls it.
    `(?<!:)` keeps `https://` inside string literals intact.
    """
    code = re.sub(r"/\*.*?\*/", "", source(), flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", code)


# ---------------------------------------------------------------------------
# source invariants
# ---------------------------------------------------------------------------
def test_every_fetch_checks_response_ok():
    """No fetch may rely on the body alone: a 500/502 that answers with JSON is
    exactly the shape that used to slip through as a successful response."""
    code = code_without_comments()
    sites = [match.start() for match in re.finditer(r"\bfetch\(", code)]
    assert len(sites) >= 2, "expected the /status and /refresh fetches"

    for index, start in enumerate(sites):
        end = sites[index + 1] if index + 1 < len(sites) else len(code)
        window = code[start:end]
        assert "!response.ok" in window, (
            "a fetch in the shell has no explicit response.ok check: "
            + " ".join(window.split())[:90]
        )


def test_a_non_ok_status_body_is_never_rendered_as_a_status():
    """The guard must sit in front of the success path, not after it."""
    code = code_without_comments()
    status_at = code.index('fetch(REFRESH_API + "/status"')
    cached_at = code.index("lastStatus = data")
    guard_at = code.index("if (!response.ok)", status_at)

    assert status_at < guard_at < cached_at, (
        "the status poll assigns lastStatus before checking response.ok"
    )

    between = code[status_at:cached_at]
    assert "httpFailure(response, data)" in between, (
        "the non-OK branch must build the error from the response"
    )
    assert "throw" in between, (
        "the non-OK branch must not fall through to the success path"
    )


def test_the_failure_path_never_clears_the_global_status():
    """Clearing the line is the bug: only a genuinely idle, successful poll
    may blank it."""
    code = code_without_comments()
    assert code.count('setStatus("")') == 1, (
        "only the successful idle path may clear the global status line"
    )

    start = code.index("const reportStatusFailure")
    failure_handler = code[start:code.index("};", start) + 2]
    assert "setStatus(failure.global)" in failure_handler
    assert "renderTargets(null, lastStatus, failure)" in failure_handler
    assert 'setStatus("")' not in failure_handler

    # Both the server-answered failure and the unreachable-service failure go
    # through that one handler, so there is a single place that decides what a
    # failed read looks like.
    assert "const reportStatusFailure = " in code
    assert code.count("reportStatusFailure(") >= 2, (
        "the /status poll and the /refresh click both need the shared handler"
    )


def test_a_known_category_keeps_its_row_on_a_transient_failure():
    """A 5xx must not repaint a category that was already known good; only a
    category with no known state gets the error row."""
    code = code_without_comments()
    assert "renderTargets = (job, status, failure)" in code, (
        "renderTargets no longer accepts the failure marker"
    )
    assert "if (!fresh && failure)" in code, (
        "the error row must be limited to categories with no known state"
    )


def test_every_built_page_carries_the_http_error_guards():
    """The shell is injected at build time, so an unbuilt artifact would keep
    shipping the unchecked fetch on the live site."""
    for built in (
        build_dashboard.OUTPUT_PATH,
        build_image_dashboard.OUTPUT_PATH,
        build_video_dashboard.OUTPUT_PATH,
    ):
        html = built.read_text(encoding="utf-8")
        assert "const httpFailure" in html, f"{built.name} is stale - rebuild it"
        assert "const reportStatusFailure" in html, f"{built.name} is stale - rebuild it"
        assert "!response.ok" in html, f"{built.name} is stale - rebuild it"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_shell_script_parses():
    """shell.js had no syntax gate at all; a typo would first show up as a page
    with no nav, no theme toggle and no refresh button."""
    result = subprocess.run(
        [NODE, "--check", str(SHELL_PATH)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# behavioural: run the real shell against a stubbed DOM and fetch
# ---------------------------------------------------------------------------
# stdout is one JSON document: {rows: [{target, state, text}], status, disabled}
HARNESS_JS = r"""
const fs = require("fs");
const vm = require("vm");

const SHELL = fs.readFileSync(process.argv[2], "utf8");
const SCENARIO = process.argv[3];

let inserted = null;
const elements = {};
const captured = { poll: null };

function makeElement(id) {
  let text = "";
  const el = {
    id: id || "",
    className: "",
    dataset: {},
    children: [],
    style: {},
    hidden: false,
    disabled: false,
    title: "",
    setAttribute(name, value) { el[name] = value; },
    appendChild(child) { el.children.push(child); return child; },
    insertAdjacentElement(_where, sibling) { inserted = sibling; return sibling; },
    addEventListener(type, handler) {
      el._handlers = el._handlers || {};
      el._handlers[type] = handler;
    },
  };
  Object.defineProperty(el, "textContent", {
    get() { return text; },
    set(value) {
      text = value === undefined || value === null ? "" : String(value);
      // renderTargets() clears the list by assigning "" before re-appending.
      if (!text) el.children.length = 0;
    },
  });
  return el;
}

const documentStub = {
  documentElement: { dataset: {} },
  head: { appendChild() {} },
  getElementById(id) {
    if (id === "themeToggle") return null;   // the toggle is optional
    if (!elements[id]) elements[id] = makeElement(id);
    return elements[id];
  },
  createElement() { return makeElement(); },
};

const hourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();
const block = (count) => ({
  retrieved_at: hourAgo,
  age_hours: 1,
  stale: false,
  model_count: count,
});
const OK_BODY = {
  ok: true,
  data: { ...block(412), media: block(54), video: block(29) },
  media: block(54),
  video: block(29),
  job: { state: "idle", finished_at: null, retry_after_seconds: 0, targets: null },
};
const OK_COOLDOWN_BODY = {
  ...OK_BODY,
  job: { ...OK_BODY.job, retry_after_seconds: 300 },
};
const ERROR_BODY = {
  ok: false,
  error: "status_unavailable",
  message: "GitHub run lookup failed with HTTP 500",
};

const queue = { specs: [] };
function scriptedFetch() {
  const spec = queue.specs.length > 1 ? queue.specs.shift() : queue.specs[0];
  return Promise.resolve({
    ok: spec.ok,
    status: spec.status,
    json() { return Promise.resolve(spec.body); },
  });
}

const context = {
  document: documentStub,
  window: { matchMedia: () => ({ matches: false, addEventListener() {} }) },
  location: {
    protocol: "http:",
    hostname: "localhost",
    pathname: "/",
    reload() {},
  },
  sessionStorage: {
    _v: {},
    getItem(key) { return this._v[key] || null; },
    setItem(key, value) { this._v[key] = String(value); },
  },
  setInterval(fn) { captured.poll = fn; return 1; },
  clearInterval() {},
  fetch: scriptedFetch,
  console,
};
vm.createContext(context);

const PRELUDE =
  'const NAV_TABS=[' +
  '{"key":"chat","label":"Chat models","href":"index.html","page":"p.html"},' +
  '{"key":"image","label":"Image models","href":"i.html","page":"i.html"},' +
  '{"key":"video","label":"Video models","href":"v.html","page":"v.html"}];\n';

function rendered() {
  const rows = (inserted ? inserted.children : []).map((li) => ({
    target: li.dataset.target,
    state: li.dataset.state,
    text: li.children.length > 1 ? li.children[1].textContent : "",
  }));
  return {
    rows,
    status: elements.refreshStatus.textContent,
    disabled: elements.refreshBtn.disabled,
  };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 25));

(async () => {
  if (SCENARIO === "server-error") {
    queue.specs = [{ ok: false, status: 502, body: ERROR_BODY }];
  } else if (SCENARIO === "success") {
    queue.specs = [{ ok: true, status: 200, body: OK_BODY }];
  } else if (SCENARIO === "later-failure") {
    queue.specs = [{ ok: true, status: 200, body: OK_COOLDOWN_BODY }];
  } else {
    throw new Error("unknown scenario: " + SCENARIO);
  }

  vm.runInContext(PRELUDE + SHELL, context);
  await tick();

  if (SCENARIO === "later-failure") {
    if (!captured.poll) {
      throw new Error("the shell did not schedule a follow-up poll");
    }
    queue.specs = [{ ok: false, status: 502, body: ERROR_BODY }];
    captured.poll();
    await tick();
  }

  process.stdout.write(JSON.stringify(rendered()));
})().catch((err) => {
  console.error((err && err.stack) || String(err));
  process.exit(1);
});
"""


def run_harness(tmp_path: Path, scenario: str) -> dict:
    script = tmp_path / "shell_harness.js"
    script.write_text(HARNESS_JS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script), str(SHELL_PATH), scenario],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_502_status_response_renders_the_error_state(tmp_path):
    """The reported bug: a 500/502 body used to become "status unavailable" on
    the affected category and then blank the global line."""
    result = run_harness(tmp_path, "server-error")

    assert [row["target"] for row in result["rows"]] == ["chat", "image", "video"]
    for row in result["rows"]:
        assert row["state"] == "error", row
        assert "502" in row["text"], row
        assert "status unavailable" not in row["text"], row

    assert result["status"], "a failed status read must not clear the global line"
    assert "502" in result["status"], result["status"]
    assert "GitHub run lookup failed with HTTP 500" in result["status"], (
        "the backend's own message should be surfaced"
    )
    assert result["disabled"] is False, "the refresh button must be usable again"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_200_status_response_still_renders_freshness(tmp_path):
    """The successful path must behave exactly as before: one honest row per
    category and a cleared global line."""
    result = run_harness(tmp_path, "success")

    assert [row["state"] for row in result["rows"]] == ["ok", "ok", "ok"]
    for row in result["rows"]:
        assert row["text"].startswith("fresh \u00b7 "), row
    assert result["status"] == ""
    assert result["disabled"] is False


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_later_failure_keeps_the_last_known_good_rows(tmp_path):
    """One bad poll must not repaint categories that were just confirmed good -
    only the global line reports the failure."""
    result = run_harness(tmp_path, "later-failure")

    assert [row["state"] for row in result["rows"]] == ["ok", "ok", "ok"], (
        "a transient failure overwrote per-category states that were known good"
    )
    assert "502" in result["status"]
    assert "Data is up to date" not in result["status"], (
        "the failure must replace the cooldown message, not be masked by it"
    )
