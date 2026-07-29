"""Tests for FlowWatcher's mode-agnostic duplicate-signal cooldown.

Regression coverage for a live bug: ProposalStore.has_recent() was the sole
dedup guard, but AUTONOMOUS mode never calls proposal_store.add() (it
dispatches straight to the executor), so the guard was always a no-op for
that mode — the same ticker re-attempted a doomed order on every new whale
print with zero cooldown (63 rejections across 5 tickers in one afternoon,
observed live). _recent_attempts tracks an attempt the moment the watcher
commits to dispatching, regardless of mode.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trader.executor.schemas import ExecutionMode, OrderRequest, OrderResult
from trader.gex.schemas import GEXRegime, GEXSetup
from trader.live.cache import GEXCache
from trader.live.position_store import PositionStore
from trader.live.proposals import ProposalStore
from trader.live.watcher import _ATTEMPT_COOLDOWN_SECONDS, FlowWatcher
from trader.scoring.schemas import BlendScores, CandidateSignal
from trader.uw.schemas import OptionContract


def _contract(ticker: str = "SPY") -> OptionContract:
    return OptionContract(
        ticker=ticker, expiry=date(2026, 8, 28),
        strike=Decimal("755"), type="call", bid=Decimal("6.10"), ask=Decimal("6.30"),
        open_interest=5000, volume=1000, delta=Decimal("0.34"),
    )


def _candidate(ticker: str = "SPY") -> CandidateSignal:
    setup = GEXSetup(
        ticker=ticker, as_of=datetime.now(timezone.utc), spot_price=Decimal("750"),
        regime=GEXRegime.NEGATIVE, flip_point=None, nearest_call_wall=None,
        nearest_put_wall=None, target_level=Decimal("770"), candidate_direction="call",
        setup_type="momentum", structure_confidence=0.6, raw_gex_by_strike=[],
    )
    return CandidateSignal(
        ticker=ticker, as_of=setup.as_of, gex_setup=setup,
        blend_scores=BlendScores(market_tide=0.5, darkpool=0.5, flow_pressure=0.5,
                                 iv_cost=0.5, technicals=0.5, composite=0.5),
        execution_status="proposed", selected_contract=_contract(ticker),
    )


def _watcher(mode: ExecutionMode, executor=None, position_store=None) -> FlowWatcher:
    cache = GEXCache()
    return FlowWatcher(
        uw_tools={}, cache=cache, proposal_store=ProposalStore(),
        execution_mode=mode, executor=executor or MagicMock(),
        position_store=position_store,
    )


def _rejected_result() -> OrderResult:
    request = OrderRequest(
        candidate=_candidate(), action="buy_to_open", quantity=1,
        limit_price=Decimal("6.20"), mode=ExecutionMode.AUTONOMOUS,
    )
    return OrderResult(request=request, placed=False,
                       rejection_reason="no buying power", timestamp=datetime.now(timezone.utc))


class TestRecentAttemptsTracking:
    def test_not_attempted_initially(self):
        watcher = _watcher(ExecutionMode.AUTONOMOUS)
        assert watcher._recently_attempted("SPY") is False

    def test_attempted_within_cooldown(self):
        watcher = _watcher(ExecutionMode.AUTONOMOUS)
        watcher._record_attempt("SPY")
        assert watcher._recently_attempted("SPY") is True

    def test_expires_after_cooldown(self):
        watcher = _watcher(ExecutionMode.AUTONOMOUS)
        watcher._record_attempt("SPY")
        watcher._recent_attempts["SPY"] = (
            datetime.now(timezone.utc) - timedelta(seconds=_ATTEMPT_COOLDOWN_SECONDS + 1)
        )
        assert watcher._recently_attempted("SPY") is False

    def test_other_ticker_unaffected(self):
        watcher = _watcher(ExecutionMode.AUTONOMOUS)
        watcher._record_attempt("SPY")
        assert watcher._recently_attempted("IWM") is False

    def test_prune_drops_only_expired_entries(self):
        watcher = _watcher(ExecutionMode.AUTONOMOUS)
        watcher._recent_attempts["OLD"] = (
            datetime.now(timezone.utc) - timedelta(seconds=_ATTEMPT_COOLDOWN_SECONDS + 100)
        )
        for i in range(200):
            watcher._recent_attempts[f"T{i}"] = datetime.now(timezone.utc)
        watcher._record_attempt("NEW")
        assert "OLD" not in watcher._recent_attempts
        assert "NEW" in watcher._recent_attempts


class TestAutonomousModeDedup:
    """End-to-end: repeated failed autonomous attempts on the same ticker
    within the cooldown window must not re-invoke the executor."""

    async def test_second_pipeline_run_within_cooldown_is_skipped(self):
        executor = MagicMock()
        executor.execute = AsyncMock(return_value=_rejected_result())
        executor.account_number = "123"
        watcher = _watcher(ExecutionMode.AUTONOMOUS, executor=executor)

        candidate = _candidate("SPY")
        with patch("trader.live.watcher.score_candidates", return_value={"candidates": [candidate]}), \
             patch("trader.live.watcher.check_flow", return_value={}), \
             patch("trader.live.watcher.select_contracts", return_value={}), \
             patch("trader.live.watcher.risk_gate", return_value={}):
            snap = MagicMock()
            snap.gex_setup = candidate.gex_setup
            snap.is_stale = False
            snap.spot_gex = []
            snap.darkpool = []
            snap.net_prem_ticks = []
            snap.option_contracts = []
            snap.interpolated_iv = []
            snap.technicals = {}
            watcher.cache.snapshot = AsyncMock(return_value=snap)

            await watcher._run_pipeline("SPY", [])
            await watcher._run_pipeline("SPY", [])  # new whale print, same ticker

        assert executor.execute.await_count == 1

    async def test_different_tickers_both_attempted(self):
        executor = MagicMock()
        executor.execute = AsyncMock(return_value=_rejected_result())
        executor.account_number = "123"
        watcher = _watcher(ExecutionMode.AUTONOMOUS, executor=executor)

        def _snap_for(ticker):
            candidate = _candidate(ticker)
            snap = MagicMock()
            snap.gex_setup = candidate.gex_setup
            snap.is_stale = False
            snap.spot_gex = []
            snap.darkpool = []
            snap.net_prem_ticks = []
            snap.option_contracts = []
            snap.interpolated_iv = []
            snap.technicals = {}
            return candidate, snap

        spy_candidate, spy_snap = _snap_for("SPY")
        iwm_candidate, iwm_snap = _snap_for("IWM")
        snaps = {"SPY": spy_snap, "IWM": iwm_snap}
        candidates = {"SPY": spy_candidate, "IWM": iwm_candidate}
        watcher.cache.snapshot = AsyncMock(side_effect=lambda t: snaps[t])

        for ticker in ("SPY", "IWM"):
            with patch("trader.live.watcher.score_candidates",
                      return_value={"candidates": [candidates[ticker]]}), \
                 patch("trader.live.watcher.check_flow", return_value={}), \
                 patch("trader.live.watcher.select_contracts", return_value={}), \
                 patch("trader.live.watcher.risk_gate", return_value={}):
                await watcher._run_pipeline(ticker, [])

        assert executor.execute.await_count == 2
