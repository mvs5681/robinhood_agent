"""Phase 8 — Backtest harness for temporal replay."""

from __future__ import annotations

import logging
from decimal import Decimal
from datetime import date

from .data_store import DataStore
from .metrics import BacktestResult, compute_backtest_result
from .policy import PolicyAdapter
from .schemas import BacktestPosition, BacktestTradeRecord
from .state import ReplayState

logger = logging.getLogger(__name__)

_MAX_CONTRACTS = 10   # hard cap per trade regardless of budget


class BacktestHarness:
    """
    Temporal replay of the full trading pipeline over a window of historical dates.

    For each date in [start_date, end_date] that has fixture data:
      1. Evaluate exits for all open positions (exits first, then entries)
      2. If below position cap, run generate_and_score for new candidates
      3. Enter positions for candidates that pass should_enter

    Portfolio simulation (when initial_capital is set):
      - Cash starts at initial_capital.
      - Each entry spends min(max_trade_pct × cash, mid × 100) per contract,
        buying as many contracts as the budget allows (min 1).
      - Each exit returns current_premium × contracts × 100 back to cash.
      - An equity curve (date → portfolio value) is computed daily.

    Two ways to run a replay:
      - run(): one-shot, stateless — fresh positions/cash, walks the whole
        window every call. Positions still open at end_date are marked
        'expired'. Used by the manual CLI (scripts/run_backtest.py) to
        evaluate "what would the CURRENT strategy config have done across
        this whole window" — retuning a dial and re-running silently
        reapplies it across all of history.
      - step_forward(): incremental — advances a ReplayState by every
        available date not yet in state.processed_dates, carrying open
        positions/cash/records forward. Positions are NOT marked expired
        (the replay isn't over). Used for the daily cumulative track
        record: each day's entries keep whatever config was live when they
        were scored, so retuning a dial only affects days going forward.
    """

    def __init__(
        self,
        policy: PolicyAdapter,
        data_store: DataStore,
        start_date: date,
        end_date: date,
        tickers: list[str],
        max_concurrent_positions: int = 3,
        initial_capital: float | None = None,
        max_trade_pct: float = 0.25,
    ) -> None:
        today = date.today()
        if start_date >= today:
            raise ValueError(
                f"start_date must be in the past (got {start_date}, today is {today})"
            )
        if start_date > end_date:
            raise ValueError(
                f"start_date {start_date} must be ≤ end_date {end_date}"
            )

        self.policy = policy
        self.data_store = data_store
        self.start_date = start_date
        self.end_date = end_date
        self.tickers = tickers
        self.max_concurrent_positions = max_concurrent_positions
        self.initial_capital = initial_capital
        self.max_trade_pct = max_trade_pct

    @property
    def simulate(self) -> bool:
        return self.initial_capital is not None

    def _contracts_for_budget(self, mid: Decimal, cash: Decimal) -> int:
        """How many contracts can we buy within max_trade_pct of cash?"""
        if mid <= 0:
            return 1
        budget = cash * Decimal(str(self.max_trade_pct))
        cost_per = mid * 100
        n = int(budget / cost_per)
        return max(1, min(_MAX_CONTRACTS, n))

    async def _step_one_day(self, trade_date: date, state: ReplayState) -> None:
        """Advance the replay by exactly one trading day, mutating state in place."""
        simulate = self.simulate
        data_slice = self.data_store.load(trade_date)
        record_by_id = state.record_by_id
        logger.debug("backtest: processing %s (%d open positions)",
                     trade_date, len(state.open_positions))

        # 1. Evaluate exits for all currently open positions
        still_open: list[BacktestPosition] = []
        for pos in state.open_positions:
            exit_signal = self.policy.should_exit(pos, data_slice)
            record = record_by_id[pos.position_id]
            if exit_signal:
                record.close(exit_signal, trade_date)
                if simulate:
                    proceeds = exit_signal.current_premium * pos.contracts * 100
                    state.cash += proceeds
                logger.info(
                    "%s: EXIT %s %s  reason=%s  pnl=%.1f%%  pnl_$=%s",
                    trade_date,
                    pos.ticker,
                    pos.contract.type,
                    exit_signal.reason.value,
                    exit_signal.pnl_pct * 100,
                    f"${record.pnl_dollars:+.2f}" if record.pnl_dollars is not None else "n/a",
                )
            else:
                still_open.append(pos)
        state.open_positions = still_open

        # 2. Skip entry scan if at position cap
        if len(state.open_positions) >= self.max_concurrent_positions:
            logger.debug(
                "%s: at cap (%d/%d), skipping entry scan",
                trade_date,
                len(state.open_positions),
                self.max_concurrent_positions,
            )
        else:
            # 3. Generate candidates via the pipeline
            candidates = await self.policy.generate_and_score(self.tickers, data_slice)

            # 4. Enter new positions for qualifying candidates
            for candidate in candidates:
                if len(state.open_positions) >= self.max_concurrent_positions:
                    break
                if not self.policy.should_enter(candidate):
                    logger.debug(
                        "%s: SKIP %s  status=%s  composite=%.3f",
                        trade_date,
                        candidate.ticker,
                        candidate.execution_status,
                        candidate.blend_scores.composite,
                    )
                    continue

                mid = candidate.selected_contract.mid
                contracts = 1
                if simulate:
                    contracts = self._contracts_for_budget(mid, state.cash)
                    entry_cost = mid * contracts * 100
                    if entry_cost > state.cash:
                        logger.info(
                            "%s: SKIP %s — insufficient cash ($%.2f < $%.2f needed)",
                            trade_date, candidate.ticker, float(state.cash), float(entry_cost),
                        )
                        continue
                    state.cash -= entry_cost

                pos = BacktestPosition.from_candidate(candidate, trade_date, contracts=contracts)
                record = BacktestTradeRecord(
                    position=pos,
                    candidate=candidate,
                    entry_date=trade_date,
                )
                state.open_positions.append(pos)
                state.records.append(record)
                logger.info(
                    "%s: ENTER %s %s @%.2f  x%d contracts  cost=$%.2f  composite=%.3f",
                    trade_date,
                    pos.ticker,
                    pos.contract.type,
                    float(pos.entry_premium),
                    pos.contracts,
                    float(pos.entry_cost),
                    candidate.blend_scores.composite,
                )

        # Daily equity snapshot: cash + mark-to-market open positions
        if simulate:
            open_mtm = sum(
                float(
                    (data_slice.get_option_premium(p.contract) or p.entry_premium)
                    * p.contracts * 100
                )
                for p in state.open_positions
            )
            state.equity_curve.append((trade_date, float(state.cash) + open_mtm))

        state.processed_dates.append(trade_date)

    async def run(self) -> BacktestResult:
        """Execute a one-shot replay from start_date to end_date and return
        a BacktestResult with trade-level records. See class docstring for
        how this differs from step_forward()."""
        state = ReplayState(
            cash=Decimal(str(self.initial_capital)) if self.simulate else Decimal("0")
        )

        for trade_date in self.data_store.available_dates():
            if not (self.start_date <= trade_date <= self.end_date):
                continue
            await self._step_one_day(trade_date, state)

        # Mark any positions still open at end of window as expired
        record_by_id = state.record_by_id
        for pos in state.open_positions:
            record_by_id[pos.position_id].expire(self.end_date)
            logger.debug("backtest: %s expired at %s", pos.ticker, self.end_date)

        return compute_backtest_result(
            state.records,
            equity_curve=state.equity_curve if self.simulate else None,
            initial_capital=float(self.initial_capital) if self.simulate else None,
        )

    async def step_forward(self, state: ReplayState | None = None) -> ReplayState:
        """Advance `state` (or a fresh one) through every available date not
        already in state.processed_dates, within [start_date, end_date].

        Unlike run(), open positions are left open at the end of this call —
        the replay isn't over, it's just paused until the next invocation.
        Pass the ReplayState returned here back in next time to resume
        exactly where it left off, without re-processing already-stepped
        dates. See class docstring for the "cumulative track record" vs.
        "what would current config have done historically" distinction.
        """
        if state is None:
            state = ReplayState(
                cash=Decimal(str(self.initial_capital)) if self.simulate else Decimal("0")
            )

        already = set(state.processed_dates)
        for trade_date in self.data_store.available_dates():
            if trade_date in already:
                continue
            if not (self.start_date <= trade_date <= self.end_date):
                continue
            await self._step_one_day(trade_date, state)

        return state
