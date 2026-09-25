# Deployment Architecture

How the OpenRouter price-vs-performance dashboard is built, published, and
refreshed in production — including the design decisions, the verified timings,
and the failure modes that were found the hard way.

> **Status:** live and verified end-to-end.
> Dashboard: <https://gelson-ai.github.io/llm-analysis/>
> Refresh proxy: `https://llm-refresh-proxy.gelson-a26.workers.dev/`

---

## 1. What this is

Two things that used to be one:

1. **A Python data pipeline** (`run_pipeline.py` + `src/`) that scrapes
   `https://openrouter.ai/api/v1/models`, normalizes it, and writes JSON/CSV
   into `data/`.
2. **A dashboard** — a single self-contained HTML file built from that data by
   `dashboard/build_dashboard.py`.

In production neither runs on anyone's machine. The scrape happens on GitHub's
runners, the HTML is served by GitHub Pages, and a small Cloudflare Worker lets
any visitor trigger a refresh on demand without ever seeing a credential.

**Total running cost: $0.** Unlimited Actions minutes (public repo), free
GitHub Pages, and the Cloudflare Workers free tier.

---

## 2. Architecture at a glance

```mermaid
flowchart TB
    subgraph browser["Visitor's browser"]
        PAGE["Dashboard HTML<br/>(GitHub Pages)"]
    end

    subgraph cf["Cloudflare"]
        WORKER["Worker: llm-refresh-proxy<br/>holds GITHUB_TOKEN"]
    end

    subgraph gh["GitHub"]
        REPO["Repo: gelson-ai/llm-analysis<br/>(public)"]
        WF["Actions workflow<br/>publish.yml"]
        RUNNER["ubuntu-latest runner"]
        PAGES["GitHub Pages"]
        API["REST API"]
    end

    OR["OpenRouter API<br/>/api/v1/models<br/>/api/v1/{images,videos}/models<br/>/benchmarks/media/*"]

    PAGE -- "GET /status (poll ~1s)" --> WORKER
    PAGE -- "POST /refresh (click)" --> WORKER
    WORKER -- "dispatch workflow" --> API
    WORKER -- "read status.json +<br/>media_status.json +<br/>refresh_status.json<br/>(Contents API)" --> API
    API --> REPO

    WF -- "weekly cron / dispatch / push" --> RUNNER
    RUNNER -- "scrape chat" --> OR
    RUNNER -- "scrape media" --> OR
    RUNNER -- "commit data + HTML back" --> REPO
    RUNNER -- "upload artifact + deploy" --> PAGES
   PAGES -- "serves all three pages" --> PAGE
```

**The one rule that shapes everything:** the dashboards are *static files*. They
cannot rewrite data or republish themselves. Every dynamic behaviour is either a
scheduled CI job or a Worker call.

**Three pipelines, one button.** `publish.yml` runs `dashboard/refresh_all.py`,
which spawns `refresh.py` (chat), `refresh_media.py` (image) and
`refresh_video.py` (video) as sequential, separate processes. They have separate
locks, status files and outputs, so one failing leaves the others updated.

---

## 3. Components

| Component | What it does | Where it lives |
| --- | --- | --- |
| **Data pipeline** | Scrapes OpenRouter, normalizes, writes `data/**` | `run_pipeline.py`, `src/**` |
| **Media pipeline** | Scrapes the image/video catalogs + media prompt benchmarks | `run_media_pipeline.py`, `src/media_*.py` |
| **Dashboard build** | Renders `data/**` into one self-contained HTML file | `dashboard/build_dashboard.py`, `dashboard/template.html` |
| **Image dashboard build** | Same, for the image-generation page | `dashboard/build_image_dashboard.py`, `dashboard/image_template.html` |
| **Video pipeline** | Fetches and normalizes the video catalogue and video prompt benchmarks | `run_video_pipeline.py`, `src/video_*.py` |
| **Video dashboard build** | Same, for the video-generation page | `dashboard/build_video_dashboard.py`, `dashboard/video_template.html` |
| **Refresh orchestrator (chat)** | Fetch → lock weekly picks → rebuild, as one command | `dashboard/refresh.py` |
| **Refresh orchestrator (media)** | Fetch media data → lock the image Model of the Week → rebuild the image page, as one command | `dashboard/refresh_media.py` |
| **Refresh orchestrator (video)** | Fetch video data → lock the video Model of the Week → rebuild the video page, as one command | `dashboard/refresh_video.py` |
| **Unified refresh** | Runs all three pipelines sequentially as separate processes, with per-target outcomes | `dashboard/refresh_all.py` |
| **Weekly picks (chat)** | Locks one "Model of the Week" per AA metric, Monday–Sunday | `dashboard/weekly_picks.py`, `data/analysis/weekly_picks.json` |
| **Weekly picks (image)** | Locks one "Model of the Week" by value, Monday–Sunday | `dashboard/media_picks.py`, `data/analysis/media_weekly_picks.json` |
| **Weekly picks (video)** | Locks one "Model of the Week" by value, Monday–Sunday | `dashboard/video_picks.py`, `data/analysis/video_dashboard_weekly_picks.json` |
| **CI workflow** | Runs the refresh, commits results, publishes Pages | `.github/workflows/publish.yml` |
| **Static host** | Serves the built HTML publicly over HTTPS | GitHub Pages |
| **Refresh proxy** | Holds the GitHub token; exposes `/status` + `/refresh` | `cloudflare-worker/worker.js` |
| **Credential** | Fine-grained PAT, one repo, `Actions: read+write` | Cloudflare Worker secret `GITHUB_TOKEN` |
| **Local dev server** | Serves the same HTML + endpoints on loopback | `dashboard/serve.py` |
| **Shared page shell** | Tab nav + theme toggle + refresh control, injected into all three pages | `dashboard/web_assets/shell.js`, `dashboard/page_shell.py`, `dashboard/nav_tabs.py` |

