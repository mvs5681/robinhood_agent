"""Unit tests for rh_call()'s 401-detection and auto-refresh retry.

Regression coverage for a real production incident: RH auth silently died
for 19 days because rh_call()'s `"401" in str(exc)` check never matched —
MultiServerMCPClient's internal TaskGroup wraps the real 401 in an
ExceptionGroup whose own str() is the generic "unhandled errors in a
TaskGroup (1 sub-exception)" message, with the actual 401 text nested one
level down in exc.exceptions. reload_rh_tools() never fired as a result.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest

from trader.rh.mcp_config import _is_unauthorized, rh_call


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
    def test_plain_401_string_matches(self):
        assert _is_unauthorized(Exception("401 Unauthorized"))

    def test_plain_unauthorized_string_matches_case_insensitive(self):
        assert _is_unauthorized(Exception("Server said UNAUTHORIZED"))

    def test_unrelated_error_does_not_match(self):
        assert not _is_unauthorized(Exception("connection reset by peer"))

    def test_exception_group_with_nested_401_matches(self):
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
        # Exercises the literal production failure mode (Python 3.12 in the
        # container) rather than the duck-typed stand-in above. Referencing
        # the builtin by name is safe even when this file is collected on
        # 3.10 — the name is only resolved when the test body actually
        # runs, and skipif prevents that below 3.11.
        real_group = ExceptionGroup(  # noqa: F821 — 3.11+ builtin, guarded above
            "unhandled errors in a TaskGroup (1 sub-exception)",
            [ValueError("401 Unauthorized")],
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
        # The actual production failure mode: the raw exception from
        # ainvoke() is a TaskGroup-wrapped ExceptionGroup, not a plain 401.
        wrapped = _FakeExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            (ValueError("401 Unauthorized"),),
        )
        tool = _FakeTool([wrapped, {"data": "recovered"}])
        tools = {"get_equity_quotes": tool}
        with patch("trader.rh.mcp_config.reload_rh_tools", new=AsyncMock()) as mock_reload:
            result = await rh_call(tools, "get_equity_quotes", {})
        assert result == {"data": "recovered"}
        assert tool.calls == 2
        mock_reload.assert_awaited_once()

    async def test_unwraps_mcp_content_envelope(self):
        import json

        envelope = [{"type": "text", "text": json.dumps({"data": "payload"}), "id": "lc_1"}]
        tool = _FakeTool([envelope])
        tools = {"get_accounts": tool}
        result = await rh_call(tools, "get_accounts", {})
        assert result == {"data": "payload"}
