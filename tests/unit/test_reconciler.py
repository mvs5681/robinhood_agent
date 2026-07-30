"""Tests for startup position reconciliation.

Regression coverage for a live incident: reconcile_positions logged
"no open positions found" while 3 positions (CRWV, IWM, SMCI) were
genuinely open in the account. _parse_positions itself proved correct
for the exact live response shape when tested in isolation — these
tests lock that in, and the added raw-response log line (fired only
when 0 items parse) makes a future recurrence diagnosable from
container logs alone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from trader.live import reconciler
from trader.live.position_store import PositionStore

ACCOUNT = "869536151"


def _rh_position(
    ticker: str = "CRWV",
    quantity: str = "1.0000",
    average_price: str = "464.0000",
    option_type: str = "call",
) -> dict:
    """A realistic get_option_positions item — same field names/shape
    confirmed live moments before this test was written."""
    return {
        "option_id": f"option-id-{ticker.lower()}",
        "chain_symbol": ticker,
        "type": "long",
        "quantity": quantity,
        "average_price": average_price,
        "expiration_date": "2026-08-07",
        "strike_price": "455.0000",
        "option_type": option_type,
        "opened_at": "2026-07-16T18:56:03.080787Z",
    }


def _tool(response) -> MagicMock:
    t = MagicMock()
    t.ainvoke = AsyncMock(return_value=response)
    return t


class TestParsePositionsShapes:
    def test_live_shape_data_positions(self):
        # Exact shape confirmed live: {"data": {"positions": [...]}}
        raw = {"data": {"positions": [_rh_position("CRWV"), _rh_position("IWM")]}}
        items = reconciler._parse_positions(raw)
        assert len(items) == 2
        assert {i["chain_symbol"] for i in items} == {"CRWV", "IWM"}

    def test_data_results_shape(self):
        raw = {"data": {"results": [_rh_position("SMCI")]}}
        items = reconciler._parse_positions(raw)
        assert len(items) == 1
        assert items[0]["chain_symbol"] == "SMCI"

    def test_top_level_results_shape(self):
        raw = {"results": [_rh_position("IWM")]}
        items = reconciler._parse_positions(raw)
        assert len(items) == 1

    def test_raw_list_shape(self):
        raw = [_rh_position("CRWV")]
        items = reconciler._parse_positions(raw)
        assert len(items) == 1

    def test_mcp_text_envelope_unwrapped(self):
        import json
        inner = {"data": {"positions": [_rh_position("CRWV")]}}
        raw = [{"type": "text", "text": json.dumps(inner)}]
        items = reconciler._parse_positions(raw)
        assert len(items) == 1
        assert items[0]["chain_symbol"] == "CRWV"

    def test_genuinely_empty_returns_empty(self):
        assert reconciler._parse_positions({"data": {"positions": []}}) == []
        assert reconciler._parse_positions({}) == []


class TestReconcilePositionsEndToEnd:
    async def test_recovers_all_three_live_style_positions(self):
        store = PositionStore()
        raw = {"data": {"positions": [
            _rh_position("CRWV", quantity="1.0000", average_price="464.0000"),
            _rh_position("IWM", quantity="1.0000", average_price="320.0000"),
            _rh_position("SMCI", quantity="2.0000", average_price="252.0000"),
        ]}}
        rh_tools = {"get_option_positions": _tool(raw)}
        recovered = await reconciler.reconcile_positions(rh_tools, store, ACCOUNT)

        assert recovered == 3
        positions = await store.all()
        tickers = {p.ticker for p in positions}
        assert tickers == {"CRWV", "IWM", "SMCI"}
        crwv = next(p for p in positions if p.ticker == "CRWV")
        assert crwv.entry_premium == Decimal("4.6400")  # 464.00 / 100 per share
        assert crwv.quantity == 1
        assert crwv.target_level is None  # profit target disabled for reconciled positions

    async def test_zero_positions_logs_raw_response_for_diagnosis(self, caplog, monkeypatch):
        monkeypatch.setattr(reconciler.asyncio, "sleep", AsyncMock())
        store = PositionStore()
        rh_tools = {"get_option_positions": _tool({"data": {"positions": []}})}
        import logging
        with caplog.at_level(logging.INFO, logger="trader.live.reconciler"):
            recovered = await reconciler.reconcile_positions(rh_tools, store, ACCOUNT)
        assert recovered == 0
        assert any("0 items parsed" in r.message for r in caplog.records)

    async def test_empty_first_attempt_retried_and_recovers_on_second(self, monkeypatch):
        # Reproduces the live incident: a transient empty result followed by
        # a real, non-empty one on retry must still fully reconcile.
        monkeypatch.setattr(reconciler.asyncio, "sleep", AsyncMock())
        store = PositionStore()
        good = {"data": {"positions": [_rh_position("CRWV")]}}
        empty = {"data": {"positions": []}}
        tool = MagicMock()
        tool.ainvoke = AsyncMock(side_effect=[empty, good])
        recovered = await reconciler.reconcile_positions(
            {"get_option_positions": tool}, store, ACCOUNT
        )
        assert recovered == 1
        assert tool.ainvoke.await_count == 2

    async def test_genuinely_empty_account_only_retries_once(self, monkeypatch):
        monkeypatch.setattr(reconciler.asyncio, "sleep", AsyncMock())
        store = PositionStore()
        empty = {"data": {"positions": []}}
        tool = MagicMock()
        tool.ainvoke = AsyncMock(return_value=empty)
        recovered = await reconciler.reconcile_positions(
            {"get_option_positions": tool}, store, ACCOUNT
        )
        assert recovered == 0
        assert tool.ainvoke.await_count == 2  # first attempt + one retry, then stop

    async def test_api_failure_returns_zero_without_raising(self):
        store = PositionStore()
        failing = MagicMock()
        failing.ainvoke = AsyncMock(side_effect=RuntimeError("RH down"))
        recovered = await reconciler.reconcile_positions(
            {"get_option_positions": failing}, store, ACCOUNT
        )
        assert recovered == 0
        assert await store.all() == []
