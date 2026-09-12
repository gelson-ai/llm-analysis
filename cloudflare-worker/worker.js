/**
 * Refresh proxy for the OpenRouter price-vs-performance dashboard.
 *
 * WHY THIS EXISTS
 * The dashboard is a static page on GitHub Pages, so it cannot trigger a data
 * refresh by itself. Dispatching a GitHub Actions workflow needs a credential,
 * and a credential in the browser would be readable by every visitor. This
 * Worker holds that credential server-side and exposes exactly two endpoints:
 *
 *   GET  /status   -> data freshness + current refresh-job state
 *   POST /refresh  -> ask GitHub to run the "Publish dashboard" workflow
 *
 * The response shapes deliberately mirror dashboard/serve.py, because
 * dashboard/template.html was written against that contract and needs no
 * rework. Only the base URL changes.
 *
 * SETUP
 *   Required secret: GITHUB_TOKEN - a fine-grained PAT scoped to this ONE repo
 *   with "Actions: Read and write". Set it under
 *   Workers & Pages -> <worker> -> Settings -> Variables and Secrets.
 *   It must never appear in this file or in the browser.
 *
 * SECURITY NOTES
 *   - CORS is locked to ALLOWED_ORIGIN; a POST from any other origin gets 403.
 *   - The token is only ever sent to api.github.com, never to the browser.
 *   - Refresh is open to anyone by choice, so the cooldown below is the only
 *     brake. Tighten COOLDOWN_SECONDS, or require a token, if abused.
 */

const OWNER = "gelson-ai";
const REPO = "llm-analysis";
const WORKFLOW_FILE = "publish.yml"; // dispatches the weekly-refresh workflow
const BRANCH = "main";

// The only origin allowed to drive this Worker.
const ALLOWED_ORIGIN = "https://gelson-ai.github.io";

// Minimum gap between refreshes, matching serve.py's default.
const COOLDOWN_SECONDS = 600;

// A completed run only counts as "the job" for this long. Without this, every
// visitor loading the page would see a stale "ok" state and auto-reload once.
const RECENT_COMPLETION_SECONDS = 900;

// The page polls /status every 1-1.5s; cache so many viewers cannot exhaust
// GitHub's 5,000/hour authenticated API limit.
const STATUS_CACHE_SECONDS = 5;

// Weekly cadence is 168h; allow headroom before calling the data stale.
const STALE_AFTER_HOURS = 192;

// Only these triggers represent an actual DATA refresh. A `push` run
// republishes the site but fetches nothing, so counting it would disable the
// refresh button for the cooldown after every commit and make each new visitor
// auto-reload once (a push run looks like a just-finished refresh).
const REFRESH_EVENTS = new Set(["workflow_dispatch", "schedule"]);

const API = "https://api.github.com";
const CONTENT_TYPE = "application/json; charset=utf-8";

function corsHeaders(origin) {
  return {
    "access-control-allow-origin": origin,
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "Content-Type",
    "access-control-max-age": "86400",
    vary: "Origin",
  };
}

function jsonResponse(body, status, origin, extraHeaders) {
  const headers = {
    "content-type": CONTENT_TYPE,
    "cache-control": "no-store",
    ...(origin ? corsHeaders(origin) : {}),
    ...(extraHeaders || {}),
  };
  return new Response(JSON.stringify(body), { status, headers });
}

/** Authenticated call to the GitHub REST API for this one repository. */
async function github(path, token, options) {
  const opts = options || {};
  return fetch(`${API}/repos/${OWNER}/${REPO}${path}`, {
    method: opts.method || "GET",
    headers: {
      authorization: `Bearer ${token}`,
      accept: opts.accept || "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "llm-analysis-refresh-proxy",
    },
    body: opts.body,
  });
}

function secondsSince(iso) {
  if (!iso) return null;
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  return (Date.now() - ms) / 1000;
}

/** Latest workflow run, shaped into the job block template.html expects. */
async function readJob(token) {
  // Ask for several runs, not one: the most recent run may be a push (which
  // republishes without refreshing data), so we filter down to refresh events.
  const res = await github(
    `/actions/workflows/${WORKFLOW_FILE}/runs?per_page=10`,
    token,
  );
  if (!res.ok) {
    throw new Error(`GitHub run lookup failed with HTTP ${res.status}`);
  }
  const body = await res.json();
  const run = (body.workflow_runs || []).find((r) =>
    REFRESH_EVENTS.has(r.event),
  );

  const idle = {
    state: "idle",
    started_at: null,
    finished_at: null,
    result: null,
    cooldown_seconds: COOLDOWN_SECONDS,
    retry_after_seconds: 0,
  };
  if (!run) return idle;

  const startedIso = run.run_started_at || run.created_at;
  const elapsed = secondsSince(startedIso);
  const cooling =
    elapsed === null
      ? 0
      : Math.max(0, Math.ceil(COOLDOWN_SECONDS - elapsed));

  // queued / in_progress mean a refresh is genuinely in flight.
  if (run.status !== "completed") {
    return {
      state: "running",
      started_at: startedIso,
      finished_at: null,
      result: null,
      cooldown_seconds: COOLDOWN_SECONDS,
      retry_after_seconds: cooling,
    };
  }

  // Only a *recent* completion is "the job". An older one is just history.
  const sinceFinish = secondsSince(run.updated_at);
  const recent =
    sinceFinish !== null && sinceFinish <= RECENT_COMPLETION_SECONDS;
  if (!recent) {
    return { ...idle, retry_after_seconds: cooling };
  }

  const ok = run.conclusion === "success";
  return {
    state: ok ? "ok" : "failed",
    started_at: startedIso,
    finished_at: run.updated_at,
    result: ok
      ? { message: "Refresh completed." }
      : {
          error: "refresh_failed",
          message:
            "The refresh run failed, so the dashboard was left untouched. " +
            "See the Actions tab for the log.",
        },
    cooldown_seconds: COOLDOWN_SECONDS,
    retry_after_seconds: cooling,
  };
}

