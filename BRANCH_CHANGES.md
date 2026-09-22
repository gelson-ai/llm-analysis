# Branch change report — `feature/image-model-dashboard`

**Base:** `1e9712f` (the image-dashboard phase, already published)
**Head:** `e8fc404` · **7 commits** · pushed to `origin/feature/image-model-dashboard`
**Tests:** 196 → **237 passing** (+41) · **65 files changed** (−2,841 deletions)

> Pushing this branch does **not** deploy: `publish.yml`'s push trigger is
> `main`-only. Nothing in this report has reached the live site.

Of the ~170k insertions, roughly **1.8k are code and tests**; the rest is refreshed
pipeline data (`data/**`) plus two media snapshots that a real pipeline run produced.

---

## 1. The seven requested items

| # | Item | Status | Where |
| --- | --- | --- | --- |
| 1 | Shared navigation toggle, extensible to a third tab | ✅ | `dashboard/nav_tabs.py`, `web_assets/shell.js` |
| 2 | One "Update All Latest AI Data" button, both pipelines kept independent, per-dashboard reporting | ✅ | `refresh_media.py`, `refresh_all.py`, `serve.py`, `publish.yml`, `worker.js` |
| 3 | Flag value leaders with no Design Arena backing | ✅ | `image_template.html` (hero chip + bar marker + caption) |
| 4 | Per-category Design Arena summary callout | ✅ | `build_design_arena_summary()`, category-leaders panel |
| 5 | Document the unrated-coverage ceiling | ✅ | `#unratedNote` |
| 6 | Sample output thumbnails | ⚠️ **Feasibility spike only** — implementation deliberately deferred to its own plan |
| 7 | Resolution / latency capture | ✅ | payload capture + "Generated at" / "Gen. time" columns + corrected caveat |

---

## 2. Commits

| Commit | Scope |
| --- | --- |
| `6e69d7f` | Shared tab nav across both dashboards · fixes the CI gate that was blocking deploys |
| `563942f` | Unified refresh, **local** half (`refresh_media.py`, `refresh_all.py`, `refresh_lock.py`, `serve.py`, shared refresh control) |
| `380e6b6` | Unified refresh, **deployed** half (`publish.yml` targets + Worker status contract) |
| `460a02d` | Items 3 + 5: preference-data flag, coverage-ceiling note |
| `63caea8` | Item 4: category leaders |
| `cdc81e4` | Item 7: resolution/duration capture · fixes two upstream breaks |
| `e8fc404` | Docs: `DEPLOYMENT.md`, `README.md` |

---

## 3. Architecture

**New modules**

| File | Role |
| --- | --- |
| `dashboard/nav_tabs.py` | Single source of truth for the tab bar. A third (video) dashboard is **one entry here**. |
| `dashboard/web_assets/shell.js` | Shared page shell: tabs + theme toggle + unified refresh control, injected into every page. |
| `dashboard/page_shell.py` | Injects the shell at `__SHELL_JS__`, together with the tab data. |
| `dashboard/refresh_media.py` | Media sibling of `refresh.py`: own lock, own status file, own exit codes. |
| `dashboard/refresh_all.py` | Runs both pipelines as separate processes; reports per-target outcomes. |
| `dashboard/refresh_lock.py` | Canonical `RefreshLock`. `refresh.py` keeps its own copy (frozen surface). |

**Why the pipelines stayed separate.** They share no code path, no lock file, no
status file and no output. `refresh_all.py` only spawns them. A media failure
therefore cannot prevent the chat dashboard from updating, and vice versa — which
is exactly what makes the per-dashboard reporting possible.

**Status contract**

- `dashboard/status.json` — chat freshness (unchanged shape)
- `dashboard/media_status.json` — media freshness, same shape, separate file
- `dashboard/refresh_status.json` — per-target outcome of the last unified run
- `job.targets` is the **same key** on both backends: `serve.py` lifts it from its
  in-process result, the Worker from the committed file, and the page reads only one thing.

---

## 4. Behaviour a user will notice

- Both pages carry tab navigation with the current page marked; the chip on the
  hero card shows **"No preference data"** when a value leader has no Design Arena row.
- The button reads **"Update All Latest AI Data"** and refreshes both dashboards in
  one click. On a partial failure it reports per dashboard — e.g.
  `Chat: failed — The dashboard rebuild was refused… Image: updated.` — and
  deliberately does **not** reload, so the message stays readable.
- The image page's reference table gained **"Generated at"** and **"Gen. time"**;
  models whose rows span several sizes are marked `(mixed)`.
- The cost-comparability caveat now states a measured fact instead of a hedge.
- `start_dashboard.bat` passes `--max-age-hours 192` so the button works out of the
  box on weekly-cadence data (the server default of 72 h would fail the chat half).

---

## 5. Pre-existing bugs found and fixed (outside the seven items)

1. **The pytest gate was red, so the site could not deploy.**
   `tests/test_image_dashboard.py` pinned `RETRIEVED_AT` to 2026-09-17 while asserting a
   72 h freshness window. `check_freshness()` compares against the real clock, so the
   suite went red on ~2026-09-20 purely from elapsed time — and `publish.yml` runs pytest
   as a hard gate before committing and deploying. Fixture now anchored to `now - 1h`;
   the stale case is still covered by an explicit 30-day offset. Audited the rest of the
   suite: `test_weekly_picks.py`'s frozen dates are safe (its helpers take the moment as
   an argument).

