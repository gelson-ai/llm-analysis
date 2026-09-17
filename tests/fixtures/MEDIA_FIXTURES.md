# Media (image/video) test fixtures

All fixtures here are used only by `tests/test_media_fetch.py`,
`tests/test_media_normalize.py` and `tests/test_media_benchmark_scraper.py`.
They are loaded with `json.load` / `read_text` and defined as pytest fixtures
**inside those test modules**, so `tests/conftest.py` — and every pre-existing
test — is deliberately untouched.

No test makes a network call.

## Provenance

| Fixture | Source | Notes |
| --- | --- | --- |
| `media_image_models_response.json` | Real `GET https://openrouter.ai/api/v1/images/models`, captured 2026-09-17 | Curated: 4 of the ~52 live models, chosen to cover the shape variance actually present (rich vs empty `supported_parameters`, image-only vs image+text `output_modalities`, SVG `output_format` enum). Objects are verbatim, unmodified API records. |
| `media_video_models_response.json` | Real `GET https://openrouter.ai/api/v1/videos/models`, captured 2026-09-17 | Curated: 6 of the ~29 live models, chosen to cover every `pricing_skus` vocabulary family observed live — cents-per-second, USD-per-second with resolution tiers, USD-per-video-token, cents-per-megapixel-second, per-generation minimum, and audio-qualified variants. Objects are verbatim, unmodified API records. |
| `media_image_endpoints_response.json` | Real `GET https://openrouter.ai/api/v1/images/models/{id}/endpoints`, captured 2026-09-17 | Curated: 3 models, one per `unit` value seen live (`token`, `image`, `megapixel`), keyed by model id. Values are verbatim API records. |
| `media_models_with_design_arena.json` | **Synthetic** | Neither media endpoint publishes a `benchmarks` block today (verified twice against live responses on 2026-09-17). This fixture exists so the Design Arena extraction path — which the feature requires — is covered by a test rather than shipping unexercised. Model ids are prefixed `example-vendor/` to make the synthetic provenance obvious. Its shape mirrors the real `benchmarks.design_arena` block already confirmed on chat models in `sample_models_response.json`. |
| `media_benchmark_page_videos_traffic_light.html` | Real `GET https://openrouter.ai/benchmarks/media/videos/traffic-light`, captured 2026-09-17 | **Real markup, not hand-written** — the entire risk in the scraper is markup drift, so a synthetic fixture would prove nothing. Trimmed to six `<li>` result-row blocks (3 models × both rendered copies) plus the real surrounding attributes; page chrome, nav and assets removed. Exercises `aria-label` extraction (`5 of 5 checks passed`), cost, generation time and duplicate-row collapsing against the actual document structure. |

## Why the curated subsets rather than full dumps

The full live responses are ~54 KB (image) / ~40 KB (video) and change weekly,
which would make failures noisy and diffs unreadable. The kept models are
deliberately the ones that exercise branching code paths; the complete
responses continue to be written verbatim to `data/raw/` by the pipeline itself.

## Refresh procedure

If OpenRouter changes these schemas, capture the new shapes the same way:

```
python run_media_pipeline.py --no-snapshot
```

then re-slice `data/raw/openrouter_image_models.json` and
`data/raw/openrouter_video_models.json` into these fixture files, keeping the
same "cover every branch" selection rule.
