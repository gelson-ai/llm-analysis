#!/usr/bin/env python3
"""
Single entrypoint for the OpenRouter data acquisition pipeline.

Usage:
    python run_pipeline.py [--no-snapshot]

Requires: pip install -r requirements.txt
Requires network access to https://openrouter.ai from wherever you run this.
"""
import argparse
import sys

from src.openrouter_client import OpenRouterAPIError
from src.pipeline import run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-snapshot", action="store_true", help="Skip writing a timestamped snapshot")
    args = parser.parse_args()

    try:
        summary = run(snapshot=not args.no_snapshot)
    except OpenRouterAPIError as exc:
        print(f"\nPIPELINE FAILED: {exc}\n", file=sys.stderr)
        print("This is a fail-loud error per the project's design principles - "
              "no partial dataset was written to data/normalized/. "
              "See data/raw/ for whatever raw response was captured, if any.", file=sys.stderr)
        return 1

    print(f"\nDone. Retrieved {summary['model_count']} models at {summary['retrieved_at']}.")
    print("See data/analysis/report.txt for the human-readable summary.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
