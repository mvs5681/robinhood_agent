"""Integration tests for Phase 8: Backtest Harness.

Runs entirely on local fixtures — no live API calls.
Fixture root: tests/fixtures/history/
Dates covered: 2026-01-02 (entry) and 2026-01-05 (profit-target exit).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from trader.backtest.data_store import BacktestDataSlice, DataStore
from trader.backtest.harness import BacktestHarness
from trader.backtest.metrics import BacktestResult, TradeMetrics
from trader.backtest.policy import PolicyAdapter, StandardPolicy
from trader.backtest.schemas import BacktestPosition, BacktestTradeRecord
from trader.exits.schemas import ExitReason
from trader.uw.schemas import OptionContract

HISTORY_ROOT = Path(__file__).parent.parent / "fixtures" / "history"
START = date(2026, 1, 2)
END = date(2026, 1, 5)
TICKERS = ["AAPL"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> DataStore:
    return DataStore(HISTORY_ROOT)


@pytest.fixture
def policy() -> StandardPolicy:
    return StandardPolicy()


@pytest.fixture
async def result(store: DataStore, policy: StandardPolicy) -> BacktestResult:
    harness = BacktestHarness(policy, store, START, END, TICKERS)
    return await harness.run()


# ---------------------------------------------------------------------------
# DataStore
# ---------------------------------------------------------------------------


class TestDataStore:
    def test_available_dates_includes_fixture_dates(self, store: DataStore):
        dates = store.available_dates()
        assert date(2026, 1, 2) in dates
        assert date(2026, 1, 5) in dates

    def test_available_dates_are_sorted(self, store: DataStore):
        dates = store.available_dates()
        assert dates == sorted(dates)

    def test_load_returns_slice_with_correct_date(self, store: DataStore):
        s = store.load(date(2026, 1, 2))
        assert isinstance(s, BacktestDataSlice)
        assert s.date == date(2026, 1, 2)

    def test_load_detects_aapl_ticker(self, store: DataStore):
        s = store.load(date(2026, 1, 2))
        assert "AAPL" in s.tickers

    def test_load_missing_date_raises_file_not_found(self, store: DataStore):
        with pytest.raises(FileNotFoundError, match="2020-01-01"):
            store.load(date(2020, 1, 1))

    def test_slice_tools_expose_required_names(self, store: DataStore):
        s = store.load(date(2026, 1, 2))
        names = {t.name for t in s.as_tools()}
        required = {
            "get_market_tide",
            "get_flow_alerts",
            "get_greek_exposure_by_strike",
            "get_dark_pool_trades",
            "get_flow_per_strike",
            "get_options_chain",
            "get_interpolated_iv",
            "get_extended_technical_indicator",
        }
        assert required.issubset(names)

    async def test_tools_are_async_callable(self, store: DataStore):
        s = store.load(date(2026, 1, 2))
        tool_map = {t.name: t for t in s.as_tools()}
        tide = await tool_map["get_market_tide"].ainvoke({})
        assert isinstance(tide, dict)
        assert "data" in tide

    async def test_ticker_tool_dispatches_on_ticker_kwarg(self, store: DataStore):
        s = store.load(date(2026, 1, 2))
        tool_map = {t.name: t for t in s.as_tools()}
        result = await tool_map["get_greek_exposure_by_strike"].ainvoke({"ticker": "AAPL"})
        assert "data" in result
        assert len(result["data"]) == 7

    async def test_technical_tool_dispatches_on_function_kwarg(self, store: DataStore):
        s = store.load(date(2026, 1, 2))
        tool_map = {t.name: t for t in s.as_tools()}
        result = await tool_map["get_extended_technical_indicator"].ainvoke(
            {"ticker": "AAPL", "function": "RSI", "interval": "daily"}
        )
        assert "data" in result
        assert len(result["data"]) >= 1

    def test_get_spot_price_from_flow_alerts(self, store: DataStore):
        s = store.load(date(2026, 1, 2))
        price = s.get_spot_price("AAPL")
        assert price is not None
        assert float(price) == pytest.approx(195.50, rel=1e-3)

    def test_get_spot_price_day_two(self, store: DataStore):
        s = store.load(date(2026, 1, 5))
        price = s.get_spot_price("AAPL")
        assert price is not None
        assert float(price) == pytest.approx(201.50, rel=1e-3)

    def test_get_spot_price_unknown_ticker_returns_none(self, store: DataStore):
        s = store.load(date(2026, 1, 2))
        assert s.get_spot_price("XYZ") is None

    def test_get_option_premium_day_two(self, store: DataStore):
        s = store.load(date(2026, 1, 5))
        contract = OptionContract(
            ticker="AAPL",
            expiry=date(2026, 1, 30),
            strike=Decimal("200"),
            type="call",
            bid=Decimal("4.50"),
            ask=Decimal("5.00"),
            open_interest=9200,
            volume=5200,
        )
        prem = s.get_option_premium(contract)
        assert prem is not None
        assert float(prem) == pytest.approx(4.75, rel=1e-3)

    def test_get_option_premium_wrong_strike_returns_none(self, store: DataStore):
        s = store.load(date(2026, 1, 5))
        contract = OptionContract(
            ticker="AAPL",
            expiry=date(2026, 1, 30),
            strike=Decimal("999"),   # not in fixture
            type="call",
            bid=Decimal("0.01"),
            ask=Decimal("0.02"),
            open_interest=1,
            volume=1,
        )
        assert s.get_option_premium(contract) is None


# ---------------------------------------------------------------------------
# BacktestHarness construction guards
# ---------------------------------------------------------------------------


class TestHarnessGuards:
    def test_rejects_future_start_date(self, store: DataStore, policy: StandardPolicy):
        from datetime import timedelta

        future = date.today() + timedelta(days=1)
        with pytest.raises(ValueError, match="start_date must be in the past"):
            BacktestHarness(policy, store, start_date=future, end_date=future, tickers=TICKERS)

    def test_rejects_today_as_start_date(self, store: DataStore, policy: StandardPolicy):
        today = date.today()
        with pytest.raises(ValueError, match="start_date must be in the past"):
            BacktestHarness(policy, store, start_date=today, end_date=today, tickers=TICKERS)

    def test_rejects_inverted_date_range(self, store: DataStore, policy: StandardPolicy):
        with pytest.raises(ValueError, match="start_date"):
            BacktestHarness(
                policy,
                store,
                start_date=date(2026, 1, 5),
                end_date=date(2026, 1, 2),
                tickers=TICKERS,
            )


# ---------------------------------------------------------------------------
# Full run — end-to-end
# ---------------------------------------------------------------------------


class TestHarnessRun:
    async def test_returns_backtest_result(self, result: BacktestResult):
        assert isinstance(result, BacktestResult)

    async def test_overall_metrics_present(self, result: BacktestResult):
        assert isinstance(result.overall, TradeMetrics)

    async def test_at_least_one_trade_entered(self, result: BacktestResult):
        assert result.overall.trade_count >= 1

    async def test_trade_entered_on_first_date(
        self, store: DataStore, policy: StandardPolicy
    ):
        harness = BacktestHarness(policy, store, START, END, TICKERS)
        result = await harness.run()
        entry_dates = {r.entry_date for r in result.records}
        assert date(2026, 1, 2) in entry_dates

    async def test_holds_past_entry_wall_when_live_wall_advances(
        self, result: BacktestResult
    ):
        # dynamic_exits_enabled defaults True (StandardPolicy()'s bare
        # ExitMonitor()) — gate 1 resolves the *live* wall each tick instead
        # of the frozen entry snapshot. Day 2 spot (201.50) crosses the
        # entry-snapshotted wall (200), but that day's live spot_gex shows a
        # bigger wall at 210 (600M gamma vs 400M at 205) now that 200 has
        # dropped below spot — the position holds for that wall instead of
        # taking the small early profit at the stale entry snapshot, so the
        # day-2 entry doesn't close within the fixture's 2-day window.
        first = result.records[0]
        assert first.entry_date == date(2026, 1, 2)
        assert first.position.target_level == Decimal("200")  # entry snapshot, unchanged
        assert first.status != "closed"

    async def test_profit_target_exit_is_positive_pnl(self, result: BacktestResult):
        closed = [r for r in result.records if r.status == "closed"]
        for record in closed:
            if record.exit_signal.reason == ExitReason.PROFIT_TARGET:
                assert record.pnl_pct > 0

    async def test_win_rate_positive(self, result: BacktestResult):
        if result.overall.closed_count > 0:
            assert result.overall.win_rate > 0

    async def test_by_regime_keyed_to_regime_string(self, result: BacktestResult):
        assert isinstance(result.by_regime, dict)
        assert len(result.by_regime) > 0
        for key in result.by_regime:
            assert key in ("positive", "negative", "mixed")

    async def test_by_setup_type_populated(self, result: BacktestResult):
        assert isinstance(result.by_setup_type, dict)
        assert len(result.by_setup_type) > 0

    async def test_by_regime_and_setup_uses_colon_separator(self, result: BacktestResult):
        for key in result.by_regime_and_setup:
            assert ":" in key

    async def test_max_concurrent_positions_respected(
        self, store: DataStore, policy: StandardPolicy
    ):
        harness = BacktestHarness(
            policy, store, START, END, TICKERS, max_concurrent_positions=1
        )
        result = await harness.run()
        # Only 1 position at a time → at most 2 trades over 2 days (1 per day)
        assert result.overall.trade_count <= 2

    async def test_records_carry_candidate_detail(self, result: BacktestResult):
        for record in result.records:
            assert isinstance(record, BacktestTradeRecord)
            assert record.candidate is not None
            assert record.position is not None
            assert record.entry_date is not None


# ---------------------------------------------------------------------------
# Static exits (dynamic_exits_enabled=False) — same fixture, different policy
# ---------------------------------------------------------------------------


class TestHarnessRunWithStaticExits:
    async def test_profit_target_fires_on_day_two(self, store: DataStore):
        # With dynamic exits off, gate 1 uses the entry-snapshotted
        # target_level (200) unconditionally — day 2's spot (201.50) has
        # already crossed it, so the position closes there instead of
        # holding for the live-advanced wall at 210.
        from trader.exits.monitor import ExitMonitor

        policy = StandardPolicy(exit_monitor=ExitMonitor(dynamic_exits_enabled=False))
        harness = BacktestHarness(policy, DataStore(HISTORY_ROOT), START, END, TICKERS)
        result = await harness.run()

        closed = [r for r in result.records if r.status == "closed"]
        assert len(closed) >= 1
        profit_targets = [
            r for r in closed if r.exit_signal.reason == ExitReason.PROFIT_TARGET
        ]
        assert len(profit_targets) >= 1
        assert any(r.exit_date == date(2026, 1, 5) for r in closed)


# ---------------------------------------------------------------------------
# Incremental replay — step_forward()
# ---------------------------------------------------------------------------


class TestStepForward:
    """step_forward() must produce the same trade outcomes as run() when
    covering the same window, whether called once or resumed across
    multiple invocations with the state round-tripped through JSON in
    between (exactly how BacktestLoop uses it night to night)."""

    @staticmethod
    def _static_policy() -> StandardPolicy:
        # These two tests' narrative depends on the specific
        # profit-target-close-then-reenter trade sequence this fixture
        # produces under the original static thresholds — orthogonal to what
        # they're actually verifying (state persistence/resumption fidelity
        # between step_forward() calls), so pin the exit mode explicitly
        # rather than coupling it to whatever the shared `policy` fixture's
        # default happens to be.
        from trader.exits.monitor import ExitMonitor

        return StandardPolicy(exit_monitor=ExitMonitor(dynamic_exits_enabled=False))

    async def test_single_call_over_full_window_matches_run(self, store: DataStore):
        policy = self._static_policy()
        harness = BacktestHarness(policy, store, START, END, TICKERS)
        state = await harness.step_forward()
        result = await BacktestHarness(self._static_policy(), store, START, END, TICKERS).run()

        assert len(state.records) == len(result.records)
        closed = [r for r in state.records if r.status == "closed"]
        assert any(r.exit_signal.reason == ExitReason.PROFIT_TARGET for r in closed)

    async def test_resuming_across_two_calls_matches_single_call(self, store: DataStore):
        from trader.backtest.state import ReplayState

        policy = self._static_policy()

        # Night 1: only 2026-01-02 is "available" yet — simulate by using a
        # harness whose end_date stops right there.
        harness_day1 = BacktestHarness(policy, store, START, date(2026, 1, 2), TICKERS)
        state = await harness_day1.step_forward()

        assert state.processed_dates == [date(2026, 1, 2)]
        assert len(state.open_positions) == 1  # entered, not yet exited

        # Persist and reload exactly as BacktestLoop would between nights
        state = ReplayState.from_dict(state.to_dict())

        # Night 2: full window now available — step_forward must only
        # process 2026-01-05 (already-processed 2026-01-02 is skipped)
        harness_full = BacktestHarness(policy, store, START, END, TICKERS)
        state = await harness_full.step_forward(state)

        assert state.processed_dates == [date(2026, 1, 2), date(2026, 1, 5)]
        # Day-2 fixture data also qualifies for a fresh entry right after the
        # profit-target exit closes the day-1 position — matches what run()
        # over the same full window produces (same trade, marked 'expired'
        # there instead of 'open' since run() considers the window finished).
        closed = [r for r in state.records if r.status == "closed"]
        assert len(closed) == 1
        assert closed[0].exit_signal.reason == ExitReason.PROFIT_TARGET
        assert closed[0].pnl_pct > 0
        assert len(state.open_positions) == 1
        assert state.records[-1].status == "open"
        assert state.records[-1].entry_date == date(2026, 1, 5)

    async def test_resuming_does_not_reopen_or_duplicate_trades(
        self, store: DataStore, policy: StandardPolicy
    ):
        harness = BacktestHarness(policy, store, START, END, TICKERS)
        state = await harness.step_forward()
        first_pass_count = len(state.records)

        # Calling step_forward again with nothing new available must be a no-op
        state = await harness.step_forward(state)
        assert len(state.records) == first_pass_count

    async def test_open_positions_not_marked_expired_unlike_run(
        self, store: DataStore, policy: StandardPolicy
    ):
        # Stop the window right after entry, before the exit fires — run()
        # would mark this 'expired'; step_forward() must leave it 'open'
        # since the replay is only paused, not finished.
        harness = BacktestHarness(policy, store, START, date(2026, 1, 2), TICKERS)
        state = await harness.step_forward()
        assert len(state.records) == 1
        assert state.records[0].status == "open"


# ---------------------------------------------------------------------------
# Policy ABC
# ---------------------------------------------------------------------------


class TestPolicyAdapter:
    def test_standard_policy_satisfies_abc(self):
        assert issubclass(StandardPolicy, PolicyAdapter)

    def test_should_enter_requires_proposed_status(self, policy: StandardPolicy):
        from trader.gex.schemas import GEXRegime, GEXSetup
        from trader.scoring.schemas import BlendScores, CandidateSignal
        from datetime import datetime, timezone

        setup = GEXSetup(
            ticker="AAPL",
            as_of=datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc),
            spot_price=Decimal("195"),
            regime=GEXRegime.POSITIVE,
            flip_point=None,
            nearest_call_wall=None,
            nearest_put_wall=None,
            target_level=Decimal("200"),
            candidate_direction="call",
            setup_type="pin",
            structure_confidence=0.8,
            raw_gex_by_strike=[],
        )
        contract = OptionContract(
            ticker="AAPL",
            expiry=date(2026, 1, 30),
            strike=Decimal("200"),
            type="call",
            bid=Decimal("2.90"),
            ask=Decimal("3.10"),
            open_interest=9000,
            volume=4500,
        )
        candidate = CandidateSignal(
            ticker="AAPL",
            as_of=datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc),
            gex_setup=setup,
            blend_scores=BlendScores(
                market_tide=0.7, darkpool=0.8, flow_pressure=0.7,
                iv_cost=0.6, technicals=0.75, composite=0.71,
            ),
            execution_status="proposed",
            selected_contract=contract,
        )
        assert policy.should_enter(candidate) is True
        skipped = candidate.model_copy(update={"execution_status": "skipped_no_flow"})
        assert policy.should_enter(skipped) is False

    def test_should_enter_requires_selected_contract(self, policy: StandardPolicy):
        from trader.gex.schemas import GEXRegime, GEXSetup
        from trader.scoring.schemas import BlendScores, CandidateSignal
        from datetime import datetime, timezone

        setup = GEXSetup(
            ticker="AAPL",
            as_of=datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc),
            spot_price=Decimal("195"),
            regime=GEXRegime.POSITIVE,
            flip_point=None,
            nearest_call_wall=None,
            nearest_put_wall=None,
            target_level=Decimal("200"),
            candidate_direction="call",
            setup_type="pin",
            structure_confidence=0.8,
            raw_gex_by_strike=[],
        )
        candidate = CandidateSignal(
            ticker="AAPL",
            as_of=datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc),
            gex_setup=setup,
            blend_scores=BlendScores(
                market_tide=0.7, darkpool=0.8, flow_pressure=0.7,
                iv_cost=0.6, technicals=0.75, composite=0.71,
            ),
            execution_status="proposed",
            selected_contract=None,
        )
        assert policy.should_enter(candidate) is False
