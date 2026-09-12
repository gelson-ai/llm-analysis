#!/usr/bin/env python3
"""
Rebuild the price-vs-performance dashboard HTML from the pipeline's
normalized data. Pure local file I/O - no network access needed, so this
is safe to run from anywhere (including a sandboxed shell), unlike
run_pipeline.py which needs to reach openrouter.ai.

Usage:
    python build_dashboard.py [--max-age-hours 72]

Exit codes:
    0  - dashboard rebuilt successfully -> dashboard/price_performance_final.html
    1  - normalized data is missing or older than --max-age-hours (stale).
         Nothing is rebuilt in this case - the caller (e.g. a scheduled
         task) should treat this as "the weekly pull didn't happen" and
         notify rather than silently republishing old data as if it were new.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DASHBOARD_DIR = Path(__file__).resolve().parent
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import weekly_picks  # noqa: E402  (sibling module - needs the path insert above)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_PATH = PROJECT_ROOT / "data" / "normalized" / "models.json"
BENCHMARKS_PATH = PROJECT_ROOT / "data" / "normalized" / "model_benchmarks.json"
COVERAGE_PATH = PROJECT_ROOT / "data" / "analysis" / "coverage_report.json"
QUALITY_PATH = PROJECT_ROOT / "data" / "analysis" / "data_quality_report.json"
TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"
OUTPUT_PATH = Path(__file__).resolve().parent / "price_performance_final.html"
# Small sidecar describing the build. The deployed dashboard is static, so
# nothing on the server can answer "how old is this data?" - the refresh
# button's proxy reads this file instead of parsing the payload back out of
# the HTML. It is deliberately committed, not ignored.
STATUS_PATH = Path(__file__).resolve().parent / "status.json"

INPUT_WEIGHT = 3
OUTPUT_WEIGHT = 1


def check_freshness(max_age_hours: float) -> tuple[bool, str]:
    if not MODELS_PATH.exists():
        return False, f"{MODELS_PATH} does not exist - has the pipeline ever run?"

    with open(MODELS_PATH) as f:
        models = json.load(f)
    if not models:
        return False, "models.json exists but is empty"

    retrieved_at = models[0].get("retrieved_at")
    if not retrieved_at:
        return False, "models.json has no retrieved_at timestamp - can't verify freshness"

    try:
        retrieved_dt = datetime.fromisoformat(retrieved_at)
    except ValueError:
        return False, f"could not parse retrieved_at: {retrieved_at!r}"

    age_hours = (datetime.now(timezone.utc) - retrieved_dt).total_seconds() / 3600
    if age_hours > max_age_hours:
        return False, (
            f"data is {age_hours:.1f} hours old (retrieved_at={retrieved_at}), "
            f"older than the {max_age_hours}-hour freshness window - the weekly "
            f"pull may not have run"
        )
    return True, f"data is {age_hours:.1f} hours old (retrieved_at={retrieved_at}) - fresh"


def read_retrieved_at() -> Optional[str]:
    """The pipeline stamps every normalized model with retrieved_at; the first
    record's value is the snapshot timestamp for the whole run."""
    if not MODELS_PATH.exists():
        return None
    try:
        with open(MODELS_PATH, encoding="utf-8") as f:
            models = json.load(f)
    except (ValueError, OSError):
        return None
    if not models:
        return None
    return models[0].get("retrieved_at")


