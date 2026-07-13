# TODO

## Backtesting against real history

Goal: replace "is the strategy profitable?" guesswork with measured win rate,
avg P&L, and drawdown per regime/setup type. The harness works
(`python -m trader.backtest.cli`) but only has synthetic fixtures today.

- [ ] Check what historical data the UW subscription exposes (flow alerts,
      GEX by strike, darkpool, option chains — how far back, which endpoints)
- [ ] Build a daily capture job that snapshots the live UW responses the
      pipeline consumes into `tests/fixtures/history/<date>/` format
      (or a dedicated `data/history/` dir) so replay data accrues going forward
- [ ] Backfill as many past days as the API allows
- [ ] Run the harness over the accumulated history; review metrics by regime
      and setup type (`by_regime`, `by_setup_type` in `BacktestResult`)
- [ ] Use results to tune the live dials: flow min premium, discovery premium,
      selector DTE/delta window, stop-loss / DTE floor
- [ ] Re-run the backtest after each tuning change to confirm improvement
      before applying it to the live config

## Later / nice to have

- [ ] Expose the contract selector window (DTE min/max, delta min/max) in the
      dashboard Settings tab
- [ ] Extend `NYSE_HOLIDAYS` in `market_hours.py` before 2028
- [ ] Update README pipeline description to match code (selector window is
      DTE 21–30, delta 0.30–0.45 — README says 7–45 / 0.30–0.55)
- [ ] Persist ProposalStore / RiskEngine daily P&L across restarts (kill-switch
      state currently resets when the container restarts)
- [ ] Sector map for the risk engine's sector-concentration gate (currently
      no map is wired, so the gate is inactive)