**`dashboard/refresh.py` is deliberately untouched by this deployment.** It was
written as a standalone CLI precisely so CI could call the identical command,
and it is a frozen surface: the image and video pipelines were added as
*siblings* rather than modes of it. `refresh_all.py` only spawns the three; it
merges neither their code nor their outputs.

---

## 4. Data flows

### 4.1 Weekly scheduled refresh

Fires on cron `0 0 * * 1` (UTC), i.e. **Monday 08:00 Asia/Manila**.

```
cron → checkout → install deps → pytest → refresh_all.py --targets all
                                       --chat-max-age-hours 192 --media-max-age-hours 192
                         --video-max-age-hours 192
     → commit data/** + dashboard/** as github-actions[bot]
   → stage index.html + status.json + image_model_analysis.html + video_model_analysis.html
     → configure-pages → upload artifact → deploy Pages
     → (only now) fail the run if a dashboard failed to refresh
```

`refresh_all.py` runs three independent pipelines and reports each outcome:

1. **chat** — `dashboard/refresh.py`: `run_pipeline.py` (network) → lock this
   week's picks → `build_dashboard.py`. **If the fetch fails, that pipeline
   aborts without rebuilding**, so a bad scrape can never replace a good
   dashboard.
2. **media** — `dashboard/refresh_media.py`: `run_media_pipeline.py` (network,
   ~90s) → `build_image_dashboard.py`, on the same fail-without-rebuilding rule.
3. **video** — `dashboard/refresh_video.py`: `run_video_pipeline.py` → lock this
   week's video pick → `build_video_dashboard.py`, with its own snapshot root
   (`data/video_snapshots/`) and the same fail-without-rebuilding rule.

A failure in one **does not skip the other**, and the workflow keeps going so
whatever succeeded is still committed and deployed. The run is marked failed
only *after* the deploy, because the refresh button's proxy reads the run's
conclusion. Per-dashboard detail lands in `dashboard/refresh_status.json`.

The bot commit uses the built-in `GITHUB_TOKEN`, and **commits made with
`GITHUB_TOKEN` do not re-trigger workflows** — so there is no loop. Pages is
deployed later in the same job rather than by a separate workflow.

### 4.2 On-demand refresh (the "Update All Latest AI Data" button)

One button on all three pages, refreshing all pipelines in a single dispatch.

```
visitor clicks
  → POST https://llm-refresh-proxy…workers.dev/refresh   (Origin: gelson-ai.github.io)
  → Worker validates Origin, checks cooldown + single-flight
  → POST /repos/…/actions/workflows/publish.yml/dispatches   → 204
   → Worker replies 202; page polls /status while the three targets run
  → run completes → /status returns state + job.targets
  → ALL targets ok  → page reloads and shows the updates
  → ANY target failed → page does NOT reload, so the per-dashboard
      summary stays readable ("Chat: failed - … Image: updated.")
```

**Measured, not estimated:** click → fresh published data in **~37 seconds**
for the chat pipeline (clicked 04:26:10Z, new data retrieved 04:26:33Z, run
finished 04:26:47Z). The media pipeline adds **~90 s** of its own (52 per-model
pricing calls + 27 benchmark pages).