def data_age_hours(retrieved_at: Optional[str]) -> Optional[float]:
    if not retrieved_at:
        return None
    try:
        moment = datetime.fromisoformat(retrieved_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() / 3600


def dashboard_built_at() -> Optional[str]:
    if not OUTPUT_PATH.exists():
        return None
    return datetime.fromtimestamp(OUTPUT_PATH.stat().st_mtime, tz=timezone.utc).isoformat()


def model_count() -> Optional[int]:
    """Cheap inventory count for status endpoints - reads the small coverage
    report instead of re-parsing the full model payload."""
    if not COVERAGE_PATH.exists():
        return None
    try:
        with open(COVERAGE_PATH, encoding="utf-8") as f:
            coverage = json.load(f)
    except (ValueError, OSError):
        return None
    return (coverage.get("model_inventory") or {}).get("total_models")


def build_payload() -> dict:
    with open(MODELS_PATH) as f:
        models = json.load(f)
    with open(BENCHMARKS_PATH) as f:
        benchmarks = json.load(f)
    with open(COVERAGE_PATH) as f:
        coverage = json.load(f)
    with open(QUALITY_PATH) as f:
        quality = json.load(f)

    retrieved_at = models[0].get("retrieved_at") if models else None

    bench_by_model: dict[str, dict[str, float]] = defaultdict(dict)
    for b in benchmarks:
        if b.get("model_id") and b.get("benchmark_source") == "Artificial Analysis":
            bench_by_model[b["model_id"]][b["benchmark_name"]] = b["score"]

    rows = []
    for m in models:
        mid = m.get("model_id")
        if not mid:
            continue
        inp = m.get("input_price_per_mtok")
        out = m.get("output_price_per_mtok")
        has_price = bool(m.get("has_valid_pricing")) and not bool(m.get("is_dynamic_pricing"))
        blended = None
        if has_price and inp is not None and out is not None:
            blended = round((INPUT_WEIGHT * inp + OUTPUT_WEIGHT * out) / (INPUT_WEIGHT + OUTPUT_WEIGHT), 6)
        b = bench_by_model.get(mid, {})
        rows.append([
            mid,
            m.get("model_name"),
            m.get("provider"),
            blended,
            round(inp, 6) if inp is not None else None,
            round(out, 6) if out is not None else None,
            m.get("context_length"),
            b.get("AA Intelligence Index"),
            b.get("AA Coding Index"),
            b.get("AA Agentic Index"),
            has_price,
            bool(m.get("is_dynamic_pricing")),
        ])
    return {
        "rows": rows,
        "coverage": coverage,
        "quality": quality,
        "data_retrieved_at": retrieved_at,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-hours", type=float, default=72.0,
                         help="Refuse to rebuild if the underlying data is older than this (default 72h)")
    args = parser.parse_args()

    fresh, message = check_freshness(args.max_age_hours)
    if not fresh:
        print(f"STALE: {message}", file=sys.stderr)
        return 1
    print(f"OK: {message}")

    payload = build_payload()
    if not payload["rows"]:
        print("STALE: no models found after joining - refusing to publish an empty dashboard", file=sys.stderr)
        return 1

    # The week's locked pick travels with the payload, so the browser never has
    # to guess what "model of the week" means for the current week.
    payload["weekly"] = weekly_picks.embed_view(weekly_picks.load_history())

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__DATA__", json.dumps(payload, separators=(",", ":")))

    # Write to a temp file and swap it in, so a viewer can never load a
    # half-written dashboard.
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = OUTPUT_PATH.with_name(OUTPUT_PATH.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp_path, OUTPUT_PATH)

    # Written from the SAME payload object as the HTML above, so the two can
    # never disagree about which snapshot was published. model_count is the
    # row count the dashboard actually renders - not the coverage report's
    # fetched-inventory count, which is a different number in principle.
    status = {
        "version": 1,
        "retrieved_at": payload.get("data_retrieved_at"),
        "generated_at": payload.get("generated_at"),
        "model_count": len(payload["rows"]),
    }
    tmp_status_path = STATUS_PATH.with_name(STATUS_PATH.name + ".tmp")
    with open(tmp_status_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp_status_path, STATUS_PATH)

    priced_count = sum(1 for row in payload["rows"] if row[10])
    print(f"Rebuilt {OUTPUT_PATH} with {len(payload['rows'])} models ({priced_count} priced).")
    print(f"Wrote {STATUS_PATH} (retrieved_at={status['retrieved_at']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
