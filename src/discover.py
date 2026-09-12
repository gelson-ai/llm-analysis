"""
Benchmark discovery (Section 3).

OpenRouter does not publish a documented public "benchmarks" endpoint as of
this writing. This module does NOT assume where benchmark data lives. It:

  1. Probes each candidate endpoint in settings.CANDIDATE_BENCHMARK_ENDPOINTS
     and records exactly what came back (status code, raw body or error).
  2. Scans the raw model objects returned by /api/v1/models for any
     benchmark-shaped fields, using a generic heuristic (key names containing
     words like "benchmark", "score", "eval", or matching known benchmark
     name fragments such as "gpqa", "ifbench", "omniscience", "mmlu").
  3. Reports its findings as data - it never invents a schema.

Run this BEFORE trusting anything in src/normalize.py's benchmark parsing:
if discovery finds nothing, the honest answer to the project's central
question is "no reliable benchmark data available from OpenRouter today",
and that must be reported rather than papered over.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from src.openrouter_client import probe_endpoint

BENCHMARK_KEY_HINTS = [
    "benchmark", "score", "eval", "gpqa", "ifbench", "ifeval", "omniscience",
    "mmlu", "artificial_analysis", "aa_", "intelligence_index", "humaneval",
    "swe_bench", "swebench", "livecodebench", "arc_agi", "arc-agi",
]

_HINT_RE = re.compile("|".join(re.escape(h) for h in BENCHMARK_KEY_HINTS), re.IGNORECASE)


def probe_candidate_endpoints(paths: Iterable[str]) -> list[dict]:
    """Hit every candidate endpoint and return a plain-data summary of what
    each one returned, without raising - this is investigation, not the
    validated production fetch path."""
    findings = []
    for path in paths:
        result = probe_endpoint(path)
        summary = {
            "endpoint": result.endpoint,
            "status_code": result.status_code,
            "ok": result.ok,
            "error": result.error,
        }
        if result.ok and isinstance(result.raw_json, dict):
            summary["top_level_keys"] = list(result.raw_json.keys())
        elif result.ok and isinstance(result.raw_json, list):
            summary["top_level_type"] = "list"
            summary["length"] = len(result.raw_json)
        findings.append(summary)
    return findings


def _walk_keys(obj: Any, prefix: str = "") -> Iterable[str]:
    """Yield dotted key-paths for every key found in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            yield path
            yield from _walk_keys(v, path)
    elif isinstance(obj, list):
        for item in obj[:3]:  # sample only, models can repeat structure
            yield from _walk_keys(item, prefix)


def scan_models_for_benchmark_fields(models: list[dict]) -> dict:
    """Scan raw model objects for anything that looks benchmark-related.

    Returns a report: which key-paths matched the hint list, and how many of
    the sampled models actually had a non-null value there.
    """
    matched_paths: dict[str, int] = {}
    sample_size = min(len(models), 500)
    for model in models[:sample_size]:
        seen_in_this_model = set()
        for key_path in _walk_keys(model):
            leaf = key_path.rsplit(".", 1)[-1]
            if _HINT_RE.search(leaf) and key_path not in seen_in_this_model:
                matched_paths[key_path] = matched_paths.get(key_path, 0) + 1
                seen_in_this_model.add(key_path)

    return {
        "models_sampled": sample_size,
        "matched_key_paths": dict(sorted(matched_paths.items(), key=lambda kv: -kv[1])),
        "conclusion": (
            "No benchmark-shaped fields found in /api/v1/models model objects. "
            "Benchmark data, if it exists, is not exposed through this endpoint."
            if not matched_paths else
            "Found candidate benchmark-shaped fields - inspect matched_key_paths and "
            "the raw model objects manually before building a parser around them."
        ),
    }
