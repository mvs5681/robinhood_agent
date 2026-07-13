"""
Unit tests for the LangGraph agent graph using MockUWTools.
No network I/O — all data sourced from fixtures.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from trader.graph.agent import build_graph, run_pipeline
from trader.graph.state import TradingAgentState
from trader.uw.mock_tools import MockUWTools

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _make_mock_tool(name: str, return_value):
    """Create a LangChain-compatible mock tool."""
    tool = MagicMock()
    tool.name = name
    tool.ainvoke = AsyncMock(return_value=return_value)
    return tool


@pytest.fixture
def mock_tool_list(flow_alerts_raw, market_tide_raw, darkpool_raw, gex_positive_raw):
    return [
        _make_mock_tool("get_flow_alerts", flow_alerts_raw),
        _make_mock_tool("get_market_tide", market_tide_raw),
        _make_mock_tool("get_dark_pool_trades", darkpool_raw),
        _make_mock_tool("get_greek_exposure_by_strike", gex_positive_raw),
        _make_mock_tool("get_flow_per_strike", {"data": []}),
        _make_mock_tool("get_options_chain", {"data": []}),
        _make_mock_tool("get_extended_technical_indicator", {"data": []}),
    ]


class TestBuildGraph:
    def test_graph_compiles(self, mock_tool_list):
        graph = build_graph(mock_tool_list)
        assert graph is not None

    async def test_pipeline_fetches_market_data(self, mock_tool_list):
        state = await run_pipeline(["AAPL"], mock_tool_list)
        assert isinstance(state, TradingAgentState)
        assert len(state.market_tide) == 3
        assert len(state.flow_alerts) == 2

    async def test_pipeline_fetches_ticker_data(self, mock_tool_list):
        state = await run_pipeline(["AAPL"], mock_tool_list)
        assert "AAPL" in state.spot_gex
        assert len(state.spot_gex["AAPL"]) == 7
        assert "AAPL" in state.darkpool
        assert len(state.darkpool["AAPL"]) == 2

    async def test_pipeline_runs_gex_detection(self, mock_tool_list):
        # AAPL spot price comes from flow_alerts fixture underlying_price=195.50
        state = await run_pipeline(["AAPL"], mock_tool_list)
        assert "AAPL" in state.gex_setups
        setup = state.gex_setups["AAPL"]
        from trader.gex.schemas import GEXRegime
        assert setup.regime == GEXRegime.POSITIVE
        assert setup.candidate_direction == "call"

    async def test_pipeline_multiple_tickers(self, mock_tool_list):
        state = await run_pipeline(["AAPL", "SPY"], mock_tool_list)
        assert "AAPL" in state.spot_gex
        assert "SPY" in state.spot_gex

    async def test_pipeline_errors_do_not_crash(self):
        broken_tool = _make_mock_tool("get_market_tide", None)
        broken_tool.ainvoke = AsyncMock(side_effect=RuntimeError("API down"))
        tools = [
            broken_tool,
            _make_mock_tool("get_flow_alerts", {"data": []}),
            _make_mock_tool("get_spot_exposures_by_strike", {"data": []}),
            _make_mock_tool("get_darkpool_ticker", {"data": []}),
            _make_mock_tool("get_net_prem_ticks", {"data": []}),
        ]
        state = await run_pipeline(["AAPL"], tools)
        assert any("market_tide" in e for e in state.errors)
        assert state.flow_alerts == []
