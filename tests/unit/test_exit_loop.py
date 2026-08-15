"""Tests for ExitLoop's thesis-invalidation wiring (GEXCache → ExitMonitor).

Price/DTE exit behavior itself is covered by test_exit_monitor.py; this file
covers the plumbing that makes the live GEX setup reach the monitor.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from trader.exits.monitor import ExitMonitor
from trader.exits.schemas import ExitContext, ExitReason, Position
from trader.executor.schemas import ExecutionMode
from trader.gex.schemas import GEXRegime, GEXSetup
from trader.live.cache import GEXCache, TickerSnapshot
from trader.live.exit_loop import ExitLoop
from trader.live.position_store import PositionStore
from trader.uw.schemas import OptionContract

ACCOUNT = "869536151"

# Always well beyond dte_floor (default 7) relative to whenever tests run —
# a fixed calendar date here previously went stale and started spuriously
# firing dte_stop once "today" caught up to it.
_FAR_EXPIRY = date.today() + timedelta(days=45)


def _contract() -> OptionContract:
    return OptionContract(
        ticker="AAPL", expiry=_FAR_EXPIRY, strike=Decimal("200"),
        type="call", bid=Decimal("2.90"), ask=Decimal("3.10"),
        open_interest=9000, volume=4500,
    )


def _position() -> Position:
    return Position(
        position_id="pos-001", ticker="AAPL", contract=_contract(),
        entry_premium=Decimal("3.00"), target_level=Decimal("250"),  # far from spot
        opened_at=datetime(2026, 7, 1, tzinfo=timezone.utc), quantity=1,
        option_instrument_id="opt-1",
    )


def _setup(direction: str = "call", regime: GEXRegime = GEXRegime.NEGATIVE, stale: bool = False):
    return GEXSetup(
        ticker="AAPL", as_of=datetime.now(timezone.utc), spot_price=Decimal("195"),
        regime=regime, flip_point=None, nearest_call_wall=None, nearest_put_wall=None,
        target_level=Decimal("250"), candidate_direction=direction,
        setup_type="momentum" if direction != "none" else "none",
        structure_confidence=0.6, raw_gex_by_strike=[],
    )


async def _cache_with(ticker: str, setup, *, stale: bool = False) -> GEXCache:
    cache = GEXCache()
    snap = TickerSnapshot(gex_setup=setup)
    if not stale:
        snap.refreshed_at = datetime.now(timezone.utc)
    await cache.update([], {ticker: snap})
    return cache


def _quote_tool(mark: str = "3.00") -> MagicMock:
    t = MagicMock()
    t.ainvoke = AsyncMock(return_value={"data": {"results": [
        {"quote": {"mark_price": mark}}
    ]}})
    return t


def _loop(cache: GEXCache | None, rh_tools: dict | None = None,
          store: PositionStore | None = None) -> ExitLoop:
    return ExitLoop(
        rh_tools=rh_tools if rh_tools is not None else {"get_option_quotes": _quote_tool()},
        position_store=store or PositionStore(),
        account_number=ACCOUNT,
        execution_mode=ExecutionMode.PROPOSE_ONLY,
        monitor=ExitMonitor(stop_loss_pct=0.35, dte_floor=7),
        cache=cache,
    )


class TestCurrentGexSetup:
    async def test_returns_none_when_no_cache_wired(self):
        loop = _loop(cache=None)
        assert await loop._current_gex_setup("AAPL") is None

    async def test_returns_none_when_ticker_not_cached(self):
        cache = GEXCache()
        loop = _loop(cache=cache)
        assert await loop._current_gex_setup("AAPL") is None

    async def test_returns_live_setup_when_fresh(self):
        cache = await _cache_with("AAPL", _setup(direction="put"))
        loop = _loop(cache=cache)
        setup = await loop._current_gex_setup("AAPL")
        assert setup is not None
        assert setup.candidate_direction == "put"

    async def test_returns_none_when_stale(self):
        cache = await _cache_with("AAPL", _setup(direction="put"), stale=True)
        loop = _loop(cache=cache)
        assert await loop._current_gex_setup("AAPL") is None


class TestCurrentExitContext:
    async def test_returns_none_when_no_cache_wired(self):
        loop = _loop(cache=None)
        assert await loop._current_exit_context("AAPL") is None

    async def test_returns_none_when_ticker_not_cached(self):
        cache = GEXCache()
        loop = _loop(cache=cache)
        assert await loop._current_exit_context("AAPL") is None

    async def test_returns_none_when_stale(self):
        cache = await _cache_with("AAPL", _setup(), stale=True)
        loop = _loop(cache=cache)
        assert await loop._current_exit_context("AAPL") is None

    async def test_returns_context_carrying_cached_signals_when_fresh(self):
        from trader.uw.schemas import InterpolatedIVEntry, SpotGEXByStrike, TechnicalPoint

        cache = GEXCache()
        snap = TickerSnapshot(
            gex_setup=_setup(),
            spot_gex=[SpotGEXByStrike(price=Decimal("200"), call_gamma_oi=Decimal("100"),
                                       put_gamma_oi=Decimal("-50"))],
            interpolated_iv=[InterpolatedIVEntry(days=7, volatility=Decimal("0.3"),
                                                  percentile=Decimal("62"))],
            technicals={"RSI": [TechnicalPoint(timestamp="2026-08-14", value=Decimal("55"))]},
        )
        snap.refreshed_at = datetime.now(timezone.utc)
        await cache.update([], {"AAPL": snap})
        loop = _loop(cache=cache)

        context = await loop._current_exit_context("AAPL")

        assert context is not None
        assert len(context.spot_gex) == 1
        assert context.iv_percentile_at(7) == Decimal("62")
        assert context.rsi_latest() == Decimal("55")


def _sequential_quote_tool(marks: list[str]) -> MagicMock:
    t = MagicMock()
    t.ainvoke = AsyncMock(side_effect=[
        {"data": {"results": [{"quote": {"mark_price": m}}]}} for m in marks
    ])
    return t


def _equity_quote_tool(price: str = "195") -> MagicMock:
    t = MagicMock()
    t.ainvoke = AsyncMock(return_value={"data": {"results": [
        {"quote": {"symbol": "AAPL", "last_trade_price": price}}
    ]}})
    return t


class TestPeakPremiumTracking:
    async def test_peak_persists_and_only_moves_upward_across_ticks(self):
        store = PositionStore()
        pos = _position()  # entry 3.00, target 250 (far from spot — no profit exit)
        await store.add(pos)

        # Dip to 3.80 stays above this peak's giveback floor (3.50) so it
        # doesn't itself trigger a trailing-stop exit — isolates peak
        # persistence from the exit condition (covered separately below).
        rh_tools = {
            "get_option_quotes": _sequential_quote_tool(["4.00", "3.80", "5.00"]),
            "get_equity_quotes": _equity_quote_tool(),
        }
        loop = _loop(cache=None, rh_tools=rh_tools, store=store)

        await loop._tick()
        assert (await store.get(pos.position_id)).peak_premium == Decimal("4.00")

        await loop._tick()  # dip — peak must not move down
        assert (await store.get(pos.position_id)).peak_premium == Decimal("4.00")

        await loop._tick()  # new high
        assert (await store.get(pos.position_id)).peak_premium == Decimal("5.00")

    async def test_trailing_stop_exits_after_giveback_from_peak(self):
        store = PositionStore()
        pos = _position()  # entry 3.00
        await store.add(pos)

        # default monitor: activation 0.30 → threshold 3.90; giveback 0.50
        # peak 6.00 → gain_at_peak 3.00 → floor = 3.00 + 3.00*0.50 = 4.50
        rh_tools = {
            "get_option_quotes": _sequential_quote_tool(["6.00", "4.40"]),
            "get_equity_quotes": _equity_quote_tool(),
        }
        loop = _loop(cache=None, rh_tools=rh_tools, store=store)

        await loop._tick()  # establishes the peak, no exit yet
        assert len(await store.all()) == 1

        await loop._tick()  # gives back below the floor — trailing stop fires
        assert await store.all() == []


class TestEvaluateWithThesisInvalidation:
    async def test_exit_triggered_when_live_direction_flips(self):
        store = PositionStore()
        pos = _position()
        await store.add(pos)
        cache = await _cache_with("AAPL", _setup(direction="put"))
        loop = _loop(cache=cache, store=store)

        # PROPOSE_ONLY dry-run — no RH calls needed, just confirm the signal fires
        await loop._evaluate(pos, {"AAPL": Decimal("195")})

        # dry-run exit still removes the position from the store
        assert await store.all() == []

    async def test_no_exit_when_cache_absent_and_price_dte_clear(self):
        store = PositionStore()
        pos = _position()
        await store.add(pos)
        loop = _loop(cache=None, store=store)
        await loop._evaluate(pos, {"AAPL": Decimal("195")})
        # nothing triggers: price far from target, premium unchanged, dte n/a
        # (option_mid_and_dte with no rh_tools returns dte only, mid=None → returns early)
        assert len(await store.all()) == 1


class TestParseNextEarningsDate:
    def test_picks_nearest_upcoming_date(self):
        loop = _loop(cache=None, rh_tools={})
        result = {"data": {"results": [
            {"symbol": "AAPL", "report_date": "2020-01-01"},  # past — excluded
            {"symbol": "AAPL", "report_date": "2099-06-15"},
            {"symbol": "AAPL", "report_date": "2099-03-01"},
        ]}}
        assert loop._parse_next_earnings_date(result) == date(2099, 3, 1)

    def test_no_upcoming_dates_returns_none(self):
        loop = _loop(cache=None, rh_tools={})
        result = {"data": {"results": [{"symbol": "AAPL", "report_date": "2020-01-01"}]}}
        assert loop._parse_next_earnings_date(result) is None

    def test_empty_result_returns_none(self):
        loop = _loop(cache=None, rh_tools={})
        assert loop._parse_next_earnings_date({"data": {"results": []}}) is None


class TestNextEarningsDate:
    async def test_returns_none_when_tool_unavailable(self):
        loop = _loop(cache=None, rh_tools={})
        assert await loop._next_earnings_date("AAPL") is None

    async def test_fetches_and_caches(self):
        t = MagicMock()
        t.ainvoke = AsyncMock(return_value={"data": {"results": [
            {"symbol": "AAPL", "report_date": "2099-06-15"},
        ]}})
        loop = _loop(cache=None, rh_tools={"get_earnings_calendar": t})

        first = await loop._next_earnings_date("AAPL")
        second = await loop._next_earnings_date("AAPL")

        assert first == date(2099, 6, 15)
        assert second == date(2099, 6, 15)
        t.ainvoke.assert_called_once()  # second call served from cache

    async def test_failed_fetch_returns_none(self):
        t = MagicMock()
        t.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
        loop = _loop(cache=None, rh_tools={"get_earnings_calendar": t})
        assert await loop._next_earnings_date("AAPL") is None


class TestParseSpreadPct:
    def test_computes_spread_as_fraction_of_mid(self):
        loop = _loop(cache=None, rh_tools={})
        result = {"data": {"results": [{"price_book": {"bid_price": "2.90", "ask_price": "3.10"}}]}}
        # mid=3.00, spread=0.20 → 0.20/3.00
        assert loop._parse_spread_pct(result) == pytest.approx(Decimal("0.0667"), abs=Decimal("0.001"))

    def test_missing_bid_ask_returns_none(self):
        loop = _loop(cache=None, rh_tools={})
        assert loop._parse_spread_pct({"data": {"results": [{}]}}) is None


class TestOptionSpreadPct:
    async def test_returns_none_when_tool_unavailable(self):
        store = PositionStore()
        pos = _position()
        loop = _loop(cache=None, rh_tools={}, store=store)
        assert await loop._option_spread_pct(pos) is None

    async def test_fetches_spread_using_cached_instrument_id(self):
        t = MagicMock()
        t.ainvoke = AsyncMock(return_value={"data": {"results": [
            {"price_book": {"bid_price": "2.90", "ask_price": "3.10"}}
        ]}})
        pos = _position()  # option_instrument_id="opt-1" already set
        loop = _loop(cache=None, rh_tools={"get_option_price_book": t})

        result = await loop._option_spread_pct(pos)

        assert result is not None
        t.ainvoke.assert_called_once_with({"instrument_ids": ["opt-1"]})


class TestExitLimitPriceWithSpread:
    def _signal(self, reason: ExitReason, premium: str = "5.00"):
        from trader.exits.schemas import ExitSignal

        return ExitSignal(
            position_id="p1", ticker="AAPL", contract=_contract(), reason=reason,
            current_premium=Decimal(premium), entry_premium=Decimal("3.00"),
            pnl_pct=0.5, dte_remaining=14, as_of=datetime.now(timezone.utc),
        )

    def test_wide_spread_biases_toward_bid_on_profit_target(self):
        loop = _loop(cache=None, rh_tools={})
        signal = self._signal(ExitReason.PROFIT_TARGET)
        price = loop._exit_limit_price(signal, spread_pct=Decimal("0.20"))
        assert price == Decimal("5.00") * Decimal("0.97")

    def test_narrow_spread_leaves_price_unchanged_on_profit_target(self):
        loop = _loop(cache=None, rh_tools={})
        signal = self._signal(ExitReason.PROFIT_TARGET)
        price = loop._exit_limit_price(signal, spread_pct=Decimal("0.05"))
        assert price == Decimal("5.00")

    def test_stop_loss_discount_takes_priority_over_spread_bias(self):
        loop = _loop(cache=None, rh_tools={})
        signal = self._signal(ExitReason.STOP_LOSS)
        price = loop._exit_limit_price(signal, spread_pct=Decimal("0.50"))
        assert price == Decimal("5.00") * Decimal("0.95")
