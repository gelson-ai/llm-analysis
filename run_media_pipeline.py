#!/usr/bin/env python3
"""
Single entrypoint for the image- and video-generation model data pipeline.

Usage:
    python run_media_pipeline.py [--no-snapshot] [--skip-image-pricing]

This is a SEPARATE pipeline from run_pipeline.py, not a mode of it:

  - it reads the dedicated /api/v1/images/models and /api/v1/videos/models
    endpoints, whose pricing is SKU/unit based rather than token based;
  - it writes only new files (data/normalized/image_models.json, video_models.json,
    data/analysis/media_coverage_report.json, ...) - it never touches the chat
    pipeline's outputs;
  - it is deliberately NOT wired into dashboard/refresh.py or run_weekly.bat.

Requires: pip install -r requirements.txt
Requires network access to https://openrouter.ai from wherever you run this.
The image catalog needs one extra request per image model to read pricing
(see --skip-image-pricing).
"""
import argparse
import sys

from src.media_pipeline import run
from src.openrouter_client import OpenRouterAPIError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-snapshot", action="store_true", help="Skip writing a timestamped snapshot")
    parser.add_argument(
        "--skip-image-pricing",
        action="store_true",
        help="Skip the per-model image endpoints fan-out. Image models are then "
             "written with no price at all (the list endpoint publishes none).",
    )
    args = parser.parse_args()

    try:
        summary = run(
            snapshot=not args.no_snapshot,
            with_image_pricing=not args.skip_image_pricing,
        )
    except OpenRouterAPIError as exc:
        print(f"\nMEDIA PIPELINE FAILED: {exc}\n", file=sys.stderr)
        print("This is a fail-loud error per the project's design principles - "
              "no partial dataset was written to data/normalized/. "
              "See data/raw/ for whatever raw response was captured, if any.", file=sys.stderr)
        return 1

    print(f"\nDone. Retrieved {summary['image_model_count']} image and "
          f"{summary['video_model_count']} video models at {summary['retrieved_at']}.")
    inv = summary["coverage_report"]["model_inventory"]
    print(f"Models with a resolved price: {inv['total_with_resolved_price']} "
          f"(image: {inv['image']['models_with_resolved_price']}, "
          f"video: {inv['video']['models_with_resolved_price']})")
    print(f"Models with Design Arena scores: {inv['total_with_design_arena']}")
    prompt_coverage = summary["coverage_report"].get("prompt_benchmark_coverage") or {}
    if prompt_coverage.get("enabled"):
        print(f"Prompt benchmark rows: {prompt_coverage['rows']} across "
              f"{prompt_coverage['prompt_page_count']} prompt page(s); "
              f"{prompt_coverage['models_matched_to_inventory']} model(s) matched "
              f"({prompt_coverage['models_unmatched']} unmatched)")
    print("See data/analysis/media_coverage_report.json and "
          "data/analysis/media_data_quality_report.json for detail.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
