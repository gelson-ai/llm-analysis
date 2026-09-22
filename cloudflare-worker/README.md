# Refresh proxy (Cloudflare Worker)

Makes the dashboard's **Refresh data** button work for everyone.

The dashboard is a static page on GitHub Pages, so it cannot start a data
refresh by itself. Dispatching the GitHub Actions workflow needs a credential,
and a credential in the browser would be readable by every visitor. This Worker
holds that credential securely on the server and exposes two endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /status` | Data freshness for **both** dashboards (`dashboard/status.json` + `dashboard/media_status.json`), the current refresh-job state, and the per-dashboard outcome of the last run (`dashboard/refresh_status.json`) |
| `POST /refresh` | Asks GitHub to run the `Publish dashboard` workflow, which refreshes **both** pipelines |

The response shapes mirror `dashboard/serve.py` on purpose, so the dashboard
pages need no rework — only a different base URL. One refresh dispatches one
workflow: `publish.yml` runs the chat and media pipelines as independent steps,
so one failing still leaves the other updated and published.

---

## Setup

### 1. Create the GitHub token

Sign in as the account that administers `gelson-ai/llm-analysis`.

Go to **https://github.com/settings/personal-access-tokens/new** and set:

- **Token name:** `llm-analysis refresh proxy`
- **Expiration:** your choice (90 days is a reasonable default; you will need to
  repeat this step when it lapses)
- **Repository access:** *Only select repositories* → `gelson-ai/llm-analysis`
- **Permissions:**
  - **Actions → Read and write** ← the only one needed
  - leave everything else as *No access*

Click **Generate token** and copy it. It is shown once.

> Never paste this token into a chat, an issue, a commit, or any file in the
> repository. It goes into Cloudflare in step 3 and nowhere else.

### 2. Create the Worker

In the Cloudflare dashboard:

1. **Workers & Pages → Create → Workers → Get started**
2. Name it `llm-refresh-proxy`
3. Choose **Edit code** (start from the "Hello World" template)
4. Replace the entire contents with `cloudflare-worker/worker.js`
5. **Deploy**

### 3. Store the token as a secret

1. **Workers & Pages → llm-refresh-proxy → Settings → Variables and Secrets**
2. **Add** → type **Secret** → name `GITHUB_TOKEN` → paste the token
3. **Save and deploy**

The value is write-only from then on: Cloudflare will never show it back to you.
If it is ever exposed, revoke it on GitHub and create a new one — that takes
about twenty seconds, which is the point of scoping it to a single repository.

### 4. Note the Worker URL

It looks like `https://llm-refresh-proxy.<your-subdomain>.workers.dev`.
That URL goes into `dashboard/template.html` as `REFRESH_API`.

---

## Verifying it works

```bash
# 1. Liveness - should return {"ok":true}
curl https://llm-refresh-proxy.<your-subdomain>.workers.dev/healthz

# 2. Status - should return data.retrieved_at and job.state
curl https://llm-refresh-proxy.<your-subdomain>.workers.dev/status

# 3. Cross-origin POST is refused - should return 403
curl -i -X POST -H "Origin: https://evil.example.com" \
  https://llm-refresh-proxy.<your-subdomain>.workers.dev/refresh

# 4. The real thing - should return 202, then 429 if repeated within 10 minutes
curl -i -X POST -H "Origin: https://gelson-ai.github.io" \
  -H "Content-Type: application/json" -d '{}' \
  https://llm-refresh-proxy.<your-subdomain>.workers.dev/refresh

# 5. Both dashboards are reported - data.media must be present, and job.targets
#    must name chat + media once a run has recently finished (null while idle)
curl -s https://llm-refresh-proxy.<your-subdomain>.workers.dev/status \
  | python -c "import json,sys; d=json.load(sys.stdin); print(sorted(d['data'])); print(d['job']['targets'])"
```

---

## Endpoint contract

`GET /status`

```json
{
  "ok": true,
  "data": {
    "retrieved_at": "2026-09-12T02:02:11Z",
    "age_hours": 0.5,
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

`data` holds the chat pipeline's freshness fields plus a `media` block for the
image dashboard, each read from its own sidecar file. `job.targets` reports the
last run per dashboard, e.g.
`{"chat": {"state": "failed", "message": "…"}, "media": {"state": "ok"}}` —
that is how the button can say "Chat: failed — …, Image: updated." after a single
click.

`POST /refresh` returns `202` accepted, `409` already running, `429` during the
cooldown (with a `Retry-After` header), or `403` for a foreign origin.

## Design notes

- **Both status sidecars are read through the GitHub Contents API**, not
  `raw.githubusercontent.com`. The raw host is CDN-cached and can serve a stale
  copy for minutes after a deploy, which would make the button misreport how old
  the data is.
- **Per-dashboard outcomes are recency-gated.** `refresh_status.json` is a
  committed file holding whatever the last run wrote — including a *local* run —
  so the Worker only trusts it when its `generated_at` is at or after the
  workflow run it is attributed to, and only while a run is recent. Without that
  guard a stale file would be reported as a brand-new result.
- **Only a *recent* completed run counts as the job state.** Without that, every
  visitor would see a lingering `ok` state and auto-reload once on page load.
- **`/status` is cached for 5 seconds.** The page polls roughly once per second
  during a cooldown; ten open tabs would otherwise burn tens of thousands of
  GitHub API calls an hour against a 5,000/hour limit.
- **The cooldown applies to every run type**, including the weekly cron. Ten
  minutes after a scheduled refresh, the button will report a countdown. That is
  intentional — there is nothing new to fetch that soon.

## Tuning

Edit the constants at the top of `worker.js`:

| Constant | Default | Effect |
| --- | --- | --- |
| `COOLDOWN_SECONDS` | `600` | Minimum gap between refreshes |
| `RECENT_COMPLETION_SECONDS` | `900` | How long a finished run counts as "the job" |
| `STATUS_CACHE_SECONDS` | `5` | `/status` cache lifetime |
| `STALE_AFTER_HOURS` | `192` | When the data is flagged stale (weekly cadence + headroom) |
| `ALLOWED_ORIGIN` | Pages URL | The only origin permitted to drive the Worker |