### 4.3 Push-triggered republish

A push to `main` touching `dashboard/**`, `data/**`, `src/**`, `config/**`,
`requirements.txt`, or the workflow file itself republishes the site — but
**skips the scrape**. Re-scraping on every commit would waste API calls and
could hit the freshness gate with committed data.

---

## 5. Refresh button API contract

The Worker deliberately mirrors `dashboard/serve.py`'s response shapes, so
`template.html` needed only a base-URL change.

`GET /status`

```json
{
  "ok": true,
  "data": {
    "retrieved_at": "2026-09-12T04:26:33Z",
    "age_hours": 0.01,
    "stale": false,
    "model_count": 445,
    "media": {
      "retrieved_at": "2026-09-17T03:40:50Z",
      "age_hours": 122.85,
      "stale": false,
      "model_count": 52
    }
  },
  "job": {
    "state": "idle",
    "started_at": null,
    "finished_at": null,
    "result": null,
    "targets": null,
    "cooldown_seconds": 600,
    "retry_after_seconds": 0
  }
}
```

- `data` keeps the **chat** pipeline's fields at the top level for backwards
  compatibility with the already-deployed page, and carries the image
  dashboard's own block under `data.media`. Each comes from that pipeline's own
  status sidecar (`dashboard/status.json`, `dashboard/media_status.json`).
- `job.targets` is the per-dashboard outcome of the last unified refresh:
  `{"chat": {"state": "ok|failed|already_running", "message": …}, "media": {…}}`.
  It is the SAME key on both backends — `dashboard/serve.py` lifts it from its
  in-process result, the Worker from the committed `refresh_status.json`.
- **The Worker's attribution is recency-gated**: `refresh_status.json` is a
  committed file, so it is only trusted when its `generated_at` is at or after
  the workflow run it is being reported against, and only while a run is recent.
  Without that guard a stale file would be reported as a fresh outcome.

`job.state` ∈ `idle | running | ok | failed`.

`POST /refresh` → `202` accepted · `409` already running · `429` cooldown (with
`Retry-After`) · `403` foreign origin · `502` GitHub rejected the dispatch.

`GET /healthz` → `{"ok":true}` — liveness only, needs no token.

**Client base-URL resolution** (in `dashboard/template.html`):

```js
const REFRESH_API = (location.hostname === "localhost" ||
                     location.hostname === "127.0.0.1")
  ? "/api"                                        // serve.py's routes
  : "https://llm-refresh-proxy.gelson-a26.workers.dev";
```

Both contexts then use `${REFRESH_API}/status`, so **local development through
`serve.py` keeps working unchanged.**

---

## 6. Security model

| Concern | Mitigation |
| --- | --- |
| Token must never reach the browser | Held only as a Worker secret; the page has no credential and POSTs no auth header |
| Token scope | Fine-grained PAT, **only** `gelson-ai/llm-analysis`, **only** `Actions: read+write` |
| Abuse of a public refresh button | 600 s cooldown + single-flight; the Worker rejects a second dispatch |
| Other sites driving the Worker | CORS locked to `https://gelson-ai.github.io`; `POST` requires an **exact** Origin match, and a missing Origin is rejected too |
| API rate-limit exhaustion | `/status` cached 5 s; otherwise ~10 open tabs would burn tens of thousands of GitHub API calls/hour against a 5,000 limit |
| Credential in the repo | `.gitignore` excludes `.env*`; the token exists only in Cloudflare |
| Blast radius if leaked | One repo, Actions only — no code write, no data read beyond what's public. Rotating takes ~20 s |

**Not protected (deliberate, per requirements):** anyone can press Refresh. The
cooldown is the only brake. See §10 for how to tighten it.

---

## 7. Configuration reference

### Workflow — `.github/workflows/publish.yml`

