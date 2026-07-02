"""Robinhood MCP client configuration.

Connects to https://agent.robinhood.com/mcp/trading using a Bearer token
obtained via the OAuth 2.0 authorization-code + PKCE flow (see scripts/auth_robinhood.py).

Token refresh is handled automatically: if RH_REFRESH_TOKEN is set and the
access token is expired, a new access token is fetched silently before connecting.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

RH_MCP_URL = "https://agent.robinhood.com/mcp/trading"
RH_TOKEN_URL = "https://api.robinhood.com/oauth2/token/"

ALLOWED_RH_TOOL_NAMES: frozenset[str] = frozenset([
    "get_option_instruments",
    "review_option_order",
    "place_option_order",
    "get_option_orders",
    "get_accounts",
    "get_option_positions",
    "get_portfolio",
])


def _rh_access_token() -> str:
    token = os.environ.get("RH_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("RH_ACCESS_TOKEN env var is not set — run scripts/auth_robinhood.py")
    return token


async def refresh_rh_token() -> str:
    """
    Exchange RH_REFRESH_TOKEN for a new access token and update the env var.
    Returns the new access token. Raises if RH_REFRESH_TOKEN is not set.
    """
    refresh_token = os.environ.get("RH_REFRESH_TOKEN", "")
    client_id = os.environ.get("RH_CLIENT_ID", "")
    if not refresh_token:
        raise RuntimeError("RH_REFRESH_TOKEN is not set — re-run scripts/auth_robinhood.py")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            RH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()

    new_token = data["access_token"]
    os.environ["RH_ACCESS_TOKEN"] = new_token
    if "refresh_token" in data:
        os.environ["RH_REFRESH_TOKEN"] = data["refresh_token"]

    logger.info("RH token refreshed successfully")
    return new_token


@asynccontextmanager
async def rh_mcp_client(token: str | None = None) -> AsyncIterator[MultiServerMCPClient]:
    """Context manager that yields an authenticated RH MCP client."""
    bearer = token or _rh_access_token()
    async with MultiServerMCPClient(
        {
            "robinhood": {
                "url": RH_MCP_URL,
                "transport": "streamable_http",
                "headers": {"Authorization": f"Bearer {bearer}"},
            }
        }
    ) as client:
        yield client


async def load_rh_tools(token: str | None = None) -> list[BaseTool]:
    """
    Connect to the RH MCP server and return the allowed tools.
    If the connection fails with a 401, attempts one token refresh and retries.
    """
    async def _load(t: str) -> list[BaseTool]:
        async with rh_mcp_client(token=t) as client:
            all_tools: list[BaseTool] = client.get_tools()
        return [tool for tool in all_tools if tool.name in ALLOWED_RH_TOOL_NAMES]

    bearer = token or _rh_access_token()
    try:
        tools = await _load(bearer)
    except Exception as exc:
        if "401" in str(exc) or "unauthorized" in str(exc).lower():
            logger.warning("RH token expired, attempting refresh…")
            bearer = await refresh_rh_token()
            tools = await _load(bearer)
        else:
            raise

    missing = ALLOWED_RH_TOOL_NAMES - {t.name for t in tools}
    if missing:
        logger.warning("RH MCP did not expose expected tools: %s", missing)

    return tools
