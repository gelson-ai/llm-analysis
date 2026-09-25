#!/usr/bin/env python3
"""
Single entrypoint for the VIDEO-generation model data pipeline.

Usage:
    python run_video_pipeline.py [--no-snapshot] [--print-pool]

Separate from run_pipeline.py (chat models) and run_media_pipeline.py (the image
catalogue and the image page's benchmark data), not a mode of either:

  - it reads /api/v1/videos/models and the VIDEO prompt-benchmark pages only;
  - it writes only VIDEO_DASHBOARD_* paths - data/raw and data/normalized
    video_dashboard_*.{json,csv}, data/analysis/video_dashboard_*.json and
    data/video_snapshots/;
  - it never touches a MEDIA_* path or a chat path, so the published image page
    (whose payload embeds the media coverage report) cannot be affected by a run;
  - it is wired into dashboard/refresh_video.py, never into dashboard/refresh.py
    or run_weekly.bat.

Requires: pip install -r requirements.txt
Requires network access to https://openrouter.ai from wherever you run this.
No per-model pricing requests are needed: video models publish their pricing_skus
on the catalogue endpoint itself.
"""
import argparse
import json
import sys

from config import settings
from src.media_benchmark_scraper import MediaBenchmarkParseError
from src.openrouter_client import OpenRouterAPIError
from src.video_coverage import BUDGET_MIN_PASS_RATE, summarize_pool
from src.video_pipeline import run


def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _number(value, spec: str) -> str:
    return "—" if value is None else format(value, spec)


def _catalogue_rates() -> dict[str, object]:
    """Catalogue USD-per-output-second per model, from the run's own output.

    Read back rather than threaded through the summary so the printed figure is
    provably the one that was written to disk.
    """
    try:
        with open(settings.VIDEO_DASHBOARD_MODELS_JSON_PATH) as handle:
            models = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {
        model.get("model_id"): model.get("comparable_price")
        for model in models
        if isinstance(model, dict)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-snapshot", action="store_true",
                        help="Skip writing a timestamped snapshot")
    parser.add_argument("--print-pool", action="store_true",
                        help="Also print every pooled model (pass rate, cost, value, catalogue "
                             "rate) - the distribution the ranking gate is set from.")
    args = parser.parse_args()

    try:
        summary = run(snapshot=not args.no_snapshot)
    except (OpenRouterAPIError, MediaBenchmarkParseError) as exc:
        print(f"\nVIDEO PIPELINE FAILED: {exc}\n", file=sys.stderr)
        print("This is a fail-loud error per the project's design principles - "
              "no partial dataset was written to data/. See data/raw/ for whatever "
              "raw response was captured, if any.", file=sys.stderr)
        return 1

    coverage = summary["coverage_report"]
    inventory = coverage["model_inventory"]
    gate = coverage["evidence_gate"]
    pool = summary["pool"]
    stats = summarize_pool(pool)

    print(f"\nDone. Retrieved {summary['video_model_count']} video model(s) at "
          f"{summary['retrieved_at']}.")
    print(f"Prompt benchmarks: {summary['prompt_benchmark_row_count']} row(s) across "
          f"{coverage['prompt_benchmark_coverage']['prompt_page_count']} prompt page(s); "
          f"{stats['models']} model(s) with judged rows.")
    print(f"Catalogue rate resolved: {inventory['models_with_resolved_price']}/"
          f"{inventory['total_models']} model(s)"
          + (f" (units: {', '.join(inventory['comparable_price_units_used'])})"
             if inventory["comparable_price_units_used"] else ""))
    print(f"Unrated (no judged row): {coverage['unrated']['count']} model(s)")
    print(f"Rankable by value: {stats['rankable']} of {stats['models']}  "
          f"(evidence gate: >= {gate['min_prompts']} prompts AND >= {gate['min_checks']} checks)")

    print("\nDistribution (per model, pooled across prompts):")
    print(f"  prompts   min {stats['prompts_min']}  max {stats['prompts_max']}")
    print(f"  checks    min {stats['checks_min']}  median {stats['checks_median']}  "
          f"max {stats['checks_max']}")
    print(f"  pass rate min {_pct(stats['pass_rate_min'])}  median {_pct(stats['pass_rate_median'])}"
          f"  max {_pct(stats['pass_rate_max'])}")
    print(f"  clearing the {int(BUDGET_MIN_PASS_RATE * 100)}% budget bar: "
          f"{stats['clearing_budget_pass_rate']} model(s)")
    if stats["gate_failures"]:
        print(f"  below the evidence gate: {stats['gate_failures']}")

    quality = summary["data_quality_report"]
    print(f"\nData quality: {quality['error_count']} error(s), {quality['warning_count']} "
          f"warning(s), {quality['info_count']} info item(s)")
    for issue in quality["issues"]:
        if issue["severity"] in ("error", "warning"):
            print(f"  [{issue['severity']}] {issue['issue']} x{issue['count']}")

    if args.print_pool:
        rates = _catalogue_rates()
        header = (f"{'model':42}{'rows':>5}{'prmpt':>6}{'checks':>7}{'pass%':>8}"
                  f"{'mean$':>9}{'value':>8}{'cat$/s':>9}")
        print("\n" + header)
        print("-" * len(header))
        for entry in sorted(pool.values(), key=lambda p: -(p["pass_rate"] or -1)):
            catalogue_rate = rates.get(entry["model_id"])
            flag = "" if entry["rankable"] else "  <- not rankable"
            print(f"{entry['model_id']:42}{entry['rows']:>5}{entry['prompts']:>6}"
                  f"{entry['evidence_checks']:>7}"
                  f"{_pct(entry['pass_rate']):>8}"
                  f"{_number(entry['avg_cost_usd'], '.3f'):>9}"
                  f"{_number(entry['value'], '.1f'):>8}"
                  f"{_number(catalogue_rate if isinstance(catalogue_rate, (int, float)) else None, '.3f'):>9}"
                  f"{flag}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
