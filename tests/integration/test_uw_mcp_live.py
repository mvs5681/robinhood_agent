"""
Live integration tests — skipped unless UW_API_TOKEN is set.

Run with:  UW_API_TOKEN=xxx pytest -m live tests/integration/test_uw_mcp_live.py
"""

import os

import pytest

from trader.uw.mcp_config import load_uw_tools
from trader.uw.validators import (
    parse_flow_alerts,
    parse_market_tide,
    parse_spot_gex_by_strike,
)


@pytest.fixture(scope="module", autouse=True)
def require_token():
    if not os.environ.get("UW_API_TOKEN"):
        pytest.skip("UW_API_TOKEN not set — skipping live tests")


@pytest.mark.live
async def test_load_tools_returns_allowed_set():
    tools = await load_uw_tools()
    names = {t.name for t in tools}
    assert "get_flow_alerts" in names
    assert "get_spot_exposures_by_strike" in names
    assert "get_market_tide" in names


@pytest.mark.live
async def test_flow_alerts_live():
    tools = await load_uw_tools()
    tool_map = {t.name: t for t in tools}
    raw = await tool_map["get_flow_alerts"].ainvoke({"limit": 10})
    alerts = parse_flow_alerts(raw)
    assert len(alerts) > 0
    assert all(a.ticker for a in alerts)
    assert all(a.total_premium > 0 for a in alerts)


@pytest.mark.live
async def test_market_tide_live():
    tools = await load_uw_tools()
    tool_map = {t.name: t for t in tools}
    raw = await tool_map["get_market_tide"].ainvoke({})
    ticks = parse_market_tide(raw)
    assert len(ticks) > 0


@pytest.mark.live
async def test_spot_gex_live():
    tools = await load_uw_tools()
    tool_map = {t.name: t for t in tools}
    raw = await tool_map["get_spot_exposures_by_strike"].ainvoke({"ticker": "SPY"})
    strikes = parse_spot_gex_by_strike(raw)
    assert len(strikes) > 0
    # Each strike has a price and at least one non-zero gamma field
    assert all(s.price > 0 for s in strikes)