| Setting | Value |
| --- | --- |
| Triggers | `schedule` `0 0 * * 1`, `workflow_dispatch` (`all`/`chat`/`media`/`video`), `push` to `main` (path-filtered) |
| Permissions | `contents: write`, `pages: write`, `id-token: write` |
| Concurrency | group `publish-dashboard`, `cancel-in-progress: false` |
| Timeout | 45 minutes (must exceed 3 x `refresh_all.py`'s per-target 600 s) |
| Refresh flags | `--targets` plus explicit 192-hour chat, media and video windows |
| Runner | `ubuntu-latest`, Python 3.12, pip cache |

### Worker — `cloudflare-worker/worker.js`

| Constant | Default | Effect |
| --- | --- | --- |
| `ALLOWED_ORIGIN` | `https://gelson-ai.github.io` | Only origin allowed to drive the Worker |
| `COOLDOWN_SECONDS` | `600` | Minimum gap between refreshes |
| `RECENT_COMPLETION_SECONDS` | `900` | How long a finished run counts as "the job" |
| `STATUS_CACHE_SECONDS` | `5` | `/status` cache lifetime |
| `STALE_AFTER_HOURS` | `192` | When data is flagged stale (weekly cadence + headroom). Applies to all dashboards |
| `REFRESH_EVENTS` | `workflow_dispatch`, `schedule` | Runs that count as a data refresh |
| `STATUS_FILES` | `chat`/`media`/`video` → one sidecar each | Separate files keep one broken run from hiding another dashboard's freshness |
| `REFRESH_STATUS_FILE` | `dashboard/refresh_status.json` | Per-target outcome, read with a recency guard (see below) |

### Local server — `dashboard/serve.py`

| Flag | Default | Effect |
| --- | --- | --- |
| `--host` / `--port` | `127.0.0.1` / `8765` | Loopback only unless changed |
| `--token` | none | Require a token on `POST /api/refresh` |
| `--cooldown-seconds` | `600` | Minimum gap between refreshes |
| `--max-age-hours` | `72` | Freshness window passed to the **chat** build |
| `--media-max-age-hours` | `192` | Freshness window passed to the **image** build. Deliberately wider: media shares the weekly cadence and its build has always defaulted to 192 |
| `--video-max-age-hours` | `192` | Freshness window passed to the **video** build |
| `--timeout-seconds` | `2100` | Whole-refresh ceiling; must exceed 3 x the 600-second per-target ceiling |
| `--skip-fetch` | off | Rebuild from disk, no network (all pipelines) |

### Dependencies — `requirements.txt`

**Pinned exactly** (`requests==2.34.2`, `pytest==9.1.1`). With `>=` ranges CI
would install whatever PyPI released most recently — not what was verified
locally — so the test gate could fail for reasons unrelated to this repo.

---

## 8. First-time setup (reproduce from scratch)

1. **Create a public GitHub repo** and push the code.
   Public is required: GitHub Pages won't publish from a private repo on a free
   account, and public repos get unlimited Action minutes.
2. **Create `.gitignore` first** — exclude `.venv/`, caches, `logs/`,
   `data/analysis/.refresh.lock`. Do **not** exclude `data/**` or
   `dashboard/price_performance_final.html`; those are the artifact and the
   history. Verify with `git ls-files --cached | findstr /i venv` (must be empty).
3. **Enable Pages:** Settings → Pages → Source = **"GitHub Actions"**.
   Must be done by hand — see §10.1.
4. **Verify the account's email is verified** (github.com/settings/emails).
   An unverified email can prevent a Pages site from publishing.
5. **Push the workflow**, then run it once manually from the Actions tab to
   produce the first published dashboard.
6. **Create a fine-grained PAT:** <https://github.com/settings/personal-access-tokens/new>
   → only this repo → **Actions: Read and write** → nothing else.
7. **Create the Cloudflare Worker** from the *"Start with Hello World!"*
   template, **not** "Continue with GitHub". Paste `cloudflare-worker/worker.js`.
8. **Add the token:** Worker → Settings → Variables and Secrets → Add →
   type **Secret** → name `GITHUB_TOKEN`.
9. **Point the dashboard at it:** set `REFRESH_API` in `dashboard/template.html`,
   rebuild, commit, push.

---

## 9. Operations runbook

### 9.1 Health checks

```bash
# Worker alive (no token needed)
curl https://llm-refresh-proxy.gelson-a26.workers.dev/healthz
#   → {"ok":true}

# Full status: data freshness + job state
curl https://llm-refresh-proxy.gelson-a26.workers.dev/status
#   → {"ok":true,"data":{...},"job":{"state":"idle",...}}
#   → "not_configured" means the GITHUB_TOKEN secret is missing

# Foreign origin must be refused
curl -i -X POST -H "Origin: https://evil.example.com" \
  https://llm-refresh-proxy.gelson-a26.workers.dev/refresh
#   → 403

# The real thing
curl -i -X POST -H "Origin: https://gelson-ai.github.io" \
  -H "Content-Type: application/json" -d '{}' \
  https://llm-refresh-proxy.gelson-a26.workers.dev/refresh
#   → 202, then 429 if repeated within 10 minutes

# Is the data actually fresh? (add ?cb=<random> to dodge caches)
curl "https://gelson-ai.github.io/llm-analysis/status.json?cb=1"
```

Locally, before pushing a change to the build:

```powershell
.\.venv\Scripts\python.exe .\dashboard\build_dashboard.py
.\.venv\Scripts\python.exe -m pytest tests/ -q      # 59 tests, fully offline
```

### 9.2 Failure modes

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Button says *"Cannot reach the refresh service"* | Worker deleted/renamed, or URL in `template.html` is stale | Check `/healthz`; fix `REFRESH_API`; rebuild |
| `/status` returns `not_configured` | `GITHUB_TOKEN` secret missing | Re-add it in Cloudflare |
| `/status` returns `502 status_unavailable` | Token expired or lost its permissions | Rotate the token (§9.3) |
| Button says *"next refresh available in Ns"* for 10 min | Cooldown — expected after any refresh run | Wait, or lower `COOLDOWN_SECONDS` |
| POST returns `403 forbidden_origin` | Page served from an origin other than `ALLOWED_ORIGIN` | Update the constant and redeploy the Worker |
| Weekly refresh never commits | Workflow disabled after ~60 days of repo inactivity, or a failing test | Check the Actions tab; re-enable; read the log |
| Workflow fails at *Run tests* | A dependency or code change broke a test | Reproduce locally with `pytest` |
| Workflow fails at *Configure Pages* | Pages not enabled, or set to "Deploy from a branch" | Settings → Pages → Source = "GitHub Actions" |
| Site shows old data after a successful run | GitHub Pages CDN (can lag minutes) | Re-check shortly; not a fault |
| `git push` rejected, `behind 1` | The workflow committed to the remote; your clone is stale | `git pull --rebase origin main`, then push |

### 9.3 Rotating the GitHub token

The PAT expires (a 90-day expiry is typical). **When it lapses, both the button
and the Monday scrape stop — the dashboard silently stops updating.** This is
the single most likely future breakage; put a reminder somewhere.

1. Create a replacement PAT (same settings: one repo, `Actions: read+write`).
2. Cloudflare → `llm-refresh-proxy` → Settings → Variables and Secrets →
   edit `GITHUB_TOKEN` → paste the new value → Save/Deploy.
3. Confirm with `curl …/status` — a `502` means it still isn't right.
4. Revoke the old token on GitHub.

No code changes and no redeploy of the dashboard are needed.

### 9.4 Changing the schedule

Edit the cron in `.github/workflows/publish.yml`. Cron is **UTC**.

| Desired (Asia/Manila, UTC+8, no DST) | Cron |
| --- | --- |
| Monday 08:00 | `0 0 * * 1` |
| Monday 06:00 | `0 22 * * 0` |
| Daily 08:00 | `0 0 * * *` |

GitHub's scheduler is best-effort — expect a few minutes' drift under load.

---

## 10. Known limitations & gotchas

These were all discovered by testing, not by reading documentation. They are
recorded here so they aren't re-learned.

1. **`GET /repos/{owner}/{repo}/pages` returns 404 when unauthenticated even
   when Pages *is* enabled.** It looks like a definitive check and isn't. The
   reliable signal is the `has_pages` field on the repository object.
2. **`actions/configure-pages`' `enablement: true` cannot work here.** Its
   `action.yml` states it "requires a token other than `GITHUB_TOKEN`" (a PAT or
   GitHub App). Enabling Pages is therefore a manual one-time click. The
   alternative would be putting a broad PAT in Actions secrets to save one
   dropdown — not worth the privilege.
3. **Push-triggered runs must not count as data refreshes.** Treating every
   recent run as "the job" disabled the button for 10 minutes after *every
   commit* and made each new visitor auto-reload once. Hence `REFRESH_EVENTS`.
4. **`raw.githubusercontent.com` is CDN-cached** and can serve a stale
   `status.json` for minutes after a deploy, which would make the button
   misreport data age. The Worker reads it through the GitHub **Contents API**.
5. **The `.refresh.lock` file only guards one machine.** Two runners have
   separate filesystems, so the workflow's `concurrency` group is the real
   guard against two refreshes interleaving and locking conflicting picks.
6. **Freshness gate mismatch:** `build_dashboard.py` refuses to build data older
   than 72 h by default, but a weekly cadence is 168 h. The workflow passes
   `--max-age-hours 192`. A fetch-then-build run always passes anyway; this is
   headroom.
7. **`git add -A` is not selective.** Ignore rules must exist *before* the first
   commit — removing a large binary from history afterwards means a rewrite.
8. **GitHub's Actions API lags** ~1 minute before a new run appears in
   `/actions/runs`.
9. **Commits made with `GITHUB_TOKEN` do not trigger workflows** — this prevents
   a loop, but it also means a separate "deploy on push" workflow would never
   fire from the bot commit. Hence deploy-in-the-same-job.
10. **Your local clone goes stale.** The workflow commits to `main`, so
    `git push` can be rejected with `behind 1`. Always `git pull --rebase` first.
11. **Don't use Cloudflare's "Continue with GitHub" / "Connect to Git" path**
    for this Worker. It creates a *Workers Build* that fails on every push
    because there's no `wrangler.toml` at the repo root (ours is in
    `cloudflare-worker/`). A stray Worker from that path may still exist and
    show failing builds.
