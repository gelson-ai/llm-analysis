/* Shared page shell for the OpenRouter dashboards.
 *
 * Injected into every dashboard at build time at the __SHELL_JS__ placeholder
 * (see the page builders), so the pages cannot drift apart. It owns two things:
 *
 *   1. the theme toggle - behaviour unchanged from when it lived inline, just
 *      shared instead of copied;
 *   2. the dashboard tab bar.
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
})();
