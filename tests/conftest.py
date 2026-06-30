import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def flow_alerts_raw() -> dict:
    return json.loads((FIXTURES_DIR / "flow_alerts.json").read_text())


@pytest.fixture
def gex_positive_raw() -> dict:
    return json.loads((FIXTURES_DIR / "gex_positive.json").read_text())


@pytest.fixture
def gex_negative_raw() -> dict:
    return json.loads((FIXTURES_DIR / "gex_negative.json").read_text())


@pytest.fixture
def gex_mixed_raw() -> dict:
    return json.loads((FIXTURES_DIR / "gex_mixed.json").read_text())


@pytest.fixture
def market_tide_raw() -> dict:
    return json.loads((FIXTURES_DIR / "market_tide.json").read_text())


@pytest.fixture
def darkpool_raw() -> dict:
    return json.loads((FIXTURES_DIR / "darkpool.json").read_text())
