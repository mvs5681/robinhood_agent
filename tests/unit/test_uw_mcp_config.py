"""Unit tests for the UW MCP tool allowlist."""

from __future__ import annotations

from trader.uw.mcp_config import ALLOWED_TOOL_NAMES


def test_interpolated_iv_is_allowed():
    # get_interpolated_iv had a full downstream implementation (schema,
    # validator, TickerSnapshot field, iv_cost_score, state_capture
    # serializer, backtest mock tools) but was never added here, so the MCP
    # client filtered it out before scanner.py ever got a handle to call it —
    # interpolated_iv was permanently empty in both live and captured data.
    assert "get_interpolated_iv" in ALLOWED_TOOL_NAMES
