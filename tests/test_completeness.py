import pytest

from config import settings
from src.coverage import build_data_quality_report
from src.normalize import normalize_model
from src.openrouter_client import OpenRouterAPIError, _request_with_retries


def test_detect_duplicate_model_ids():
    raw_models = [
        {"id": "dupe/model", "name": "A", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        {"id": "dupe/model", "name": "B", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
    ]
    normalized = [normalize_model(m, "2026-09-07T00:00:00Z") for m in raw_models]
    report = build_data_quality_report(raw_models, normalized, [], [])
    dupe_issues = [i for i in report["issues"] if i["issue"] == "duplicate_model_id"]
    assert len(dupe_issues) == 1
    assert dupe_issues[0]["count"] == 1


def test_detect_missing_model_id():
    raw_models = [{"name": "No ID"}]
    normalized = [normalize_model(m, "2026-09-07T00:00:00Z") for m in raw_models]
    report = build_data_quality_report(raw_models, normalized, [], [])
    assert any(i["issue"] == "missing_model_id" for i in report["issues"])


def test_detect_duplicate_benchmark_records():
    normalized_benchmarks = [
        {"model_id": "a/b", "benchmark_name": "GPQA Diamond", "benchmark_version": None, "raw_model_ref": "a/b", "matched": True, "score_unit": "fraction_0_1"},
        {"model_id": "a/b", "benchmark_name": "GPQA Diamond", "benchmark_version": None, "raw_model_ref": "a/b", "matched": True, "score_unit": "fraction_0_1"},
    ]
    report = build_data_quality_report([], [], normalized_benchmarks, [])
    assert any(i["issue"] == "duplicate_benchmark_record" for i in report["issues"])


def test_suspiciously_low_model_count_raises(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = ""
        def json(self):
            return {"data": [{"id": f"vendor/model-{i}"} for i in range(5)]}

    def fake_get(url, timeout=None, headers=None):
        return FakeResponse()

    monkeypatch.setattr("src.openrouter_client.requests.get", fake_get)
    from src.openrouter_client import fetch_models
    with pytest.raises(OpenRouterAPIError, match="fewer than the configured"):
        fetch_models()


def test_zero_models_raises(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = ""
        def json(self):
            return {"data": []}

    def fake_get(url, timeout=None, headers=None):
        return FakeResponse()

    monkeypatch.setattr("src.openrouter_client.requests.get", fake_get)
    from src.openrouter_client import fetch_models
    with pytest.raises(OpenRouterAPIError, match="zero models"):
        fetch_models()


def test_malformed_top_level_shape_raises(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = ""
        def json(self):
            return {"unexpected": "shape"}

    def fake_get(url, timeout=None, headers=None):
        return FakeResponse()

    monkeypatch.setattr("src.openrouter_client.requests.get", fake_get)
    from src.openrouter_client import fetch_models
    with pytest.raises(OpenRouterAPIError, match="missing 'data' key"):
        fetch_models()