12. **`on:` parses as boolean `True` in PyYAML.** `yaml.safe_load(...)["on"]`
    raises `KeyError`; use `[True]`. Not a workflow bug.
13. **GitHub disables scheduled workflows after ~60 days of repository
    inactivity.** The weekly bot commit counts as activity, so this should
    self-sustain — but if the schedule ever stops for no reason, check here.
14. **Token expiry is silent.** Nothing warns you; the dashboard just stops
    updating. See §9.3.
15. **The two templates disagree about script order, on purpose.**
    `template.html` runs its payload script before the shared shell; the image
    template runs the shell FIRST, so the theme is applied before first paint.
    A shell that read `PAYLOAD` would therefore work on one page and silently
    render zero tabs on the other. The tab data is injected WITH the code as
    `const NAV_TABS=[…]` for exactly this reason, and a test strips comments and
    asserts the shell never references `PAYLOAD`.
16. **`refresh_status.json` is a committed file holding the last run's result —
    including a local one.** The Worker only trusts it when its `generated_at`
    is at or after the workflow run it is attributed to, and only while a run is
    recent. Removing that recency guard would make the live button report a
    stale outcome as a fresh one.
17. **Running `refresh.py` (even with `--skip-fetch`) LOCKS the week's picks**
    from whatever data is on disk. A verification refresh on stale data
    therefore locks a stale-derived Model of the Week that the design then
    refuses to overwrite mid-week; undo with
    `git checkout -- data/analysis/weekly_picks.json`.
