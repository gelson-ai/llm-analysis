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
    setStatus("Opened as a local file - start dashboard/serve.py to enable refresh.");
    return;
  }

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
      .then((r) => r.json())
      .then((data) => {
        const job = data.job || {};
        const retry = job.retry_after_seconds || 0;
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
      .catch(() => {
        stopPolling();
        refreshBtn.disabled = false;
        setStatus("Cannot reach the refresh service. Please try again shortly.");
      });
  }

  refreshBtn.addEventListener("click", () => {
    refreshBtn.disabled = true;
    setStatus("Starting...");
    fetch(REFRESH_API + "/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (response.status === 202 || response.status === 409) {
          setStatus("Refreshing both dashboards...");
          schedule(1500);
          return;
        }
        if (response.status === 429 || data.error === "cooldown") {
          poll();
          return;
        }
        stopPolling();
        refreshBtn.disabled = false;
        if (response.status === 401) {
          setStatus("This server requires a refresh token (start it with --token).");
          return;
        }
        if (response.status === 403) {
          setStatus("Blocked: the request did not come from this dashboard.");
          return;
        }
        setStatus(data.message || data.error || "Refresh failed.");
      })
      .catch(() => {
        stopPolling();
        refreshBtn.disabled = false;
        setStatus("Cannot reach the refresh service. Please try again shortly.");
      });
  });

  poll();
})();
