import pytest

from src.openrouter_client import OpenRouterAPIError, fetch_models


def test_fetch_models_success(monkeypatch, sample_models_response):
    class FakeResponse:
        status_code = 200
        text = ""
        def json(self):
            return sample_models_response

    def fake_get(url, timeout=None, headers=None):
        return FakeResponse()

    monkeypatch.setattr("src.openrouter_client.requests.get", fake_get)
    monkeypatch.setattr("src.openrouter_client.settings.MIN_EXPECTED_MODEL_COUNT", 1)

    result = fetch_models()
    assert result.ok is True
    assert len(result.raw_json["data"]) == len(sample_models_response["data"])


def test_fetch_models_retries_on_500(monkeypatch, sample_models_response):
    calls = {"n": 0}

    class FailResponse:
        status_code = 500
        text = "server error"

    class OkResponse:
        status_code = 200
        text = ""
        def json(self):
            return sample_models_response

    def fake_get(url, timeout=None, headers=None):
        calls["n"] += 1
        return FailResponse() if calls["n"] < 2 else OkResponse()

    monkeypatch.setattr("src.openrouter_client.requests.get", fake_get)
    monkeypatch.setattr("src.openrouter_client.settings.MIN_EXPECTED_MODEL_COUNT", 1)
    monkeypatch.setattr("src.openrouter_client.settings.HTTP_RETRY_BACKOFF_SECONDS", 0.01)

    result = fetch_models()
    assert result.ok is True
    assert calls["n"] == 2


def test_fetch_models_raises_on_non_json_response(monkeypatch):
    class BadResponse:
        status_code = 200
        text = "<html>not json</html>"
        def json(self):
            raise ValueError("no JSON object could be decoded")

    def fake_get(url, timeout=None, headers=None):
        return BadResponse()

    monkeypatch.setattr("src.openrouter_client.requests.get", fake_get)
    with pytest.raises(OpenRouterAPIError):
        fetch_models()


def test_fetch_models_raises_on_persistent_connection_failure(monkeypatch):
    import requests as requests_module

    def fake_get(url, timeout=None, headers=None):
        raise requests_module.exceptions.ConnectionError("boom")

    monkeypatch.setattr("src.openrouter_client.requests.get", fake_get)
    monkeypatch.setattr("src.openrouter_client.settings.HTTP_RETRY_BACKOFF_SECONDS", 0.01)
    with pytest.raises(OpenRouterAPIError):
        fetch_models()