18. **A benchmark page can publish assets but no judged checks.**
    `/benchmarks/media/images/portraits` does exactly that (2026-09-22): 192
    result-row blocks, 144 assets, and the word "checks" nowhere in a 1.6 MB
    document. The scraper requires a check marker to recognise a row, so such a
    page parses to zero rows. It is now SKIPPED and recorded (coverage report +
    data-quality report) rather than failing the whole run — but a page that
    publishes markers and still yields no rows still raises, so real markup
    drift stays loud.
19. **A `--skip-fetch` media run still snapshots.** `run_media_pipeline.py`
    writes `data/media_snapshots/<date>[-N]` on every run; the once-per-day
    relaxation lives in `refresh_media.py`, not in the pipeline. Running the
    pipeline twice in a day commits two snapshot directories.

---

## 11. Cost

| Service | Tier | Notes |
| --- | --- | --- |
| GitHub repo | Free, public | Unlimited Actions minutes on public repos |
| GitHub Pages | Free | Public repos only, on a free account |
| Cloudflare Workers | Free | 100k requests/day; `/status` caching keeps usage tiny |
| OpenRouter API | Free | `GET /api/v1/models` needs no key |

Private repos would change this: Pages needs GitHub Pro, and Actions drops to
2,000 minutes/month.

---

## 12. File map

