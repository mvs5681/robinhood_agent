"""
Configures the Unusual Whales MCP server connection and exposes UW tools
as LangChain-compatible tools for use in the LangGraph agent.

Transport: streamable_http (UW MCP endpoint at /public-api/mcp)
Auth:      Bearer token in Authorization header + UW-CLIENT-API-ID header
"""

from __future__ import annotations

import os

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

UW_MCP_URL = "https://mcp.unusualwhales.com"

# Subset of MCP tool names we actually use — keeps the ToolNode small and auditable.
ALLOWED_TOOL_NAMES: frozenset[str] = frozenset(
    [
        # Flow
        "get_flow_alerts",
        "get_stock_flow_alerts",
        # GEX
        "get_spot_exposures_by_strike",
        "get_greek_exposure_by_strike",
        # Market-wide
        "get_market_tide",
        # Ticker data
        "get_darkpool_ticker",
        "get_net_prem_ticks",
        "get_option_contracts",
        "get_greeks",
        "get_interpolated_iv",
        "get_technical_indicator",
        # Screener
        "get_option_contracts_screener",
    ]
)


def _uw_headers() -> dict[str, str]:
    token = os.environ.get("UW_API_TOKEN", "")
    client_id = os.environ.get("UW_CLIENT_API_ID", "100001")
    if not token:
        raise RuntimeError("UW_API_TOKEN env var is not set")
    return {
        "Authorization": f"Bearer {token}",
        "UW-CLIENT-API-ID": client_id,
    }


async def load_uw_tools() -> list[BaseTool]:
    """
    Connect to the UW MCP server and return only the allowed tools as
    LangChain BaseTool instances suitable for binding to a LangGraph ToolNode.
    """
    client = MultiServerMCPClient(
        {
            "unusualwhales": {
                "url": UW_MCP_URL,
                "transport": "streamable_http",
                "headers": _uw_headers(),
            }
        }
    )
    all_tools: list[BaseTool] = await client.get_tools()

    allowed = [t for t in all_tools if t.name in ALLOWED_TOOL_NAMES]

    missing = ALLOWED_TOOL_NAMES - {t.name for t in allowed}
    if missing:
        # Non-fatal: warn so caller can decide whether to proceed.
        import warnings
        warnings.warn(
            f"UW MCP server did not expose expected tools: {missing}",
            stacklevel=2,
        )

    return allowed


def tools_by_name(tools: list[BaseTool]) -> dict[str, BaseTool]:
    return {t.name: t for t in tools}
