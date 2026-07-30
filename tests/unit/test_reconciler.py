"""Tests for startup position reconciliation.

Regression coverage for a live incident, in two parts:

1. reconcile_positions logged "no open positions found" while the API
   response was genuinely empty on a transient basis — the empty-result
   retry (with raw-response logging on a repeat empty) covers this.

2. A deeper, separate bug found while investigating #1: even a fully
   correct, non-empty get_option_positions response was silently turning
   into 0 recovered positions. get_option_positions has no strike_price
   field, and its own "type" field means "long"/"short" (position
   direction), not "call"/"put" — _to_position was reading "type" as if
   it were the option's call/put type and rejecting every real position
   outright. The fix batch-fetches each position's real strike/type via
   get_option_instruments (using option_id) before conversion. These
   tests use the exact field shapes confirmed live for both endpoints.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from trader.live import reconciler
from trader.live.position_store import PositionStore

ACCOUNT = "869536151"


def _rh_position(
    ticker: str = "CRWV",
    option_id: str | None = None,
    quantity: str = "1.0000",
    average_price: str = "464.0000",
    expiration_date: str = "2026-08-07",
) -> dict:
    """A get_option_positions item — exact field set confirmed live.

    Deliberately has no strike_price, and "type" here means long/short,
    not call/put — matching the real API, not what _to_position used to
    assume.
    """
    return {
        "option_id": option_id or f"option-id-{ticker.lower()}",
        "chain_id": f"chain-id-{ticker.lower()}",
        "chain_symbol": ticker,
        "type": "long",
        "quantity": quantity,
        "average_price": average_price,
        "expiration_date": expiration_date,
        "trade_value_multiplier": "100.0000",
        "opened_at": "2026-07-16T18:56:03.080787Z",
    }


def _rh_instrument(
    option_id: str,
    strike_price: str = "455.0000",
    option_type: str = "call",
) -> dict:
    """A get_option_instruments item — exact field set confirmed live."""
    return {
        "id": option_id,
        "chain_id": "chain-id",
        "chain_symbol": "CRWV",
        "underlying_type": "equity",
        "expiration_date": "2026-08-07",
        "strike_price": strike_price,
        "type": option_type,
        "state": "active",
        "tradability": "tradable",
    }


def _tool(response) -> MagicMock:
    t = MagicMock()
    t.ainvoke = AsyncMock(return_value=response)
    return t


def _rh_tools(positions_response, instruments_response=None) -> dict:
    return {
        "get_option_positions": _tool(positions_response),
        "get_option_instruments": _tool(instruments_response or {"data": {"instruments": []}}),
    }


class TestParsePositionsShapes:
    def test_live_shape_data_positions(self):
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
        inner = {"data": {"positions": [_rh_position("CRWV")]}}
        raw = [{"type": "text", "text": json.dumps(inner)}]
        items = reconciler._parse_positions(raw)
        assert len(items) == 1
        assert items[0]["chain_symbol"] == "CRWV"

    def test_genuinely_empty_returns_empty(self):
        assert reconciler._parse_positions({"data": {"positions": []}}) == []
        assert reconciler._parse_positions({}) == []


class TestToPositionRequiresInstrumentEnrichment:
    """Locks in the actual live bug: get_option_positions alone can never
    produce a Position — instrument enrichment is not optional."""

    def test_returns_none_without_instrument(self):
        # This is exactly what silently dropped every real position live:
        # no strike_price on the position record, and "type" is long/short.
        assert reconciler._to_position(_rh_position("CRWV")) is None

    def test_position_type_field_is_never_read_as_call_put(self):
        item = _rh_position("CRWV")
        assert item["type"] == "long"  # sanity: confirms the field the old code misread
        assert reconciler._to_position(item) is None

    def test_succeeds_with_matching_instrument(self):
        item = _rh_position("CRWV", option_id="opt-1")
        instrument = _rh_instrument("opt-1", strike_price="455.0000", option_type="call")
        pos = reconciler._to_position(item, instrument)
        assert pos is not None
        assert pos.contract.type == "call"
        assert pos.contract.strike == Decimal("455.0000")
        assert pos.entry_premium == Decimal("4.6400")  # 464.00 / 100

    def test_rejects_instrument_with_non_call_put_type(self):
        item = _rh_position("CRWV", option_id="opt-1")
        instrument = _rh_instrument("opt-1", option_type="unknown")
        assert reconciler._to_position(item, instrument) is None


class TestFetchInstrumentDetails:
    async def test_empty_ids_short_circuits_without_a_call(self):
        tool = _tool({"data": {"instruments": []}})
        result = await reconciler._fetch_instrument_details({"get_option_instruments": tool}, [])
        assert result == {}
        tool.ainvoke.assert_not_called()

    async def test_batches_and_dedups_ids(self):
        tool = _tool({"data": {"instruments": [
            _rh_instrument("opt-1"), _rh_instrument("opt-2"),
        ]}})
        result = await reconciler._fetch_instrument_details(
            {"get_option_instruments": tool}, ["opt-1", "opt-2", "opt-1", None]
        )
        assert set(result) == {"opt-1", "opt-2"}
        call_params = tool.ainvoke.call_args[0][0]
        assert call_params["ids"] == "opt-1,opt-2"

    async def test_failure_degrades_to_empty(self):
        tool = MagicMock()
        tool.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
        result = await reconciler._fetch_instrument_details(
            {"get_option_instruments": tool}, ["opt-1"]
        )
        assert result == {}


class TestReconcilePositionsEndToEnd:
    async def test_recovers_all_three_live_style_positions(self):
        store = PositionStore()
        positions_raw = {"data": {"positions": [
            _rh_position("CRWV", option_id="opt-crwv", average_price="464.0000"),
            _rh_position("IWM", option_id="opt-iwm", quantity="1.0000", average_price="320.0000"),
            _rh_position("SMCI", option_id="opt-smci", quantity="2.0000", average_price="252.0000"),
        ]}}
        instruments_raw = {"data": {"instruments": [
            _rh_instrument("opt-crwv", strike_price="455.0000", option_type="call"),
            _rh_instrument("opt-iwm", strike_price="320.0000", option_type="call"),
            _rh_instrument("opt-smci", strike_price="250.0000", option_type="put"),
        ]}}
        rh_tools = _rh_tools(positions_raw, instruments_raw)
        recovered = await reconciler.reconcile_positions(rh_tools, store, ACCOUNT)

        assert recovered == 3
        positions = await store.all()
        tickers = {p.ticker for p in positions}
        assert tickers == {"CRWV", "IWM", "SMCI"}
        crwv = next(p for p in positions if p.ticker == "CRWV")
        assert crwv.entry_premium == Decimal("4.6400")  # 464.00 / 100 per share
        assert crwv.contract.type == "call"
        assert crwv.contract.strike == Decimal("455.0000")
        assert crwv.quantity == 1
        assert crwv.target_level is None  # profit target disabled for reconciled positions

    async def test_missing_instrument_for_one_position_skips_only_that_one(self):
        store = PositionStore()
        positions_raw = {"data": {"positions": [
            _rh_position("CRWV", option_id="opt-crwv"),
            _rh_position("IWM", option_id="opt-iwm"),
        ]}}
        # Only CRWV's instrument is returned — IWM's lookup came back empty
        instruments_raw = {"data": {"instruments": [
            _rh_instrument("opt-crwv", option_type="call"),
        ]}}
        rh_tools = _rh_tools(positions_raw, instruments_raw)
        recovered = await reconciler.reconcile_positions(rh_tools, store, ACCOUNT)

        assert recovered == 1
        tickers = {p.ticker for p in await store.all()}
        assert tickers == {"CRWV"}

    async def test_zero_positions_logs_raw_response_for_diagnosis(self, caplog, monkeypatch):
        monkeypatch.setattr(reconciler.asyncio, "sleep", AsyncMock())
        store = PositionStore()
        rh_tools = _rh_tools({"data": {"positions": []}})
        with caplog.at_level(logging.INFO, logger="trader.live.reconciler"):
            recovered = await reconciler.reconcile_positions(rh_tools, store, ACCOUNT)
        assert recovered == 0
        assert any("0 items parsed" in r.message for r in caplog.records)

    async def test_empty_first_attempt_retried_and_recovers_on_second(self, monkeypatch):
        # Reproduces the live incident: a transient empty result followed by
        # a real, non-empty one on retry must still fully reconcile.
        monkeypatch.setattr(reconciler.asyncio, "sleep", AsyncMock())
        store = PositionStore()
        good = {"data": {"positions": [_rh_position("CRWV", option_id="opt-crwv")]}}
        empty = {"data": {"positions": []}}
        tool = MagicMock()
        tool.ainvoke = AsyncMock(side_effect=[empty, good])
        instruments_tool = _tool({"data": {"instruments": [
            _rh_instrument("opt-crwv", option_type="call"),
        ]}})
        recovered = await reconciler.reconcile_positions(
            {"get_option_positions": tool, "get_option_instruments": instruments_tool},
            store, ACCOUNT,
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
            {"get_option_positions": tool, "get_option_instruments": _tool({})}, store, ACCOUNT
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
