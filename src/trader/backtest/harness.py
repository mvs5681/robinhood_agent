"""Phase 8 — Backtest harness for temporal replay."""

from __future__ import annotations

import logging
from datetime import date

from .data_store import DataStore
from .metrics import BacktestResult, compute_backtest_result
from .policy import PolicyAdapter
from .schemas import BacktestPosition, BacktestTradeRecord

logger = logging.getLogger(__name__)


class BacktestHarness:
    """
    Temporal replay of the full trading pipeline over a window of historical dates.

    For each date in [start_date, end_date] that has fixture data:
      1. Evaluate exits for all open positions (exits first, then entries)
      2. If below position cap, run generate_and_score for new candidates
      3. Enter positions for candidates that pass should_enter

    Positions still open at end_date are marked 'expired' in the trade records.

    Temporal split guarantee: start_date must be strictly in the past relative
    to today so that future/current data cannot leak into the backtest.
    """

    def __init__(
        self,
        policy: PolicyAdapter,
        data_store: DataStore,
        start_date: date,
        end_date: date,
        tickers: list[str],
        max_concurrent_positions: int = 3,
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

    async def run(self) -> BacktestResult:
        """Execute the replay and return a BacktestResult with trade-level records."""
        open_positions: list[BacktestPosition] = []
        records: list[BacktestTradeRecord] = []
        record_by_id: dict[str, BacktestTradeRecord] = {}

        for trade_date in self.data_store.available_dates():
            if not (self.start_date <= trade_date <= self.end_date):
                continue

            data_slice = self.data_store.load(trade_date)
            logger.debug("backtest: processing %s (%d open positions)", trade_date, len(open_positions))

            # 1. Evaluate exits for all currently open positions
            still_open: list[BacktestPosition] = []
            for pos in open_positions:
                exit_signal = self.policy.should_exit(pos, data_slice)
                record = record_by_id[pos.position_id]
                if exit_signal:
                    record.close(exit_signal, trade_date)
                    logger.info(
                        "%s: EXIT %s %s  reason=%s  pnl=%.1f%%",
                        trade_date,
                        pos.ticker,
                        pos.contract.type,
                        exit_signal.reason.value,
                        exit_signal.pnl_pct * 100,
                    )
                else:
                    still_open.append(pos)
            open_positions = still_open

            # 2. Skip entry scan if at position cap
            if len(open_positions) >= self.max_concurrent_positions:
                logger.debug(
                    "%s: at cap (%d/%d), skipping entry scan",
                    trade_date,
                    len(open_positions),
                    self.max_concurrent_positions,
                )
                continue

            # 3. Generate candidates via the pipeline
            candidates = await self.policy.generate_and_score(self.tickers, data_slice)

            # 4. Enter new positions for qualifying candidates
            for candidate in candidates:
                if len(open_positions) >= self.max_concurrent_positions:
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

                pos = BacktestPosition.from_candidate(candidate, trade_date)
                record = BacktestTradeRecord(
                    position=pos,
                    candidate=candidate,
                    entry_date=trade_date,
                )
                open_positions.append(pos)
                records.append(record)
                record_by_id[pos.position_id] = record
                logger.info(
                    "%s: ENTER %s %s @%.2f  target=%.2f  composite=%.3f",
                    trade_date,
                    pos.ticker,
                    pos.contract.type,
                    float(pos.entry_premium),
                    float(pos.target_level),
                    candidate.blend_scores.composite,
                )

        # Mark any positions still open at end of window as expired
        for pos in open_positions:
            record_by_id[pos.position_id].expire(self.end_date)
            logger.debug("backtest: %s expired at %s", pos.ticker, self.end_date)

        return compute_backtest_result(records)
