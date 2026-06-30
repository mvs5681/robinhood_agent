"""Unit tests for MockUWTools — verifies the fixture adapter returns parseable data."""

from pathlib import Path

import pytest

from trader.uw.mock_tools import MockUWTools
from trader.uw.validators import (
    parse_darkpool_prints,
    parse_flow_alerts,
    parse_market_tide,
    parse_spot_gex_by_strike,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def mock_tools() -> MockUWTools:
    return MockUWTools(FIXTURES)


class TestMockUWTools:
    def test_get_flow_alerts_parseable(self, mock_tools):
        raw = mock_tools.get_flow_alerts()
        alerts = parse_flow_alerts(raw)
        assert len(alerts) >= 1

    def test_get_market_tide_parseable(self, mock_tools):
        raw = mock_tools.get_market_tide()
        ticks = parse_market_tide(raw)
        assert len(ticks) >= 1

    def test_get_darkpool_ticker_parseable(self, mock_tools):
        raw = mock_tools.get_darkpool_ticker(ticker="AAPL")
        prints = parse_darkpool_prints(raw)
        assert len(prints) >= 1

    def test_get_spot_exposures_by_strike_positive(self, tmp_path):
        src = FIXTURES / "gex_positive.json"
        (tmp_path / "AAPL_spot_gex.json").write_text(src.read_text())
        tools = MockUWTools(tmp_path)
        raw = tools.get_spot_exposures_by_strike(ticker="AAPL")
        strikes = parse_spot_gex_by_strike(raw)
        assert len(strikes) == 7
        assert all(s.net_gex > 0 for s in strikes)

    def test_missing_fixture_raises_file_not_found(self, mock_tools):
        with pytest.raises(FileNotFoundError):
            mock_tools.get_spot_exposures_by_strike(ticker="NONEXISTENT")
