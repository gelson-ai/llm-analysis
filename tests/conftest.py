import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_models_response():
    with open(FIXTURES_DIR / "sample_models_response.json") as f:
        return json.load(f)


@pytest.fixture
def sample_raw_models(sample_models_response):
    return sample_models_response["data"]
