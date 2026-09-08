"""Unit tests for rh_call()'s 401-detection and auto-refresh retry.

Regression coverage for a real production incident: RH auth silently died
for 19 days because rh_call()'s `"401" in str(exc)` check never matched —
MultiServerMCPClient's internal TaskGroup wraps the real 401 in an
ExceptionGroup whose own str() is the generic "unhandled errors in a
TaskGroup (1 sub-exception)" message. Confirmed live against the actual RH
MCP endpoint with a deliberately invalid token: the nested exception is
httpx.HTTPStatusError with a real exc.response.status_code == 401 — a
structured field, which is what _is_unauthorized() now checks first,
rather than relying on message text. reload_rh_tools() never fired before
this fix.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from trader.rh.mcp_config import _is_unauthorized, rh_call


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build a real httpx.HTTPStatusError with a genuine .response.status_code,
    matching exactly what raise_for_status() produces — and what the live RH
    MCP endpoint's streamable_http transport actually raises on a 401
    (verified directly against the real service, not assumed from source)."""
    request = httpx.Request("POST", "https://agent.robinhood.com/mcp/trading")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status_code}' for url", request=request, response=response
    )


class _FakeTool:
    def __init__(self, responses) -> None:
        # responses: list of return values or Exception instances, consumed in order
        self._responses = list(responses)
        self.calls = 0

    async def ainvoke(self, params: dict):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


class _FakeExceptionGroup(Exception):
    """Duck-types ExceptionGroup's .exceptions attribute without relying on
    the Python 3.11+ builtin, so tests run the same on any interpreter."""

    def __init__(self, message: str, exceptions: tuple[BaseException, ...]) -> None:
        super().__init__(message)
        self.exceptions = exceptions


class TestIsUnauthorized:
    def test_http_status_error_401_matches_on_status_code(self):
        # Primary path: a real httpx.HTTPStatusError, matched on the
        # structured status_code field, not by parsing its message text.
        assert _is_unauthorized(_http_status_error(401))

    def test_http_status_error_non_401_does_not_match(self):
        # A different HTTPStatusError (e.g. a 500) must not be mistaken for
        # an auth failure just because it's the same exception type.
        assert not _is_unauthorized(_http_status_error(500))

    def test_exception_group_with_nested_http_status_error_401_matches(self):
        # The literal production/live-verified shape: streamable_http's
        # raise_for_status() 401 nested inside a TaskGroup ExceptionGroup.
        exc = _FakeExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            (_http_status_error(401),),
        )
        assert _is_unauthorized(exc)

    def test_exception_group_with_nested_http_status_error_500_does_not_match(self):
        exc = _FakeExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            (_http_status_error(500),),
        )
        assert not _is_unauthorized(exc)

    def test_plain_401_string_matches_as_fallback(self):
        # Fallback path: whatever isn't a plain HTTPStatusError (e.g. an
        # MCP protocol-level auth error) but still mentions 401 in its text.
        assert _is_unauthorized(Exception("401 Unauthorized"))

    def test_plain_unauthorized_string_matches_case_insensitive(self):
        assert _is_unauthorized(Exception("Server said UNAUTHORIZED"))

    def test_unrelated_error_does_not_match(self):
        assert not _is_unauthorized(Exception("connection reset by peer"))

    def test_exception_group_with_nested_401_string_matches(self):
        exc = _FakeExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            (ValueError("RH 401: Unauthorized"),),
        )
        assert _is_unauthorized(exc)

    def test_exception_group_with_nested_unrelated_error_does_not_match(self):
        exc = _FakeExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            (ConnectionError("timed out"),),
        )
        assert not _is_unauthorized(exc)

    def test_nested_exception_group_recurses_to_any_depth(self):
        innermost = ValueError("401 Unauthorized")
        middle = _FakeExceptionGroup("wrapper", (innermost,))
        outer = _FakeExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", (middle,))
        assert _is_unauthorized(outer)

    def test_exception_group_with_multiple_subexceptions_any_match(self):
        exc = _FakeExceptionGroup(
            "unhandled errors in a TaskGroup (2 sub-exceptions)",
            (ConnectionError("timed out"), ValueError("401 Unauthorized")),
        )
        assert _is_unauthorized(exc)

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup is 3.11+")
    def test_real_exception_group_builtin(self):
        # Exercises the exact live-verified production failure mode
        # (Python 3.12 in the container, real httpx.HTTPStatusError nested
        # in a real builtin ExceptionGroup) rather than the duck-typed
        # stand-ins above. Referencing the builtin by name is safe even
        # when this file is collected on 3.10 — the name is only resolved
        # when the test body actually runs, and skipif prevents that below
        # 3.11.
        real_group = ExceptionGroup(  # noqa: F821 — 3.11+ builtin, guarded above
            "unhandled errors in a TaskGroup (1 sub-exception)",
            [_http_status_error(401)],
        )
        assert _is_unauthorized(real_group)


class TestRhCallRetry:
    async def test_success_on_first_call_no_retry(self):
        tool = _FakeTool([{"data": "ok"}])
        tools = {"get_accounts": tool}
        result = await rh_call(tools, "get_accounts", {})
        assert result == {"data": "ok"}
        assert tool.calls == 1

    async def test_non_401_error_propagates_without_retry(self):
        tool = _FakeTool([ValueError("boom")])
        tools = {"get_accounts": tool}
        with pytest.raises(ValueError, match="boom"):
            await rh_call(tools, "get_accounts", {})
        assert tool.calls == 1

    async def test_401_triggers_reload_and_retries_once(self):
        tool = _FakeTool([ValueError("401 Unauthorized"), {"data": "recovered"}])
        tools = {"get_accounts": tool}
        with patch("trader.rh.mcp_config.reload_rh_tools", new=AsyncMock()) as mock_reload:
            result = await rh_call(tools, "get_accounts", {})
        assert result == {"data": "recovered"}
        assert tool.calls == 2
        mock_reload.assert_awaited_once()

    async def test_exception_group_401_triggers_reload_and_retries(self):
        # The exact live-verified production failure shape: the raw
        # exception from ainvoke() is a TaskGroup-wrapped ExceptionGroup
        # containing a real httpx.HTTPStatusError(401), not a plain string.
        wrapped = _FakeExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            (_http_status_error(401),),
        )
        tool = _FakeTool([wrapped, {"data": "recovered"}])
        tools = {"get_equity_quotes": tool}
        with patch("trader.rh.mcp_config.reload_rh_tools", new=AsyncMock()) as mock_reload:
            result = await rh_call(tools, "get_equity_quotes", {})
        assert result == {"data": "recovered"}
        assert tool.calls == 2
        mock_reload.assert_awaited_once()

    async def test_exception_group_500_does_not_trigger_reload(self):
        wrapped = _FakeExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            (_http_status_error(500),),
        )
        tool = _FakeTool([wrapped])
        tools = {"get_equity_quotes": tool}
        with patch("trader.rh.mcp_config.reload_rh_tools", new=AsyncMock()) as mock_reload:
            with pytest.raises(_FakeExceptionGroup):
                await rh_call(tools, "get_equity_quotes", {})
        mock_reload.assert_not_awaited()

    async def test_unwraps_mcp_content_envelope(self):
        import json

        envelope = [{"type": "text", "text": json.dumps({"data": "payload"}), "id": "lc_1"}]
        tool = _FakeTool([envelope])
        tools = {"get_accounts": tool}
        result = await rh_call(tools, "get_accounts", {})
        assert result == {"data": "payload"}
