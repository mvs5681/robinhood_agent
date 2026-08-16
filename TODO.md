# TODO

## Backtesting against real history

Goal: replace "is the strategy profitable?" guesswork with measured win rate,
avg P&L, and drawdown per regime/setup type — and now also a direct
comparison against what the live account actually did, to surface gaps
between backtested and real strategy behavior. The manual CLI
(`scripts/run_backtest.py`) evaluates "what would the current config have
done over this window"; the dashboard's Backtest tab is a separate,
cumulative "what has this strategy actually decided, day by day" track
record (see CHANGELOG 2026-08-15).

- [x] Check what historical data the UW subscription exposes — confirmed:
      only `get_greek_exposure_by_strike`/`get_market_tide`/`get_flow_per_strike`
      support historical `date=` filtering; flow alerts/darkpool/options
      chain are current-data-only, hence the daily capture job below.
- [x] Build a daily capture job that snapshots the live UW responses the
      pipeline consumes into `data/history/<date>/` — `CaptureLoop` +
      `StateCaptureLoop`, both wired into `run_live.py`, firing at 4:30pm ET.
      18 trading days accumulated so far (2026-07-22 → 2026-08-14).
- [x] Fix `get_interpolated_iv` never being fetched — missing from
      `ALLOWED_TOOL_NAMES`, so `interpolated_iv` was silently empty in every
      live scan and every captured fixture, permanently. See CHANGELOG
      2026-08-15. Captures from now on will have real IV data; the 18 days
      already on disk likely can't be backfilled (current-data-only endpoint).
- [x] Run the harness over the accumulated history — `BacktestLoop` does
      this nightly now, incrementally, feeding the dashboard's Backtest tab.
      `scripts/run_backtest.py` remains available for one-off manual runs
      with different params (metrics already sliced `by_regime`/
      `by_setup_type` in `BacktestResult`).
- [x] `BacktestLoop` defaults to `bypass_flow_gate=True` — confirmed live
      against the real corpus that captured `flow_alerts.json` snapshots
      can be many hours stale relative to `FlowTrigger`'s 4h lookback,
      rejecting 100% of candidates without it. See CHANGELOG 2026-08-15.
- [x] Capture intraday flow-alert timing — `FlowAlertCapture`, piggybacked
      on `FlowWatcher`'s 60s poll, appends real-timestamped alerts to
      `data/history/<date>/flow_alerts_intraday.jsonl`. See CHANGELOG
      2026-08-15. Data starts accruing from deployment forward — the
      once-daily `flow_alerts.json` snapshot can never be backfilled with
      accurate intraday timing (current-data-only endpoint).
- [ ] Consume `flow_alerts_intraday.jsonl` in `DataStore`/`BacktestHarness`
      once enough days have accumulated, so the flow-confirmation gate can
      be genuinely replayed instead of relying on `bypass_flow_gate`. Needs
      `FlowTrigger`'s lookback check to filter the intraday log by real
      `created_at` relative to each replay day's `as_of`, not just read a
      single snapshot file.
- [ ] Backfill as many past days as the API allows for the endpoints that
      support it, to widen the regime coverage faster than daily capture
      alone accumulates it.
- [ ] Use results to tune the live dials: flow min premium, discovery premium,
      selector DTE/delta window, stop-loss / DTE floor.
- [ ] Re-run the backtest after each tuning change to confirm improvement
      before applying it to the live config.
- [ ] **v2: per-trade divergence matching.** The current dashboard
      comparison is aggregate-only (trade count, win rate, avg P&L side by
      side) — deliberately deferred: matching a specific real trade to "the
      backtest would have taken this same ticker around this date" to
      explain *why* they diverge, not just *that* they do. Would need
      fuzzy ticker/date matching (real trade timestamps are intraday;
      backtest trades are date-granularity until the intraday capture
      work below lands) and handling for trades one side took that the
      other didn't at all. Also the natural home for entry-slippage
      tracking (quoted mid-price at proposal time vs. actual average fill
      premium) — currently uncaptured on the real side entirely.
- [x] Intraday flow-alert log — done, see the `FlowAlertCapture` item above.
- [ ] Increase capture granularity beyond once-daily for slow-moving GEX
      data too: hourly snapshots piggybacked on `GEXScanner`'s existing
      hourly refresh — zero extra UW calls, just persisting more of what's
      already fetched. Would raise the ceiling on how precise v2's matching
      above can be.

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
- [x] Exit loop currently checks thesis invalidation via a simple
      direction-flip rule (live `candidate_direction` vs. the held contract's
      type). Consider also comparing wall distance/structure confidence
      drift from entry for earlier, more nuanced invalidation — would need
      Position to retain the entry-time GEXSetup snapshot.
      Done as part of the dynamic-exits work: `Position.entry_gex_setup` now
      holds the entry snapshot, and `ExitMonitor._thesis_confidence_decayed`
      exits on confidence-decay/wall-drift even without a full direction
      flip. Gated behind `LiveConfig.dynamic_exits_enabled` — validated in
      backtest and enabled live (default True as of the enable-dynamic-exits
      branch; `BacktestLoop`'s nightly replay uses the same setting).
- [ ] Earnings-gap and liquidity-aware exits (new in the dynamic-exits work)
      are live-only: `days_to_earnings`/`spread_pct` come from RH
      `get_earnings_calendar`/`get_option_price_book`, neither threaded into
      `BacktestDataSlice` — `StandardPolicy.should_exit` never passes them,
      so these two gates can't be backtested against existing history. Same
      current-data-only limitation as flow alerts/darkpool above; would need
      a new daily capture source for earnings dates + EOD price-book snapshots.
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