2. **The media pipeline could not run at all.** OpenRouter added
   `/benchmarks/media/images/portraits`, which publishes 192 result-row blocks and 144
   generated assets but **no judged pass count anywhere** — the word "checks" does not
   appear in its 1.6 MB document. The scraper needs a check marker to recognise a row, so
   the page parsed to zero rows and the pipeline's zero-row guard aborted the entire run.
   Fixed with `page_publishes_judged_checks()`, which distinguishes the two empty results:
   no markers anywhere → **skip and record**; markers present but no rows → **still raise**.

3. **A latent script-order trap in the shell design.** `template.html` runs its payload
   script before the shell; the image template runs the shell first (so the theme applies
   before first paint). A shell reading `PAYLOAD` would have worked on one page and
   silently rendered zero tabs on the other. Tab data is injected *with* the code, and a
   test strips comments and asserts the shell never references `PAYLOAD`.

---

## 6. Item 6 (thumbnails) — investigation result

**Scenario (b) applies: we never generate images ourselves.** There is no generation code
anywhere (GET-only, no credentials), so nothing is discarded after scoring. OpenRouter's
benchmark pages already embed the outputs as a first-class JSON field:

- `asset.thumbnailUrl` (`thumb-0.webp`, ~5–18 KB) and `asset.url` (`original-0.jpg`, ~160–360 KB)
- Assets are fetchable unauthenticated: `200`, `cache-control: public, max-age=31536000, immutable`

So hotlinking is viable and mirroring is optional, at no API cost. **Not implemented** —
deferred to its own plan per the agreed scope, which still needs decisions on hotlink vs
mirror and on which rows get a thumbnail.

---

## 7. Item 7 — the premise was wrong, in a useful way

The plan assumed no per-row resolution existed. It does, in the page's embedded payload,
at 100% coverage: **867/867 rows** now carry output resolution, duration and a thumbnail URL.
Capture is additive — pass counts stay authoritative from the rendered markup, because
`score.checks` is empty on exactly the pages that would break a payload-only parser — and no
previously published number moved.

The caveat can now be **answered rather than hedged**: resolution is not held fixed, spanning
768→2560 px on the long edge, with cost tracking it and 7 models generating at more than one
size. Notably the #1 value leader, Meta: Muse Image, generates at **1600×1600** while most
competitors are at 1024×1024 — and is still the cheapest, so its value win is not a resolution
artefact.

Two sources of the same measurement are also cross-checked: the rendered badge (16.5 s) and
the payload (16 462 ms) agree within rounding, and a test asserts it.

---

## 8. Data

The media pipeline was run for real (retrieved **2026-09-22**), which is why `data/**` moves
with `cdc81e4`: 867 rows across 27 pages, 1 page skipped and recorded, 52 image / 29 video
models, 13 with Design Arena. Two media snapshots are included because the pipeline
snapshots on every run; the once-per-day relaxation lives in `refresh_media.py`, not the pipeline.

---

## 9. Verification performed

| Check | Result |
| --- | --- |
| Full suite | ✅ 237 passed |
| Tabs, active state, both directions, both pages | ✅ browser-verified |
| Header right edge at 1440 px | ✅ still x = 1206–1302 |
| Unified refresh, both pipelines | ✅ `Chat: updated. Image: updated.` |
| Unified refresh, one pipeline failing | ✅ chat failed / media updated, chat HTML untouched, no reload |
| Nav test not vacuous | ✅ mutation-checked (points a tab at an unstaged file → fails) |
| Chart label overflow after adding markers | ✅ widest 883 of a 900 viewBox; nothing spills |
| `worker.js` | ✅ `node --check` (now a test, skipped where node is absent) |
| Worker status-file paths | ✅ asserted against what the pipelines actually write |

---

## 10. Deferred

- **Item 6 thumbnails** — spike done, implementation needs its own plan.
- **Video dashboard / third tab** — designed for (one entry in `nav_tabs.py`), not built.
- **`refresh_status.json` is a committed file holding the last run's result**, currently my
  local failure-isolation test result. The Worker's recency guard makes that harmless, and
  the next real refresh overwrites it — but it is worth knowing before reviewing the diff.
- **Polling cadence:** after a completed refresh the page polls `/status` roughly once a
  second for the whole 600 s cooldown. Pre-existing, carried over unchanged, worth a
  follow-up if it ever runs against the Worker's API quota.

---

## 11. Decisions the owner may want to revisit

1. **Skipping unjudged pages** (`portraits`) rather than failing, on the reasoning that a page
   with no judgements is a different page type. If portrait-style pages deserve their own
   treatment — they do publish useful assets and costs — that is a feature, not a bug fix.
2. **`--max-age-hours 192` in `start_dashboard.bat`** changed the local default behaviour
   (the server's own default is still 72).
3. **Bar scale for category leaders** was locked to win rate on a fixed 0–100 axis with Elo
   in the label, per the earlier decision.

---

## 12. Reviewing locally

```bash
.\.venv\Scripts\python.exe -m pytest tests/ -q          # 237 passing, offline
.\.venv\Scripts\python.exe .\dashboard\serve.py --open --max-age-hours 192
# then: both pages, tabs, and one click of "Update All Latest AI Data"
```

Rebuild either page directly:

```bash
.\.venv\Scripts\python.exe .\dashboard\build_dashboard.py --max-age-hours 192
.\.venv\Scripts\python.exe .\dashboard\build_image_dashboard.py
```

> Both builders must be re-run after a template edit — the workflow stages the
> **committed** HTML rather than rebuilding it. `DEPLOYMENT.md` §10 covers the rest.