```
.github/workflows/publish.yml     CI: refresh all pipelines + commit + deploy Pages
cloudflare-worker/worker.js       Refresh proxy (the only credential holder)
cloudflare-worker/wrangler.toml   Config for optional CLI deploys
cloudflare-worker/README.md       Worker setup + curl checks

dashboard/nav_tabs.py             THE tab list: adding a dashboard is one entry here
dashboard/web_assets/shell.js     Shared page shell: tabs + theme toggle + refresh control
dashboard/page_shell.py           Injects the shell at __SHELL_JS__ (with the tab data)
dashboard/template.html           Chat page source; all CSS/DOM/JS
dashboard/image_template.html     Image page source; byte-identical <style> blocks
dashboard/video_template.html     Video page source, derived from the image template
dashboard/build_dashboard.py      Chat: renders template + data → HTML and status.json
dashboard/build_image_dashboard.py Image: renders image_template → image_model_analysis.html
dashboard/build_video_dashboard.py Video: renders video_template → video_model_analysis.html
dashboard/refresh.py              CHAT: fetch → lock picks → rebuild (frozen surface)
dashboard/refresh_media.py        MEDIA: fetch → lock the image pick → rebuild the image page
dashboard/refresh_video.py        VIDEO: fetch → lock the video pick → rebuild the video page
dashboard/refresh_all.py          Runs all three, reports per-dashboard outcomes
dashboard/refresh_lock.py         Shared lock implementation (refresh.py keeps its own copy)
dashboard/weekly_picks.py         Monday–Sunday pick locking (UTC+8), chat metrics
dashboard/media_picks.py          Monday–Sunday pick locking (UTC+8), image value metric
dashboard/video_picks.py          Monday–Sunday pick locking (UTC+8), video value metric
dashboard/serve.py                Local-only dev server (loopback)
dashboard/price_performance_final.html   GENERATED — never hand-edit
dashboard/image_model_analysis.html      GENERATED — never hand-edit
dashboard/video_model_analysis.html      GENERATED — never hand-edit
dashboard/status.json             GENERATED — CHAT freshness sidecar
dashboard/media_status.json       GENERATED — MEDIA freshness sidecar
dashboard/video_status.json       GENERATED — VIDEO freshness sidecar
dashboard/refresh_status.json     GENERATED — per-target outcome of the last unified run

data/normalized/*.json|csv        CHAT and MEDIA pipeline outputs share this directory.
                                  models.json / model_benchmarks.json are chat;
                                  image_models / video_models / media_benchmarks /
                                  media_prompt_benchmarks are media. Never assume.
data/analysis/weekly_picks.json   Weekly pick history (tracked)
data/analysis/media_*_report.json Media coverage + data-quality reports (tracked)
data/snapshots/<date>/            CHAT snapshots (tracked)
data/media_snapshots/<date>/      MEDIA snapshots: deliberately a SEPARATE top-level
                                  directory - see the note below
data/video_snapshots/<date>/      VIDEO snapshots: also a separate top-level directory
data/analysis/.refresh.lock       Machine-local runtime lock (IGNORED)
data/analysis/.media_refresh.lock MEDIA's own lock - NOT the chat one (IGNORED)
data/analysis/.video_refresh.lock VIDEO's own lock - NOT either existing lock (IGNORED)

DEPLOYMENT.md                     This document
requirements.txt                  Pinned runtime + test dependencies
```

> **The image and video pipelines are part of this deployment**, but remain
> separate from chat and from each other. They share one workflow run
> (because a `GITHUB_TOKEN` commit does not re-trigger workflows, so a separate
> media workflow could never deploy its own output), and nothing else.
>
> Three consequences worth knowing:
>
> - **The three pipelines never share a lock file.** Their chat, media and video
>   locks are deliberately distinct: a stuck run must not block another target.
>   Tests assert the paths differ,
>   and `.gitignore` covers both (`data/analysis/.*_refresh.lock`) so CI's
>   `git add -A data` cannot commit a lock.
> - Media snapshots live in **`data/media_snapshots/`**, not `data/snapshots/`.
>   That separation is load-bearing: `snapshot_exists_for()` in
>   `dashboard/refresh.py` infers "today's chat snapshot already ran" from nothing
>   more than a directory named `<date>` (or `<date>-N`) under `data/snapshots/`,
>   so a media snapshot written there would silently suppress that day's real
>   chat snapshot. Locked in by `tests/test_media_snapshot_paths.py`.
> - `dashboard/media_status.json` and `dashboard/refresh_status.json` are **not
>   staged into `_site`**. The Worker reads them from the repository via the
>   Contents API, and nothing in the browser requests them directly.

---

## 13. Design decisions log

