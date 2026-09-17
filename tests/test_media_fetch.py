"""Fetch tests for the media (image/video) catalogs.

Offline only - `requests.get` is monkeypatched in every test, same style as
tests/test_fetch.py. Fixtures are defined here rather than in conftest.py so no
pre-existing test file is touched.
"""
import json
from pathlib import Path

import pytest

from src.openrouter_client import (
    OpenRouterAPIError,
    fetch_image_model_endpoints,
    fetch_image_models,
    fetch_video_models,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


@pytest.fixture
def sample_image_models_response():
    return _load("media_image_models_response.json")


@pytest.fixture
def sample_video_models_response():
    return _load("media_video_models_response.json")


@pytest.fixture
def sample_image_endpoints_response():
    return _load("media_image_endpoints_response.json")


def _patch_get(monkeypatch, response_or_callable):
    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    def fake_get(url, timeout=None, headers=None):
        if callable(response_or_callable):
            return FakeResponse(response_or_callable(url))
        return FakeResponse(response_or_callable)

    monkeypatch.setattr("src.openrouter_client.requests.get", fake_get)


# ---------------------------------------------------------------------------
# fetch_image_models / fetch_video_models
# ---------------------------------------------------------------------------
def test_fetch_image_models_success(monkeypatch, sample_image_models_response):
    _patch_get(monkeypatch, sample_image_models_response)
    monkeypatch.setattr("src.openrouter_client.settings.MIN_EXPECTED_IMAGE_MODEL_COUNT", 1)

    result = fetch_image_models()

    assert result.ok is True
    assert len(result.raw_json["data"]) == len(sample_image_models_response["data"])


def test_fetch_video_models_success(monkeypatch, sample_video_models_response):
    _patch_get(monkeypatch, sample_video_models_response)
    monkeypatch.setattr("src.openrouter_client.settings.MIN_EXPECTED_VIDEO_MODEL_COUNT", 1)

    result = fetch_video_models()

    assert result.ok is True
    assert len(result.raw_json["data"]) == len(sample_video_models_response["data"])


def test_image_fetch_hits_the_dedicated_images_endpoint(monkeypatch, sample_image_models_response):
    seen = {}

    def route(url):
        seen["url"] = url
        return sample_image_models_response

    _patch_get(monkeypatch, route)
    monkeypatch.setattr("src.openrouter_client.settings.MIN_EXPECTED_IMAGE_MODEL_COUNT", 1)
    fetch_image_models()

    assert seen["url"] == "https://openrouter.ai/api/v1/images/models"


def test_video_fetch_hits_the_dedicated_videos_endpoint(monkeypatch, sample_video_models_response):
    seen = {}

    def route(url):
        seen["url"] = url
        return sample_video_models_response

    _patch_get(monkeypatch, route)
    monkeypatch.setattr("src.openrouter_client.settings.MIN_EXPECTED_VIDEO_MODEL_COUNT", 1)
    fetch_video_models()

    assert seen["url"] == "https://openrouter.ai/api/v1/videos/models"


def test_fetch_image_models_raises_on_empty_catalog(monkeypatch):
    _patch_get(monkeypatch, {"data": []})
    with pytest.raises(OpenRouterAPIError, match="zero image models"):
        fetch_image_models()


def test_fetch_video_models_raises_on_empty_catalog(monkeypatch):
    _patch_get(monkeypatch, {"data": []})
    with pytest.raises(OpenRouterAPIError, match="zero video models"):
        fetch_video_models()


def test_fetch_image_models_raises_on_non_list_data(monkeypatch):
    _patch_get(monkeypatch, {"data": {"not": "a list"}})
    with pytest.raises(OpenRouterAPIError, match="was not a list"):
        fetch_image_models()


def test_fetch_video_models_raises_on_missing_data_key(monkeypatch):
    _patch_get(monkeypatch, {"models": []})
    with pytest.raises(OpenRouterAPIError, match="missing 'data' key"):
        fetch_video_models()


def test_fetch_image_models_raises_below_configured_floor(monkeypatch, sample_image_models_response):
    """The floor must be read from settings at call time, and it must be the
    IMAGE floor - reusing MIN_EXPECTED_MODEL_COUNT (100) against this small
    catalog would falsely fail every run."""
    _patch_get(monkeypatch, sample_image_models_response)
    monkeypatch.setattr("src.openrouter_client.settings.MIN_EXPECTED_IMAGE_MODEL_COUNT", 1000)

    with pytest.raises(OpenRouterAPIError) as excinfo:
        fetch_image_models()

    assert "1000" in str(excinfo.value)
    assert len(sample_image_models_response["data"]) < 1000


def test_fetch_video_models_raises_below_configured_floor(monkeypatch, sample_video_models_response):
    _patch_get(monkeypatch, sample_video_models_response)
    monkeypatch.setattr("src.openrouter_client.settings.MIN_EXPECTED_VIDEO_MODEL_COUNT", 1000)

    with pytest.raises(OpenRouterAPIError) as excinfo:
        fetch_video_models()

    assert "1000" in str(excinfo.value)


def test_media_floor_is_not_the_chat_floor(monkeypatch, sample_image_models_response):
    """Guard against someone later 'simplifying' the media floors into the chat
    one: the live media catalogs are an order of magnitude smaller."""
    from config import settings

    assert settings.MIN_EXPECTED_IMAGE_MODEL_COUNT != settings.MIN_EXPECTED_MODEL_COUNT
    assert settings.MIN_EXPECTED_VIDEO_MODEL_COUNT != settings.MIN_EXPECTED_MODEL_COUNT
    assert settings.MIN_EXPECTED_IMAGE_MODEL_COUNT < settings.MIN_EXPECTED_MODEL_COUNT
    assert settings.MIN_EXPECTED_VIDEO_MODEL_COUNT < settings.MIN_EXPECTED_MODEL_COUNT

    # The fixture is a 4-model subset, so lower the floor for this call only -
    # what is being asserted above is the relationship between the constants.
    _patch_get(monkeypatch, sample_image_models_response)
    monkeypatch.setattr("src.openrouter_client.settings.MIN_EXPECTED_IMAGE_MODEL_COUNT", 1)
    assert fetch_image_models().ok is True


def test_fetch_image_models_raises_on_non_json_response(monkeypatch):
    _patch_get(monkeypatch, ValueError("no JSON object could be decoded"))
    with pytest.raises(OpenRouterAPIError):
        fetch_image_models()


def test_fetch_image_models_retries_on_500(monkeypatch, sample_image_models_response):
    calls = {"n": 0}

    class FailResponse:
        status_code = 500
        text = "server error"

    class OkResponse:
        status_code = 200
        text = ""

        def json(self):
            return sample_image_models_response

    def fake_get(url, timeout=None, headers=None):
        calls["n"] += 1
        return FailResponse() if calls["n"] < 2 else OkResponse()

    monkeypatch.setattr("src.openrouter_client.requests.get", fake_get)
    monkeypatch.setattr("src.openrouter_client.settings.MIN_EXPECTED_IMAGE_MODEL_COUNT", 1)
    monkeypatch.setattr("src.openrouter_client.settings.HTTP_RETRY_BACKOFF_SECONDS", 0.01)

    assert fetch_image_models().ok is True
    assert calls["n"] == 2


def test_fetch_video_models_raises_on_persistent_connection_failure(monkeypatch):
    import requests as requests_module

    def fake_get(url, timeout=None, headers=None):
        raise requests_module.exceptions.ConnectionError("boom")

    monkeypatch.setattr("src.openrouter_client.requests.get", fake_get)
    monkeypatch.setattr("src.openrouter_client.settings.HTTP_RETRY_BACKOFF_SECONDS", 0.01)

    with pytest.raises(OpenRouterAPIError):
        fetch_video_models()


def test_fetch_image_models_raises_on_http_error_status(monkeypatch):
    class NotFoundResponse:
        status_code = 404
        text = "not found"

        def json(self):
            return {}

    monkeypatch.setattr(
        "src.openrouter_client.requests.get",
        lambda url, timeout=None, headers=None: NotFoundResponse(),
    )
    with pytest.raises(OpenRouterAPIError, match="404"):
        fetch_image_models()


# ---------------------------------------------------------------------------
# fetch_image_model_endpoints - the one non-raising fetch
# ---------------------------------------------------------------------------
def test_fetch_image_model_endpoints_returns_payload(monkeypatch, sample_image_endpoints_response):
    seen = {}

    def route(url):
        seen["url"] = url
        return sample_image_endpoints_response["openai/gpt-image-2"]

    _patch_get(monkeypatch, route)

    result = fetch_image_model_endpoints("openai/gpt-image-2")

    assert result.ok is True
    assert seen["url"] == "https://openrouter.ai/api/v1/images/models/openai/gpt-image-2/endpoints"
    assert result.raw_json["id"] == "openai/gpt-image-2"


def test_fetch_image_model_endpoints_does_not_raise_on_failure(monkeypatch):
    """A single dead endpoints record must not destroy an otherwise-good run -
    the caller counts and reports failures instead."""
    class NotFoundResponse:
        status_code = 404
        text = "not found"

        def json(self):
            return {}

    monkeypatch.setattr(
        "src.openrouter_client.requests.get",
        lambda url, timeout=None, headers=None: NotFoundResponse(),
    )

    result = fetch_image_model_endpoints("some/model")

    assert result.ok is False
    assert result.status_code == 404
    assert result.error


def test_fetch_models_is_unaffected_by_media_fetchers(monkeypatch):
    """fetch_models() must keep working exactly as before - the media fetchers
    are additive and share no settings with it."""
    from src.openrouter_client import fetch_models

    payload = {"data": [{"id": "a/b"}]}
    _patch_get(monkeypatch, payload)
    monkeypatch.setattr("src.openrouter_client.settings.MIN_EXPECTED_MODEL_COUNT", 1)

    result = fetch_models()

    assert result.ok is True
    assert result.raw_json == payload
