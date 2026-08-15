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

- [x] Reconcile working (unfilled/partially-filled) orders at startup —
      `OrderLifecycleManager.adopt_working_orders()` now sweeps every
      non-terminal order state (queued/confirmed/partially_filled/
      pending_cancelled) and splits partial fills into an immediately
      protected `Position` + a continued working order for the remainder.
- [x] Expose the contract selector window (DTE min/max, delta min/max) in the
      dashboard Settings tab
- [x] Extend `NYSE_HOLIDAYS` in `market_hours.py` before 2028
- [x] Update README pipeline description to match code (selector window is
      DTE 21–30, delta 0.30–0.45 — README says 7–45 / 0.30–0.55)
- [x] Persist RiskEngine daily P&L / kill-switch across restarts
      (`logs/risk_state.json`, resets at midnight UTC)
- [ ] Persist `ProposalStore` across restarts — lower priority than the
      RiskEngine state above since pending proposals expire after 30 min
      anyway; only matters for exact continuity of in-flight approvals
      across a restart.
- [x] Sector map wiring for the risk engine's sector-concentration gate is
      code-complete (`SECTOR_MAP_FILE` env var, `sector_map.example.json`) —
      still needs a real `sector_map.json` populated and deployed for the
      gate to actually activate; currently inactive with no map on disk.
- [x] Fix test isolation in `test_risk_engine.py` — autouse fixture now
      monkeypatches the default state path to a per-test `tmp_path`. This
      was genuinely flaky, not hypothetical: it silently loaded the real
      production kill-switch trip into "clean" test engines whenever the
      persisted date happened to match test-run day.
- [x] Fix the kill-switch itself staying tripped for two weeks straight —
      `_maybe_roll_day()` now checks for a day rollover live, on every
      `check()`/`record_pnl()`, not only at `RiskEngine.__init__`. See
      CHANGELOG 2026-08-15.
- [ ] Exit loop currently checks thesis invalidation via a simple
      direction-flip rule (live `candidate_direction` vs. the held contract's
      type). Consider also comparing wall distance/structure confidence
      drift from entry for earlier, more nuanced invalidation — would need
      Position to retain the entry-time GEXSetup snapshot.
- [x] Fix autonomous-mode duplicate-signal dedup — `_recent_attempts` now
      gates all three execution modes uniformly (was a no-op for autonomous,
      63 repeated buying-power rejections in one afternoon).
- [ ] The underlying "not enough overnight buying power" account condition
      itself is still unresolved — the dedup fix stops the hammering, but
      doesn't address why buying power is exhausted. Check margin/overnight
      buying power directly in the Robinhood account.
- [ ] Consider a longer, reason-specific cooldown for systemic/account-level
      rejections (e.g. buying power) vs. the standard 30-min per-ticker
      cooldown — a fresh whale print on a *different* ticker will still hit
      the same account-wide wall immediately today.
- [x] Keep held-position tickers in the scanner's discovery universe every
      cycle so their GEXCache entry (and thesis-invalidation/trailing-stop
      checks) doesn't go stale once the ticker stops trending.
- [x] Harden `reconcile_positions` against a false "0 positions" result at
      startup (retry once + log raw response on empty) — found live, 3 real
      positions went unprotected for one restart cycle.
- [x] Fix the actual root cause: `_to_position` read `get_option_positions`'
      own `"type"` field (long/short) as if it were call/put and rejected
      every real position. `reconcile_positions` now enriches each position
      via `get_option_instruments` (batch `ids=` lookup) for the real
      strike/type. Verified against the real account — recovers all 3 open
      positions with correct strikes/types/premiums.
- [x] Checked `order_manager.py`'s adoption path for the same class of gap —
      it reads `strike_price`/`option_type` directly from order leg data
      (`get_option_orders`), which genuinely includes both fields natively
      (unlike `get_option_positions`). Not affected.
- [x] Full audit for the same three bug classes (startup-only state, API
      field misassumptions, silent-empty results) plus a fourth (guards not
      applied uniformly across execution modes). Two real bugs found and
      fixed — see CHANGELOG 2026-08-15:
      `OrderLifecycleManager.adopt_working_orders()` had the reconciler's
      exact silent-zero gap (now retries once + logs raw responses), and
      `Executor` never re-checked `risk_engine.check()` immediately before
      placing an `rh_approval`-mode order approved via Telegram/dashboard
      (now re-verified in `_autonomous()`/`_rh_approval()` right before
      `place_option_order`).