| Decision | Why |
| --- | --- |
| Static site + CI, no always-on server | Free, and the PC can be off |
| Reuse `refresh.py` unchanged in CI | It already sequence-guards fetch→build and aborts without republishing on a failed fetch |
| Deploy Pages in the same job as the refresh | `GITHUB_TOKEN` commits don't re-trigger workflows |
| Concrete `concurrency` group, no cancel | Prevents two runs locking conflicting weekly picks; never kills a run mid-commit |
| Test step blocks the weekly refresh | A wrong block is loud and re-runnable; a wrong publish is silent and public |
| Skip the scrape on `push` | Committed data may be old, and an API call per commit is wasteful |
| Worker mirrors `serve.py`'s contract | Keeps `template.html` changes to a single constant |
| Short `/status` cache | The page polls ~1/s; uncached, ten tabs would exhaust the API limit |
| `REFRESH_API` = `/api` locally, Worker URL in public | Local `serve.py` development keeps working |
| Fine-grained PAT, one repo, Actions only | Smallest blast radius; rotation is ~20 seconds |
| Token in a Cloudflare **Secret**, never in git | A credential in the browser is readable by every visitor |
| Media pipeline as a SIBLING of `refresh.py`, not a mode of it | The media pipeline must never be able to break the chat dashboard, and `refresh.py` is a frozen surface. Separate processes, separate locks, separate status files, separate exit codes |
| One workflow, not two | A `GITHUB_TOKEN` commit does not re-trigger workflows, so a media-only workflow's output could never be deployed by itself |
| A refresh failure fails the run only AFTER the deploy | Whatever succeeded is still published; the run conclusion is what the button reports |
| Navigation and the refresh control in ONE injected shell | Two templates already carried a copy-pasted theme toggle; a third page is coming, and copy-paste is how a two-way toggle becomes an N-way mess |
| Tab data injected with the shell code, not read from the payload | The templates order their scripts differently on purpose, so a payload read would silently render no tabs on one page |
| Refresh status per target, recency-gated in the Worker | Lets one click report "Chat: failed — … Image: updated." without a stale committed file being mistaken for a fresh result |
| `job.targets` as the same key on both backends | The page must not care whether it is talking to `serve.py` or the Worker |
| Per-pipeline freshness windows (72 h chat / 192 h media) | Media shares the weekly cadence; forcing the chat window on it would refuse legitimately fresh media data |
| Capture resolution + duration from the page's embedded payload | Additive, zero extra API cost, and it turns the cost-comparability caveat from an assumption into a measurement |
| Pass counts stay authoritative from the rendered markup | `score.checks` is empty on some pages, so a payload-only parser would lose them |
| Skip a page with no judged checks, but record it | A page publishing assets with no judgements is a different page type; its cost-only rows would break the rule that price and performance come from the same rows. Recorded in both reports so it is never silent |

---

## 14. Verification history

Recorded so "it works" is a claim with evidence.

| Check | Result |
| --- | --- |
| Push run (`event: push`) | ✅ success, 25 s; scrape correctly **skipped** |
| Manual dispatch (`workflow_dispatch`) | ✅ success, 38 s; scrape ran; 437 → 445 models |
| Weekly pick across a mid-week refresh | ✅ pick unchanged; `weekly_picks.json` diff was **1 line** (`updated_at` only) |
| Snapshot policy in CI | ✅ `data/snapshots/2026-09-12/` created (5 files) |
| `.refresh.lock` in CI | ✅ not committed |
| Real button click, browser → Worker → GitHub | ✅ run #4, `event: workflow_dispatch` |
| Click → fresh published data | ✅ **~37 s** |
| Live `status.json` after the run | ✅ same new `retrieved_at` |
| Weekly pick across a *user-triggered* refresh | ✅ still "Z.ai: GLM 5.3 Flash", locked 11 Sep 03:09 PHT |
| Test suite | ✅ 59 passed when this section was first written; **237 passed** after the unified-nav-and-refresh work |
| Worker syntax | ✅ `node --check`, exit 0 (now enforced by a test, skipped where node is absent) |
| Tabs on all three pages, active state | ✅ verified in-browser; refresh button and theme toggle intact |
| Unified refresh, all pipelines, one click | ✅ `Chat: updated. Image: updated. Video: updated.` with all targets named |
| Unified refresh, one pipeline failing | ✅ with a deliberately impossible chat window: `Chat: failed — The dashboard rebuild was refused… Image: updated.` — media still published, chat kept its previous HTML, page did NOT reload so the message stayed readable |
| Nav test is not vacuous | ✅ mutation-checked: pointing a tab at an unstaged file fails with `"the image tab links to MUTATION_TEST_not_staged.html, which the workflow never stages"` |
| Media data capture completeness | ✅ 867/867 rows carry output resolution, duration and thumbnail URL |
| Resolution really is not held fixed | ✅ live spread 768–2560 px on the long edge; 7 models generated at more than one size. The #1 value leader generates at 1600×1600 while most competitors are at 1024×1024 — and is still cheapest |
| Chart labels after adding markers | ✅ measured: widest 883 of a 900 viewBox (value leaders), 749 of 900 (category leaders). No overflow past the scroll container |
| Media pipeline before the portraits fix | ❌ aborted — `MediaBenchmarkParseError` on a page with no judged checks. Fixed, then ✅ 867 rows across 27 pages with 1 page skipped and recorded |
