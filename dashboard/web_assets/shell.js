/* Shared page shell for the OpenRouter dashboards.
 *
 * Injected into every dashboard at build time at the __SHELL_JS__ placeholder
 * (see the page builders), so the pages cannot drift apart. It owns three things:
 *
 *   1. the theme toggle - behaviour unchanged from when it lived inline, just
 *      shared instead of copied;
 *   2. the dashboard tab bar;
 *   3. the unified "Update All Latest AI Data" refresh control.
 *
 * The tab list is NOT defined here. It comes from the Python-side source of
 * truth (dashboard/nav_tabs.py) and is injected immediately above this code as
 * `const NAV_TABS = [...]` rather than being read out of the page payload.
 * That is deliberate: the two templates do not put their scripts in the same
 * order - the image page's shell runs BEFORE its payload script, so the theme
 * toggle applies before first paint - and reading the payload here would
 * therefore silently render no tabs on one of the two pages.
 *
 * Everything is defensive: with no tab list the nav is simply hidden.
 */
(() => {
  "use strict";

  /* --------------------------------------------------------------------- *
   * theme toggle
   * --------------------------------------------------------------------- */
  const root = document.documentElement;
  const themeBtn = document.getElementById("themeToggle");
  const mq = window.matchMedia("(prefers-color-scheme: dark)");

  const resolved = () => root.dataset.theme || (mq.matches ? "dark" : "light");
  const label = () => {
    if (!themeBtn) return;
    const theme = resolved();
    themeBtn.textContent = theme === "dark" ? "Light mode" : "Dark mode";
    themeBtn.setAttribute("aria-pressed", String(theme === "dark"));
  };
  label();
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      root.dataset.theme = resolved() === "dark" ? "light" : "dark";
      label();
    });
  }
  if (mq.addEventListener) {
    mq.addEventListener("change", label);
  } else if (mq.addListener) {
    mq.addListener(label);
  }

  /* --------------------------------------------------------------------- *
   * dashboard tabs
   * --------------------------------------------------------------------- */
  const nav = document.getElementById("navTabs");
  if (!nav) return;

  const tabs = (typeof NAV_TABS !== "undefined" && NAV_TABS) || [];
  if (!tabs.length) {
    nav.hidden = true;
    return;
  }

  // Which tab is "here"? Compare against both the staged URL basename and the
  // built filename, so the active state is right when served by serve.py (as
  // "/" or "/dashboard.html"), on the live site, and when opened as a file.
  const here = location.pathname.split("/").pop() || "";
  const isCurrent = (tab) =>
    tab.href === here ||
    tab.page === here ||
    (here === "" && tab.href === "index.html");

  tabs.forEach((tab) => {
    const link = document.createElement("a");
    link.className = "tab";
    link.href = tab.href;
    link.textContent = tab.label;
    link.dataset.tab = tab.key;
    if (isCurrent(tab)) link.setAttribute("aria-current", "page");
    nav.appendChild(link);
  });

  /* --------------------------------------------------------------------- *
   * unified refresh control
   *
   * One click refreshes BOTH dashboards. The pipelines themselves stay
   * independent (dashboard/refresh_all.py only spawns them), and this reports
   * per-dashboard success/failure when one works and the other does not.
   * --------------------------------------------------------------------- */
  const refreshBtn = document.getElementById("refreshBtn");
  const refreshStatus = document.getElementById("refreshStatus");
  if (!refreshBtn || !refreshStatus) return;

  const setStatus = (text) => {
    refreshStatus.textContent = text;
  };

  /* --------------------------------------------------------------------- *
   * failed status reads
   *
   * fetch() RESOLVES for 4xx/5xx, and BOTH backends answer a failure with a
   * JSON body ({"ok":false,"error":...}), so an unchecked .json() fed an
   * error payload into the renderer as if it were a status: categories with no
   * block read "status unavailable", the chat block read as stale, and the
   * idle path then cleared the global line - hiding the very failure that
   * caused it. Every response is therefore checked explicitly, and the body of
   * a non-OK response is never treated as a status.
   * --------------------------------------------------------------------- */
  const httpFailure = (response, data) => {
    const detail = (data && (data.message || data.error)) || "";
    return {
      status: response.status,
      // Global line: the CHECK failed, so nothing is claimed about the data.
      global:
        "Could not check dashboard status (HTTP " + response.status + ")" +
        (detail ? ": " + detail : ""),
      // Per-category text, used only where no earlier state is known.
      row: "unavailable — HTTP " + response.status,
    };
  };

  // No response at all (offline, DNS, CORS): there is no HTTP status to quote.
  const UNREACHABLE = {
    global: "Cannot reach the refresh service. Please try again shortly.",
    row: "unavailable — service unreachable",
  };

  /* --------------------------------------------------------------------- *
   * per-category status
   *
   * One row per dashboard category (Text/LLM, Image, Video) so a refresh
   * reports each pipeline separately instead of collapsing three outcomes
   * into one sentence - and so each category has its own loading, error and
   * freshness state. When nothing is running, a row falls back to that
   * category's own data freshness, which is the at-a-glance "is this
   * category's data loaded / stale / failed" answer.
   *
   * Built from the injected NAV_TABS (so the labels always match the tabs)
   * and created here in the DOM rather than in the templates: the pages own
   * no category-status markup, and the shell is the one place that knows the
   * refresh contract.
   * --------------------------------------------------------------------- */
  const TARGET_KEY = { chat: "chat", image: "media", video: "video" };
  const targetsEl = document.createElement("ul");
  targetsEl.id = "refreshTargets";
  targetsEl.className = "refresh-targets";
  targetsEl.setAttribute("role", "status");
  targetsEl.setAttribute("aria-live", "polite");
  targetsEl.setAttribute("aria-label", "Dashboard data status");
  refreshStatus.insertAdjacentElement("afterend", targetsEl);

  // The styling lives here, injected at runtime, so the shared shell gains its
  // own rules without touching any page's <style> blocks (which are frozen and
  // must stay byte-identical across the three dashboards).
  const targetStyle = document.createElement("style");
  targetStyle.textContent =
    "#refreshTargets{list-style:none;margin:6px 0 0;padding:0;display:flex;flex-wrap:wrap;gap:4px 16px}" +
    "#refreshTargets li{display:inline-flex;gap:6px;align-items:baseline;font-size:11.5px;color:var(--ink-soft,#666)}" +
    "#refreshTargets .refresh-target-name{font-weight:600;color:var(--ink,#222)}" +
    '#refreshTargets li[data-state="error"] .refresh-target-state{color:#b00020}' +
    '#refreshTargets li[data-state="stale"] .refresh-target-state,' +
    '#refreshTargets li[data-state="loading"] .refresh-target-state{color:#8a6d00}';
  document.head.appendChild(targetStyle);

  let lastStatus = null;

  // Freshness of one category. The two backends nest it differently: serve.py
  // returns `media`/`video` beside `data` (chat), while the Cloudflare Worker
  // nests them inside `data`. Read both so neither backend needs changing.
  const freshFor = (status, key) => {
    if (!status) return null;
    const chat = status.data || {};
    if (key === "chat") return chat;
    return chat[key] || status[key] || null;
  };

  const relTime = (iso) => {
    if (!iso) return "";
    const then = Date.parse(iso);
    if (Number.isNaN(then)) return "";
    const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
    if (mins < 60) return mins + "m ago";
    const hours = Math.round(mins / 60);
    if (hours < 48) return hours + "h ago";
    return Math.round(hours / 24) + "d ago";
  };

  // `failure` is set when the status endpoint itself could not be read. It
  // only affects rows with NO known state: a category that was already known
  // good keeps its row, so a transient 5xx cannot falsely repaint it red.
  const renderTargets = (job, status, failure) => {
    const outcomes = job && job.targets ? job.targets : null;
    const running = Boolean(job) && (job.state === "running" || job.state === "starting");
    targetsEl.textContent = "";
    tabs.forEach((tab) => {
      const key = TARGET_KEY[tab.key] || tab.key;
      const li = document.createElement("li");
      li.dataset.target = tab.key;
      const name = document.createElement("span");
      name.className = "refresh-target-name";
      name.textContent = tab.label;
      const state = document.createElement("span");
      state.className = "refresh-target-state";

      const outcome = outcomes ? outcomes[key] : null;
      let kind = "idle";
      let text;
      if (running) {
        // The refresh runs the three pipelines in one job and writes its
        // per-target outcome only at the end, so every category reads as
        // loading together, then resolves to its own outcome.
        kind = "loading";
        text = job.state === "starting" ? "checking…" : "refreshing…";
      } else if (outcome && outcome.state === "ok") {
        kind = "ok";
        text = "updated";
      } else if (outcome && outcome.state === "failed") {
        kind = "error";
        text = "failed — " + (outcome.message || outcome.error || "unknown error");
      } else if (outcome && outcome.state === "already_running") {
        kind = "loading";
        text = "a refresh was already running";
      } else {
        const fresh = freshFor(status, key);
        if (!fresh && failure) {
          // Nothing is known about this category and the status endpoint is
          // failing, so say THAT instead of the misleading "status
          // unavailable" (which reads as "this backend does not report it").
          kind = "error";
          text = failure.row;
        } else if (!fresh) {
          kind = "unknown";
          text = "status unavailable";
        } else if (fresh.stale || fresh.age_hours == null) {
          kind = "stale";
          text = "stale — refresh recommended";
        } else {
          kind = "ok";
          const count = typeof fresh.model_count === "number" ? fresh.model_count + " models · " : "";
          text = "fresh · " + count + (relTime(fresh.retrieved_at) || "up to date");
        }
      }
      li.dataset.state = kind;
      state.textContent = text;
      li.appendChild(name);
      li.appendChild(state);
      targetsEl.appendChild(li);
    });
  };

  const SEEN_KEY = "llmDashboardRefreshSeenAt";
  // Base URL of the status/refresh endpoints: dashboard/serve.py locally (its
  // routes live under /api), the Cloudflare Worker in public. The Worker holds
  // the GitHub token server-side so it is never readable by page visitors.
  const REFRESH_API =
    location.hostname === "localhost" || location.hostname === "127.0.0.1"
      ? "/api"
      : "https://llm-refresh-proxy.gelson-a26.workers.dev";

  if (location.protocol === "file:") {
    refreshBtn.disabled = true;
    refreshBtn.textContent = "Refresh unavailable";
    refreshBtn.title =
      "Refreshing requires the local server. Run: python dashboard/serve.py, then open the http:// address it prints.";
    // No endpoint to ask from a file:// page, so the per-category rows say so
    // rather than sitting on the initial "checking" state forever.
    renderTargets(null, null);
    setStatus("Opened as a local file - start dashboard/serve.py to enable refresh.");
    return;
  }

  // Nothing has been fetched yet; say we are checking instead of showing an
  // empty list, and let the first poll fill in the real per-category states.
  renderTargets({ state: "starting" }, null);

  let timer = null;
  const stopPolling = () => {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  };
  const schedule = (ms) => {
    stopPolling();
    timer = setInterval(poll, ms);
  };

  // One place for "we could not read the status": stop polling, hand the button
  // back, re-render (known-good rows survive) and leave a message on the global
  // line. Deliberately never calls setStatus("").
  const reportStatusFailure = (failure) => {
    stopPolling();
    refreshBtn.disabled = false;
    renderTargets(null, lastStatus, failure);
    setStatus(failure.global);
  };

  // refresh_all.py returns one outcome per target, so a single click can say
  // "Chat: updated. Image: failed - <reason>". `job.targets` is the contract on
  // BOTH backends: dashboard/serve.py lifts it from its own job result, and the
  // Cloudflare Worker lifts it from the committed refresh_status.json.
  const summarise = (job) => {
    const targets = job.targets;
    if (!targets) return (job.result || {}).message || "Refresh failed.";
    const labels = { chat: "Chat", media: "Image", video: "Video" };
    return Object.keys(targets)
      .map((key) => {
        const res = targets[key] || {};
        const label = labels[key] || key;
        if (res.state === "ok") return label + ": updated.";
        if (res.state === "already_running") return label + ": a refresh was already running.";
        return label + ": failed - " + (res.message || res.error || "unknown error");
      })
      .join(" ");
  };

  function poll() {
    fetch(REFRESH_API + "/status", { cache: "no-store" })
      .then((response) =>
        response
          .json()
          // A non-JSON error page (a proxy 502, say) must not become the
          // exception that hides the status code.
          .catch((err) => {
            if (response.ok) throw err;
            return {};
          })
          .then((data) => {
            if (!response.ok) {
              const err = new Error("the status request did not succeed");
              err.failure = httpFailure(response, data);
              throw err;
            }
            return data;
          }),
      )
      .then((data) => {
        // Only a 2xx body ever reaches here, so the cache can never hold an
        // error payload either.
        lastStatus = data;
        const job = data.job || {};
        const retry = job.retry_after_seconds || 0;
        renderTargets(job, data);
        if (job.state === "running") {
          setStatus("Refreshing both dashboards...");
          schedule(1500);
          return;
        }
        const stamp = job.finished_at || "";
        const seen = sessionStorage.getItem(SEEN_KEY) || "";
        const isNew = Boolean(stamp) && stamp !== seen;
        if (job.state === "ok" && isNew) {
          stopPolling();
          sessionStorage.setItem(SEEN_KEY, stamp);
          setStatus("Updated - reloading...");
          location.reload();
          return;
        }
        if (job.state === "failed" && isNew) {
          // Deliberately does NOT reload. A partial failure is exactly when the
          // per-dashboard summary matters most, and reloading would wipe it.
          // Each page keeps showing whatever its own dashboard last published.
          stopPolling();
          sessionStorage.setItem(SEEN_KEY, stamp);
          refreshBtn.disabled = false;
          setStatus(summarise(job));
          return;
        }
        if (retry > 0) {
          refreshBtn.disabled = true;
          setStatus(`Data is up to date - next refresh available in ${retry}s.`);
          schedule(1000);
          return;
        }
        stopPolling();
        refreshBtn.disabled = false;
        setStatus("");
      })
      .catch((err) => {
        // A server that ANSWERED with a non-OK status is not the same as an
        // unreachable service, and neither may fall through to the success
        // path (which would clear the line).
        reportStatusFailure((err && err.failure) || UNREACHABLE);
      });
  }

  refreshBtn.addEventListener("click", () => {
    refreshBtn.disabled = true;
    setStatus("Starting...");
    renderTargets({ state: "starting" }, lastStatus);
    fetch(REFRESH_API + "/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (response.status === 202 || response.status === 409) {
          setStatus("Refreshing both dashboards...");
          renderTargets({ state: "running" }, lastStatus);
          schedule(1500);
          return;
        }
        if (response.status === 429 || data.error === "cooldown") {
          poll();
          return;
        }
        // 409 and 429 are handled above on purpose: they mean "not now", not
        // "the request failed". Everything else that is not ok is explicit
        // here - 500/502 included - instead of relying on whatever the body
        // happened to contain.
        if (!response.ok) {
          stopPolling();
          refreshBtn.disabled = false;
          // The DATA is fine; the REQUEST failed. Each category keeps its own
          // freshness rather than being painted as broken.
          renderTargets(null, lastStatus);
          if (response.status === 401) {
            setStatus("This server requires a refresh token (start it with --token).");
          } else if (response.status === 403) {
            setStatus("Blocked: the request did not come from this dashboard.");
          } else {
            setStatus(
              data.message ||
                data.error ||
                "Refresh failed (HTTP " + response.status + ").",
            );
          }
          return;
        }
        stopPolling();
        refreshBtn.disabled = false;
        renderTargets(null, lastStatus);
        setStatus(data.message || data.error || "Refresh failed.");
      })
      .catch(() => {
        // Same treatment as an unreachable /status: the data is untouched, so
        // the categories keep their last-known state and only the line reports.
        reportStatusFailure(UNREACHABLE);
      });
  });

  poll();
})();