/**
 * Read dashboard/status.json straight from the repository.
 *
 * Deliberately the Contents API rather than raw.githubusercontent.com: the raw
 * host is CDN-cached, so it can serve a stale copy for minutes after a deploy
 * and the button would misreport how old the data is.
 */
async function readDataStatus(token) {
  const res = await github("/contents/dashboard/status.json", token, {
    accept: "application/vnd.github.raw+json",
  });
  if (!res.ok) {
    return {
      retrieved_at: null,
      age_hours: null,
      stale: true,
      model_count: null,
    };
  }
  let parsed;
  try {
    parsed = JSON.parse(await res.text());
  } catch {
    return {
      retrieved_at: null,
      age_hours: null,
      stale: true,
      model_count: null,
    };
  }

  const ageHours = (() => {
    const seconds = secondsSince(parsed.retrieved_at);
    return seconds === null ? null : seconds / 3600;
  })();

  return {
    retrieved_at: parsed.retrieved_at || null,
    age_hours: ageHours === null ? null : Math.round(ageHours * 100) / 100,
    stale: ageHours === null || ageHours > STALE_AFTER_HOURS,
    model_count: typeof parsed.model_count === "number" ? parsed.model_count : null,
  };
}

async function buildStatus(token) {
  const [job, data] = await Promise.all([
    readJob(token),
    readDataStatus(token),
  ]);
  return { ok: true, data, job };
}

async function cachedStatus(request, token) {
  const cacheKey = new Request(
    new URL("/__status_cache", request.url).toString(),
    { method: "GET" },
  );

  const hit = await caches.default.match(cacheKey);
  if (hit) return hit.json();

  const payload = await buildStatus(token);
  await caches.default.put(
    cacheKey,
    new Response(JSON.stringify(payload), {
      headers: {
        "content-type": CONTENT_TYPE,
        "cache-control": `max-age=${STATUS_CACHE_SECONDS}`,
      },
    }),
  );
  return payload;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin") || "";
    const originAllowed = origin === ALLOWED_ORIGIN;
    const replyOrigin = originAllowed ? ALLOWED_ORIGIN : null;

    // The dashboard POSTs JSON, which is not a "simple" request, so browsers
    // send a preflight. Answer it only for our own page.
    if (request.method === "OPTIONS") {
      if (!originAllowed) {
        return new Response(null, { status: 403 });
      }
      return new Response(null, { status: 204, headers: corsHeaders(ALLOWED_ORIGIN) });
    }

    if (url.pathname === "/healthz") {
      return jsonResponse({ ok: true }, 200, replyOrigin);
    }

    const token = env.GITHUB_TOKEN;
    if (!token) {
      return jsonResponse(
        {
          ok: false,
          error: "not_configured",
          message: "The GITHUB_TOKEN secret has not been set on this Worker.",
        },
        500,
        replyOrigin,
      );
    }

    if (url.pathname === "/status" && request.method === "GET") {
      try {
        return jsonResponse(await cachedStatus(request, token), 200, replyOrigin);
      } catch (err) {
        return jsonResponse(
          { ok: false, error: "status_unavailable", message: String(err) },
          502,
          replyOrigin,
        );
      }
    }

    if (url.pathname === "/refresh" && request.method === "POST") {
      // A browser always sends Origin on a cross-origin POST; requiring an
      // exact match also blocks non-browser callers that omit it.
      if (!originAllowed) {
        return jsonResponse(
          {
            ok: false,
            error: "forbidden_origin",
            message: "Refresh requests must come from the dashboard.",
          },
          403,
          null,
        );
      }

      let job;
      try {
        // Never cached: the cooldown and single-flight checks must be current.
        job = await readJob(token);
      } catch (err) {
        return jsonResponse(
          { ok: false, error: "status_unavailable", message: String(err) },
          502,
          ALLOWED_ORIGIN,
        );
      }

      if (job.state === "running") {
        return jsonResponse(
          {
            ok: false,
            error: "refresh_already_running",
            message: "A refresh is already running.",
          },
          409,
          ALLOWED_ORIGIN,
        );
      }

      if (job.retry_after_seconds > 0) {
        return jsonResponse(
          {
            ok: false,
            error: "cooldown",
            message: "A refresh completed recently.",
            retry_after_seconds: job.retry_after_seconds,
          },
          429,
          ALLOWED_ORIGIN,
          { "retry-after": String(job.retry_after_seconds) },
        );
      }

      // 204 No Content means GitHub accepted the dispatch.
      const dispatched = await github(
        `/actions/workflows/${WORKFLOW_FILE}/dispatches`,
        token,
        { method: "POST", body: JSON.stringify({ ref: BRANCH }) },
      );

      if (dispatched.status === 204) {
        return jsonResponse(
          { ok: true, state: "running", message: "Refresh started." },
          202,
          ALLOWED_ORIGIN,
        );
      }

      const detail = await dispatched.text();
      return jsonResponse(
        {
          ok: false,
          error: "dispatch_failed",
          message: `GitHub rejected the refresh request (HTTP ${dispatched.status}).`,
          detail: detail.slice(0, 300),
        },
        502,
        ALLOWED_ORIGIN,
      );
    }

    return jsonResponse({ ok: false, error: "not_found" }, 404, replyOrigin);
  },
};
