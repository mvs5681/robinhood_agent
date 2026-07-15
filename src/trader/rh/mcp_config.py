"""Robinhood MCP client configuration.

Connects to https://agent.robinhood.com/mcp/trading using a Bearer token
obtained via the OAuth 2.0 authorization-code + PKCE flow (see scripts/auth_robinhood.py).

Token lifecycle:
- At startup, always exchange RH_REFRESH_TOKEN for a fresh access token.
  RH_ACCESS_TOKEN in .env is not trusted — it may be stale.
- After each refresh, tokens are written to TOKEN_FILE (default
  /app/logs/rh_tokens.json on the mounted volume) so the next container
  restart uses up-to-date credentials.
- Mid-session 401s trigger reload_rh_tools(), which refreshes, saves, and
  reloads the tool dict in-place so both Executor and ExitLoop get fresh tools
  without any additional wiring.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

import httpx
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

RH_MCP_URL = "https://agent.robinhood.com/mcp/trading"
RH_TOKEN_URL = "https://api.robinhood.com/oauth2/token/"

TOKEN_FILE = Path(os.environ.get("TOKEN_FILE", "/app/logs/rh_tokens.json"))

ALLOWED_RH_TOOL_NAMES: frozenset[str] = frozenset([
    "get_option_instruments",
    "review_option_order",
    "place_option_order",
    "cancel_option_order",
    "get_option_orders",
    "get_accounts",
    "get_option_positions",
    "get_portfolio",
    "get_equity_quotes",
    "get_option_quotes",
])


# ---------------------------------------------------------------------------
# Token file helpers
# ---------------------------------------------------------------------------

def load_token_file() -> dict:
    """Return stored tokens from TOKEN_FILE, or {} if the file doesn't exist."""
    try:
        return json.loads(TOKEN_FILE.read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Could not read token file %s: %s", TOKEN_FILE, exc)
        return {}


def save_token_file(access_token: str, refresh_token: str, client_id: str) -> None:
    """Atomically write tokens to TOKEN_FILE (write temp + rename)."""
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=TOKEN_FILE.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            Path(tmp_path).replace(TOKEN_FILE)
        except Exception:
            os.unlink(tmp_path)
            raise
        logger.debug("RH tokens saved to %s", TOKEN_FILE)
    except Exception as exc:
        logger.warning("Could not save token file %s: %s", TOKEN_FILE, exc)


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

async def refresh_rh_token() -> str:
    """
    Exchange RH_REFRESH_TOKEN for a fresh access token.

    Resolution order for refresh_token and client_id:
      1. TOKEN_FILE (written by previous refresh — most up-to-date)
      2. Environment variables (initial bootstrap)

    After a successful refresh, saves new tokens to TOKEN_FILE and updates
    os.environ so the current process uses them immediately.

    Returns the new access token.
    """
    stored = load_token_file()

    refresh_token = (
        stored.get("refresh_token")
        or os.environ.get("RH_REFRESH_TOKEN", "")
    )
    client_id = (
        stored.get("client_id")
        or os.environ.get("RH_CLIENT_ID", "")
    )

    if not refresh_token:
        raise RuntimeError(
            "RH_REFRESH_TOKEN is not set — run scripts/auth_robinhood.py first"
        )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            RH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

    new_access = data["access_token"]
    new_refresh = data.get("refresh_token", refresh_token)

    os.environ["RH_ACCESS_TOKEN"] = new_access
    os.environ["RH_REFRESH_TOKEN"] = new_refresh

    save_token_file(new_access, new_refresh, client_id)
    logger.info("RH token refreshed and saved to %s", TOKEN_FILE)
    return new_access


# ---------------------------------------------------------------------------
# MCP client + tool loading
# ---------------------------------------------------------------------------

async def load_rh_tools(token: str | None = None) -> list[BaseTool]:
    """Connect to the RH MCP server and return the allowed tools."""
    bearer = token or os.environ.get("RH_ACCESS_TOKEN", "")
    if not bearer:
        raise RuntimeError("RH_ACCESS_TOKEN is not set — call refresh_rh_token() first")
    client = MultiServerMCPClient(
        {
            "robinhood": {
                "url": RH_MCP_URL,
                "transport": "streamable_http",
                "headers": {"Authorization": f"Bearer {bearer}"},
            }
        }
    )
    all_tools: list[BaseTool] = await client.get_tools()
    tools = [t for t in all_tools if t.name in ALLOWED_RH_TOOL_NAMES]
    missing = ALLOWED_RH_TOOL_NAMES - {t.name for t in tools}
    if missing:
        logger.warning("RH MCP did not expose expected tools: %s", missing)
    return tools


async def reload_rh_tools(rh_tools: dict[str, BaseTool]) -> None:
    """
    Refresh the RH OAuth token, reload tools, and update rh_tools in-place.

    Both Executor and ExitLoop hold a reference to the same dict object, so
    updating it in-place propagates to both without any additional wiring.
    Called automatically by _rh_call() when a 401 is detected mid-session.
    """
    await refresh_rh_token()
    new_tools = await load_rh_tools()
    rh_tools.clear()
    rh_tools.update({t.name: t for t in new_tools})
    logger.info("RH tools reloaded (%d tools)", len(rh_tools))


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def unwrap_mcp(result: object) -> object:
    """Unwrap the langchain MCP content envelope to the tool's JSON payload.

    MCP tools return [{'type': 'text', 'text': '<json>', 'id': 'lc_...'}].
    Callers that index into the raw envelope get langchain block metadata —
    e.g. treating the envelope as an instruments list once sent the block's
    'lc_...' id to Robinhood as an option_id. Non-envelope values pass through.
    """
    if (
        isinstance(result, list) and result
        and isinstance(result[0], dict) and "text" in result[0]
    ):
        try:
            return json.loads(result[0]["text"])
        except (ValueError, TypeError):
            pass
    return result


async def rh_call(rh_tools: dict[str, BaseTool], name: str, params: dict):
    """
    Call an RH MCP tool by name, retrying once after a token refresh on 401.
    Returns the unwrapped JSON payload, not the MCP content envelope.
    Use this instead of rh_tools[name].ainvoke(params) directly.
    """
    try:
        return unwrap_mcp(await rh_tools[name].ainvoke(params))
    except Exception as exc:
        if "401" in str(exc) or "unauthorized" in str(exc).lower():
            logger.warning("RH 401 on %s — refreshing token and retrying", name)
            await reload_rh_tools(rh_tools)
            return unwrap_mcp(await rh_tools[name].ainvoke(params))
        raise
