# Changelog

## 2026-08-15 (capture intraday flow alerts — start collecting real timing)

- **New `FlowAlertCapture`** (`src/trader/live/flow_capture.py`), piggybacked
  on `FlowWatcher`'s existing 60s poll — zero extra UW API calls. Appends
  every newly-seen flow alert (real `created_at` timestamp) to a per-day
  `data/history/<date>/flow_alerts_intraday.jsonl` log, deduped by the same
  `(ticker, expiry, strike, type, created_at)` composite key `FlowWatcher`
  already uses internally for its own pipeline-trigger dedup. Captures
  every fetched alert regardless of whether the ticker is in the current
  GEX-cache universe — maximizes future backtest coverage, not just what
  today's pipeline happens to act on.
  Directly motivated by the `bypass_flow_gate` finding below: the once-daily
  `flow_alerts.json` snapshot can be many hours stale by the time it's
  captured (confirmed live — a 2026-08-14 capture held alerts timestamped
  2026-08-12), which is why the backtest currently has to bypass the flow
  gate entirely to produce any simulated trades. `get_flow_alerts` has no
  historical `date=` filtering, so this intraday log is the only way to
  ever recover real flow timing — every day without it is unrecoverable.
  Consuming this log in `DataStore`/`BacktestHarness` to replace
  `bypass_flow_gate` with genuine flow-gated replay is a separate,
  deliberately deferred follow-up (needs several days of accumulated data
  to be useful in the first place — see TODO.md).

## 2026-08-15 (bypass_flow_gate — real captured data always failed the flow gate)

- **`BacktestLoop` produced zero simulated trades against the real 18-day
  captured corpus — found by actually running it locally.** Root-caused
  precisely: `data/history/2026-08-14/flow_alerts.json`'s captured alerts
  are timestamped `2026-08-12` — a single end-of-day snapshot of "whatever
  the UW feed currently returns" can be many hours (here, ~44h) stale
  relative to the replay day, which is always outside `FlowTrigger`'s
  default 4h lookback. Every candidate, every ticker, every day, rejected
  at the flow gate — 100%, not reduced fidelity.
  `BacktestLoop` gained `bypass_flow_gate: bool = True` (also
  `BACKTEST_BYPASS_FLOW_GATE` env var in `run_live.py`), threaded to
  `StandardPolicy`, same mechanism `scripts/fetch_polygon_history.py`'s
  backtests already use. Verified against real data: 0 trades → 7 trades
  (4 closed, win rate 50%) once bypassed. The dashboard now shows a visible
  "Flow gate bypassed" warning badge plus an explanatory note whenever
  `bypass_flow_gate` is active, so the simulated numbers are never mistaken
  for a full-fidelity backtest — they reflect GEX regime + contract
  selection + exit logic only, not the flow-confirmation edge the live
  strategy also requires. Revisit once intraday flow-alert logging
  (TODO.md) lands and captured flow data is no longer single-snapshot.
- Note for anyone re-running `BacktestLoop` locally with a state file that
  predates this change: already-`processed_dates` are never
  re-evaluated — that's the incremental model working as designed (a
  config change only affects days going forward), but it also means an old
  state file won't retroactively pick up `bypass_flow_gate`. Delete
  `backtest_state.json`/`backtest_results.json` to replay from scratch.

## 2026-08-15 (backtest-vs-reality dashboard tab)

Goal: a daily, cumulative simulated track record that can be compared
against what the live account actually did over the same period, to
surface gaps between backtested and real strategy behavior — not just "is
the backtest profitable" in isolation.

- **`BacktestHarness` gained a second, incremental replay mode.** The
  existing `run()` (stateless, whole-window, used by `scripts/run_backtest.py`
  for "what would the current strategy config have done historically")
  is unchanged. New `step_forward(state)` advances a persisted `ReplayState`
  (open positions, cash, full trade log, equity curve) through only the
  dates not yet processed, so a nightly job can build up a cumulative track
  record without re-walking the entire growing history from scratch each
  time — each day's entries keep whatever config was live when they were
  scored, instead of a retune silently reapplying across all of history.
  `ReplayState.to_dict()`/`from_dict()` round-trip through JSON (Pydantic's
  `model_dump(mode="json")` handles the nested `CandidateSignal`/
  `ExitSignal`/`OptionContract` fields; `BacktestPosition`/
  `BacktestTradeRecord` are serialized explicitly since they're plain
  dataclasses).
- **New `BacktestLoop`** (`src/trader/live/backtest_loop.py`), wired into
  `run_live.py` alongside the other daily loops: runs once per trading day
  at 5:00pm ET (30 min after `CaptureLoop`/`StateCaptureLoop` finish, so
  that day's fixtures are guaranteed on disk), steps the replay forward
  over `data/history/`, and persists `data/backtest_state.json` (full
  resumable state) + `data/backtest_results.json` (display payload: overall
  + by-regime/by-setup-type metrics both all-time and trailing-90-day, a
  $2000-simulated equity curve, and a capped recent-trades list).
  Runs via `asyncio.to_thread()` — the harness's async call chain does no
  genuine I/O yielding against local JSON fixtures (it's "async" in
  signature only), so calling it directly would block the same event loop
  `FlowWatcher`'s 60s poll and `ExitLoop`'s 20s poll run on for the full
  replay duration. Read-only against `data/history/`, fully isolated from
  RiskEngine/PositionStore/Executor — a failure here can't touch live
  trading.
- **New "Backtest" dashboard tab** — side-by-side simulated-vs-actual stat
  panels (win rate, avg P&L, trade count) over the same window, with the
  trade-count delta called out prominently: this needs no per-trade
  matching to be useful — "47 simulated trades vs 3 real trades" is
  immediately legible as a strategy-vs-reality gap on its own (exactly what
  the kill-switch/reconciliation/dedup bugs earlier this project would have
  looked like from this view). New `GET /api/backtest` endpoint serves the
  persisted results; the real side reuses the existing `/api/telemetry/pnl`
  endpoint client-side rather than duplicating trade data server-side.
  Clearly labeled "Simulated — not real trades" to avoid confusion with the
  real P&L tab, which it deliberately mirrors in style.
- **Telemetry: `exit_signal` events now carry `quantity`, `entry_regime`,
  `entry_setup_type`.** Two gaps identified while designing the comparison:
  real exits had no dollar-P&L basis (no quantity) and no regime/setup_type
  slicing (unlike backtest trades, which always had `GEXSetup` context via
  `CandidateSignal`) — meaning the comparison could only ever report blunt
  aggregates, not *where* strategy and reality diverge. `Position` gained
  `entry_regime`/`entry_setup_type` fields (populated from the entry-time
  `CandidateSignal.gex_setup` in `order_manager.py::_promote()` and
  `position_store.py::make_position()`; `None` for reconciled/adopted
  positions where that context doesn't exist), threaded through to
  `TelemetryLogger.exit_signal()` and surfaced in
  `TelemetryReader.pnl_series()`. Entry-fill slippage (quoted vs. actual
  fill price) remains uncaptured — deferred to the per-trade divergence
  matching in TODO.md's v2 item, same bucket as explaining *why* trades
  diverge rather than just *that* they do.

## 2026-08-15 (clean up orphaned MockUWTools mock module)

- **Removed `src/trader/uw/mock_tools.py` and its dedicated test file** —
  found while fixing the `get_spot_exposures_by_strike` tool-name drift
  below: this whole `MockUWTools` class was dead code. It was imported in
  `test_agent_graph.py` but never actually instantiated there (that file's
  real tests use ad hoc `MagicMock` tool fixtures instead), leaving it
  exercised only by its own `test_mock_tools.py`. Its method names had
  drifted heavily from the real production tool names it was meant to
  mirror — 6 of its ~11 methods (`get_stock_flow_alerts`,
  `get_darkpool_ticker`, `get_net_prem_ticks`, `get_option_contracts`,
  `get_greeks`, `get_technical_indicator`, `get_option_contracts_screener`)
  don't match anything in `ALLOWED_TOOL_NAMES`. The real backtest harness
  has used `BacktestDataSlice`/`DataStore` for this purpose since Phase 8;
  this module predates that and was never cleaned up.
  Also fixed the same tool-name drift in `test_agent_graph.py`'s
  `test_pipeline_errors_do_not_crash` (ad hoc tool list used 3 names that
  don't exist in production — didn't affect what the test actually
  asserted, but was misleading) and a cosmetic label in
  `scripts/demo_dashboard.py`'s synthetic telemetry generator.

## 2026-08-15 (fix: get_interpolated_iv never actually fetched)

- **`interpolated_iv` has been silently empty for every ticker, every day,
  in both live trading and every captured backtest fixture — found while
  scoping how to backtest against `data/history/`.** The feature was fully
  built: `InterpolatedIVEntry` schema, `parse_interpolated_iv` validator,
  `TickerSnapshot.interpolated_iv` field, `iv_cost_score()` consumer (20% of
  the blend composite weight), the `state_capture.py` serializer, and the
  backtest mock tools all exist and are correct. But `get_interpolated_iv`
  was never added to `ALLOWED_TOOL_NAMES` in `trader/uw/mcp_config.py`, so
  the MCP client filtered it out before `scanner.py` ever had a tool handle
  to call — `_scan_ticker()` had no fetch call for it at all. `iv_cost_score()`
  degrades gracefully to a neutral `0.5` on empty input, so this didn't crash
  or bias anything — it just meant the IV-cost dimension of scoring
  contributed zero real signal since the scanner was first written.
  Fixed by adding `get_interpolated_iv` to `ALLOWED_TOOL_NAMES` and wiring
  the fetch call into `_scan_ticker()`, in the fetch order the module's own
  docstring always claimed ("spot GEX, darkpool, net-prem ticks, option
  contracts, IV, technicals"). Per `fetch_history.py`'s documented UW
  endpoint coverage, `interpolated-iv` isn't in the list of endpoints that
  support historical `date=` filtering, so this only benefits captures going
  forward — the 18 days already in `data/history/` will likely stay
  IV-blind permanently.

## 2026-08-15 (full bug-class audit — order-adoption silent gap, risk-check bypass)

Follow-up to the reconciliation and kill-switch bugs below: rather than wait
for the next one to surface live, audited the codebase for the same three
bug classes (startup-only state, API field misassumptions, silent-empty
results) plus a fourth (guards that don't apply uniformly across all three
execution modes). Two real, unfixed bugs came out of it:

- **`OrderLifecycleManager.adopt_working_orders()` had the exact same
  silent-empty gap the reconciler had before its fix.** A false "0 orders to
  adopt" at startup — from a parse miss rather than genuine emptiness — was
  indistinguishable from the common, legitimate case: no log line either
  way. An already-placed, already-paying order would go completely
  unmonitored (no reprice, no give-up, never promoted to a tracked position)
  for the container's entire lifetime, with no trail to diagnose it after
  the fact. Now retries the full 4-state sweep once after a short delay and
  logs the raw per-state responses on a final zero, mirroring the
  reconciler's fix.
- **`risk_engine.check()` was never re-verified at the moment of placement
  for delayed-approval order flows.** `Executor` held no reference to the
  risk engine at all — `check()` runs exactly once, at proposal creation,
  inside the graph's `risk_gate()` node. For `autonomous` mode that's
  effectively atomic with placement, but `rh_approval` mode's real
  production path — a human tapping Approve in Telegram or the dashboard,
  arbitrarily long after the proposal was created — calls
  `execute_approved()`, which places without ever re-checking the risk
  gate. A kill-switch trip (or a position-cap/sector-cap breach) between
  proposal and approval did not block the placement. `Executor` now takes
  an optional `risk_engine` and re-verifies `check()` immediately before
  `place_option_order` in both `_autonomous()` (covering
  `execute_approved()`, which routes through it) and `_rh_approval()`'s own
  interrupt-resume placement path.

## 2026-08-15 (kill-switch stayed tripped for two weeks — day rollover fix)

- **Root cause of "no trades in 2 weeks"**: the July 30 reconciliation fix
  (below) correctly closed 3 previously-unprotected, deeply-underwater
  positions on its first tick (CRWV `thesis_invalidated` −50.5%, IWM
  `stop_loss` −64.5%, SMCI `stop_loss` −65.7%), realizing a combined −$772
  loss that correctly tripped the 5%-of-NAV daily-loss kill-switch — the
  risk system working exactly as designed. But `RiskEngine`'s documented
  "resets at midnight UTC" only ran inside `_load_state()`, called once at
  `__init__` — a long-running container that never restarts never
  re-evaluates it. Since nothing restarted the container in the two weeks
  since, the kill-switch stayed latched in memory the entire time: every
  single `risk_check` since (1,658 of them, 100%) was rejected with
  `kill_switch_active: daily loss limit reached`, and zero `order_attempt`
  events occurred in that window — a one-day circuit breaker silently
  became an indefinite full halt.
  `RiskEngine` now checks for a day rollover (`_maybe_roll_day()`) at the
  top of every `check()` and `record_pnl()` call, not only at startup —
  daily P&L and the kill-switch reset live within one call of UTC midnight,
  no restart required. Open positions and sector counts are untouched by a
  rollover (not daily-scoped). New tests in `TestKillSwitchDayRollover`
  cover the reset itself, that it persists to disk, that open positions
  survive it, and that a fresh bad day can retrip it afterward.
- **Fixed real (non-flaky) test isolation in `test_risk_engine.py`** — every
  test constructed a bare `RiskEngine()` with no explicit `state_file`,
  so all of them read/wrote the *real* `logs/risk_state.json`. This was
  calendar-dependent flakiness, not a hypothetical: earlier the same day
  the production kill-switch tripped, this suite's `RiskEngine()` calls
  silently loaded that real trip (persisted date happened to match "today"
  at the time), failing ~10 tests that expected a clean engine — exactly
  the failures tracked as "pre-existing, unrelated" throughout this
  session. New autouse fixture monkeypatches the module's default state
  path to a per-test `tmp_path`, isolating every existing call site with
  no changes to the test bodies themselves.
- Also fixed a hardcoded near-term contract expiry in `test_exit_loop.py`
  that had gone stale as real time caught up to it, spuriously triggering
  `dte_stop` in tests that expected no exit — same underlying bug class
  (state/fixtures that silently go stale with time) in test code instead
  of production code.

## 2026-07-30 (the real reconciliation bug: missing instrument enrichment)

- **Fix the actual root cause behind "no open positions found" — found by
  continuing to investigate after the empty-result retry (below) didn't
  change the outcome on a real restart.** `get_option_positions` has no
  `strike_price` field, and its own `"type"` field means `"long"`/`"short"`
  (position direction) — not `"call"`/`"put"`. `_to_position` was reading
  that `"type"` field as the option's call/put type, so every single real
  position was silently rejected, every time, almost certainly since this
  function was first written — the retry fix below was necessary but not
  sufficient, since `items` was never actually empty; `_to_position` was
  just discarding every real item it was handed.
  `reconcile_positions` now batch-fetches each position's real strike/type
  via `get_option_instruments` (`ids=<comma-separated option_ids>`) before
  conversion — confirmed correct against the real account: recovers CRWV
  ($80c), IWM ($302c), and SMCI ($35c) with correct strikes, types, and
  entry premiums, verified against live-captured API responses for both
  endpoints. `tests/unit/test_reconciler.py` rewritten to use the real
  field shapes throughout (the previous version's fixtures had
  `strike_price`/`option_type` directly on the position dict — a shape
  that doesn't exist on the real API, which is exactly why those tests
  passed while production silently failed).

## 2026-07-30 (harden startup reconciliation against empty results)

- **Fix: reconciliation silently reported 0 open positions while 3 were
  genuinely open** — found live during a force-recreate: container logs
  showed "Reconciliation complete — no open positions found in Robinhood"
  moments after a live `get_option_positions` query confirmed CRWV, IWM,
  and SMCI were all still open. `_parse_positions` was verified correct in
  isolation for the exact live response shape (`{"data": {"positions":
  [...]}}`), so this wasn't a parsing bug — the most likely cause is the
  RH MCP session churn visible in the same log window (several
  connect/400/reconnect cycles within ~3 seconds, right around the
  reconciliation call). Net effect: all three positions ran with **zero**
  stop-loss/DTE/thesis-invalidation/trailing-stop protection until the
  next successful restart.
  `reconcile_positions` now retries once (3s backoff) if the first fetch
  parses to 0 items before accepting it as ground truth, and logs the raw
  response on a final empty result so a real recurrence is diagnosable
  from container logs alone instead of requiring a live re-query to prove
  or disprove. New `tests/unit/test_reconciler.py` (11 tests) locks in the
  live response shape parsing and the retry behavior.

## 2026-07-29 (fix autonomous-mode duplicate-signal dedup)

- **Fix: autonomous mode had no duplicate-signal cooldown** — found live:
  63 rejected order attempts across 5 tickers (SPY x35) in one afternoon,
  all "not enough overnight buying power," with zero backoff. Root cause:
  the sole dedup guard, `ProposalStore.has_recent(ticker)`, only reflects
  tickers that had a `Proposal` added via `proposal_store.add()` —
  `PROPOSE_ONLY`/`RH_APPROVAL` call that, so the guard worked for them, but
  `AUTONOMOUS` mode dispatches straight to `executor.execute()` and never
  touches `ProposalStore`, so `has_recent()` was always `False` there — a
  no-op. Every new whale print re-attempted a doomed order against the same
  wall, all day, on any ticker whose orders keep failing at the broker (an
  open position never got created to trip the *other* guard either).
  Replaced with `FlowWatcher._recent_attempts`, a mode-agnostic tracker
  recorded the moment the watcher commits to dispatching a candidate
  (before the mode branch), checked uniformly for all three modes with the
  same 30-minute cooldown `ProposalStore` already used. Removed
  `ProposalStore.has_recent()` (now fully superseded, no other callers).

## 2026-07-29 (keep held positions in the discovery universe)

- **Fix: thesis-invalidation/trailing-stop silently going stale for a held
  position** — found live: CRWV's GEX regime had been `mixed/none` for 9+
  days (since 7/20), which should trigger `THESIS_INVALIDATED` immediately,
  but the exit hadn't fired. Root cause: `ExitLoop` only acts on
  `GEXCache`, and the scanner's per-cycle universe is driven purely by
  *current* flow-alert volume — once CRWV stopped trending, it fell out of
  discovery entirely, its cache entry aged past the 65-minute staleness
  threshold, and `_current_gex_setup()` correctly (per its own logic)
  returned `None` rather than act on stale data. Net effect: a position
  can lose thesis/trailing-stop protection simply by going quiet, with no
  error or log line calling it out.
  `GEXScanner` now takes an optional `position_store` and force-includes
  every ticker with an open position in each scan cycle — the same
  treatment `seed_tickers` already get (exempt from the discovery premium
  threshold and ticker cap), including the all-slices-failed fallback path.
  Wired in `run_live.py`.

## 2026-07-29 (trailing/give-back stop exit)

- **New exit condition: trailing stop** — closes a gap thesis invalidation
  doesn't cover: a position (e.g. CRWV, bought 7/16 at $4.70) can gain real
  profit and then decay back toward zero *without* the GEX regime ever
  structurally flipping — thesis invalidation only fires on a regime/
  direction change, so a position whose thesis stays technically intact but
  whose price simply reverses would previously ride all the way down with no
  protection (no profit-target touch, no stop-loss trip from *entry*).
  `Position` now carries `peak_premium` — the highest premium observed since
  entry, updated and persisted by `ExitLoop` every tick (a dip never lowers
  it). `ExitReason.TRAILING_STOP` arms once `peak_premium` reaches
  `entry * (1 + trailing_stop_activation_pct)` (default 30% gain), then
  fires once the current premium has given back
  `trailing_stop_giveback_pct` (default 50%) of the gain achieved at that
  peak. Checked after profit-target and thesis-invalidation, before the
  plain entry-based stop-loss. Both new percentages are dashboard-editable
  (Settings tab) via `LiveConfig`, synced into `ExitMonitor` each tick like
  the other exit dials. Also exposed `wall_proximity_pct` in the Settings
  tab — it was added to `LiveConfig` in the earlier proximity-exit change
  but never wired into the dashboard form.

## 2026-07-22 (restart-awareness + thesis-invalidation exit)

- **Fix restart-awareness gap in order adoption** — `OrderLifecycleManager
  .adopt_working_orders()` only swept `confirmed`/`queued` order states at
  startup. Per the live `get_option_orders` schema, `partially_filled` and
  `pending_cancelled` are also non-terminal — an order restarted mid-fill or
  mid-cancel was completely invisible to the agent, meaning contracts that
  already cost real money sat unmonitored (no stop-loss, no DTE floor) until
  the order eventually resolved on its own. Now sweeps all four states, and
  a partially-filled order is split at adoption: the already-filled quantity
  is immediately promoted to a protected `Position` (using the true average
  fill premium), while the remaining unfilled quantity continues as a
  working order. An adopted `pending_cancelled` order is marked
  cancelling+giving_up so it isn't blindly re-chased into a cancel someone
  else already initiated.
- **Remove `reconciler.reconcile_open_orders()`** — this parallel
  reconciliation path (added alongside the order lifecycle manager) used the
  wrong MCP parameter (`placed_by_agent: true` instead of the schema's
  `placed_agent: "agentic"`), so it silently no-op'd on every restart. Worse,
  had it worked, it promoted *unfilled* orders directly to full-quantity
  `Position` objects — meaning the exit loop would evaluate stop-loss/DTE
  against contracts that were never actually bought. Fully superseded by the
  fix above; removed rather than patched.
- **New exit condition: thesis invalidation** — the exit loop previously only
  reacted to price and DTE. `ExitReason.THESIS_INVALIDATED` fires when the
  ticker's *live* GEX setup no longer supports the direction the position
  was bought for (regime went mixed, or flipped to the opposite side) —
  checked after profit-target but before the price-based stop-loss, so a
  soured setup exits before the position has to fully round-trip through the
  stop-loss threshold. `ExitLoop` now takes the shared `GEXCache` and looks
  up the current `GEXSetup` per position each tick (skipped if the cached
  setup is stale — no fresh read, no action). `ExitMonitor.evaluate()` takes
  the live setup as a plain, optional argument; fully backward compatible
  when omitted.

## 2026-07-15 13:05 EDT

- **Round limit prices onto the instrument's tick grid** — the approved TQQQ
  order was rejected by Robinhood with "Price does not satisfy the min tick
  value": non-penny options tick in $0.05/$0.10 steps, and the agent priced
  at the raw bid/ask mid. New `rh/ticks.py::round_price_to_tick` floors every
  buy, replacement, and exit limit onto the grid using the instrument's
  `min_ticks` rule (penny fallback when unknown). Executor resolves
  `min_ticks` with the instrument id; exit loop and order manager fetch it
  per instrument.

## 2026-07-15 12:05 EDT

- **Order lifecycle manager** (`live/order_manager.py`) — placement is no
  longer fire-and-forget. A new polling loop (20s) tracks every placed buy:
  positions are only created on **actual fill** (with the true average fill
  premium, not the requested limit); an order unfilled for 2 minutes is
  cancelled and re-placed stepping toward the ask (fresh mid → mid/ask
  midpoint → ask, max 3 replacements, always capped at the risk engine's
  $500-per-contract premium); after 10 minutes total it cancels for good and
  notifies. At startup, still-working agentic buy orders from before the
  restart are adopted so a post-restart fill still becomes a monitored
  position (closes the working-order reconciliation TODO). Telegram messages
  on fill and on give-up. `cancel_option_order` added to the RH tool
  allowlist.

## 2026-07-15 11:20 EDT

- **Fix order-id extraction for the live place response** (first real order!)
  — the first approved order (HOOD $130c 8/14, $4.20 limit) placed
  successfully at Robinhood but was misreported as failed and left untracked:
  `place_option_order` nests the order at `data.order.id`, one level deeper
  than the extractor checked. Regression test uses the exact live response
  shape. Until the container restarts and reconciles, a fill of that order is
  unmonitored — restart promptly after this fix.

## 2026-07-14 15:35 EDT — Deployment, bug-fix, and hardening sweep (Jul 12–14)

Covers all changes since the live agent was containerized. Ordered newest first.

### Order execution

- **Fix exit-loop quotes so stop-loss/DTE exits can actually fire** (`d57a352`)
  — dry-running the sell path against the live RH MCP found the exit loop
  calling `get_option_quotes` with `option_ids` (schema wants
  `instrument_ids`; strict schema rejected every call) and both quote parsers
  reading fields at the item top level when RH nests them under
  `results[].quote.*` — the option premium was always None, so exit
  evaluation silently skipped every tick. Verified instrument resolution,
  option mid, equity spots, and order-id extraction against live-captured
  payloads.
- **Fix orders sent with a bogus option_id; no more phantom "executed" states**
  (`397b361`) — `rh_call` returned Robinhood responses still wrapped in the MCP
  content envelope; the executor mistook the envelope for the instrument list
  and sent the langchain block id (`lc_...`) to Robinhood as the option to buy,
  then reported `placed=True` despite the response containing no order id
  (dashboard said "executed", no order existed). `rh_call` now unwraps the
  envelope centrally, payload extraction handles RH's
  `{"data": {"results": [...]}}` nesting in the executor and exit loop (whose
  quote parsing consumed the same raw envelope — exits would never have
  fired), and a place response without an order id is reported as
  `placed=False` with the response snippet.
- **Fix "unexpected ref_id" on order approval** (`b3b2fe8`) — `review_option_order`
  and `place_option_order` have strict, *different* MCP schemas, but the executor
  sent both the same params dict. Review rejected `ref_id`; place would then have
  rejected `chain_symbol`/`underlying_type`. Params are now built per endpoint.
  The exit loop's auto-sell orders had the same defect and now also carry a
  deterministic `uuid5(position_id:reason)` idempotency key so a retried exit
  can never double-sell.
- **Fix dashboard Approve button** (`78ecda5`) — the endpoint called
  `executor.execute()`, which in `rh_approval` mode hits a LangGraph
  `interrupt()` outside any graph and 500s. Now uses `execute_approved()` and
  records the fill in `PositionStore` so exits monitor it.
- **Prevent duplicate order placement** (`78ecda5`) — `ProposalStore.approve()`/
  `reject()` now return None unless this call made the pending→decided
  transition (double taps and redelivered Telegram callbacks previously
  executed twice), and `approve()` enforces the 30-min TTL.

### Signals & pipeline

- **Fix Telegram proposal notifications** (`5780c91`) — the notifier read
  `gex_setup.direction` but the field is `candidate_direction`; every proposal
  notification died on AttributeError after the proposal was stored. All of
  today's 20 NVDA proposals were affected.
- **Gate duplicate proposals** (`5780c91`) — one live signal per ticker: no new
  proposal while one exists within the TTL window or a position is open.
  Previously every new whale print re-proposed the same signal (NVDA: 20
  proposals in under an hour; autonomous mode would have re-bought).
- **Fix contract selection starvation** (`dd120f1`) — the unfiltered options
  chain returned an arbitrary 50 contracts (SPY: only 0/1/4/18 DTE), so the
  selector's 21–30 DTE / 0.30–0.45 delta window was empty by construction.
  Contracts now come from `get_options_screener` filtered server-side to the
  selector window, with real bid/ask quotes.
- **Cut watcher trigger noise** (`dd120f1`) — the 60s flow poll now filters
  server-side to the flow-confirmation premium, so pipeline runs only fire on
  prints that could actually confirm a trade.
- **Widen ticker discovery ~4x** (`cbf13c6`) — the UW MCP flow-alerts endpoint
  caps responses at 50 with no pagination; a single unfiltered call surfaced
  only ~5 tickers/scan. Discovery now makes one pre-filtered call per
  issue-type slice (Index/ETF and Common Stock/ADR), reliably filling all 20
  ticker slots with indexes represented.

### Risk & positions

- **Activate the risk gates** (`78ecda5`) — `record_fill`/`record_pnl` were
  never called, so the position cap, sector limit, and daily-loss kill-switch
  could never engage. One shared `RiskEngine` now reads live position count
  from `PositionStore` and receives realized P&L from the exit loop.
- **Fix restart auto-liquidation** (`78ecda5`) — reconciled positions stored
  Robinhood's per-contract `average_price` as the per-share entry premium
  (100x too big), so every position hit a false ~-99% stop-loss on the first
  tick after a container restart.
- **Track real order quantity** (`78ecda5`) — positions recorded the static
  `ORDER_QUANTITY` instead of the actual sized quantity from
  `MAX_TRADE_SPEND`, so exits would have sold the wrong number of contracts.

### Reliability

- **Fix frozen loops after first scan** (`78ecda5`) — per-run child telemetry
  loggers closed the shared telemetry file on garbage collection ("I/O
  operation on closed file"), silently killing all scans and polls ~1 minute
  into each trading day.
- **Log swallowed pipeline errors; ordered alert dedup; NYSE holiday calendar
  (2026–27); prune proposal/notifier memory growth** (`78ecda5`).
- **Fix backtest replay entering zero trades** (`048f4f5`) — GEX detection
  didn't anchor to `pipeline_date`, so contract DTEs were computed from
  wall-clock now and every historical contract looked expired. Also fixed the
  stale pre-rename tool names in the backtest data store and test fixtures;
  full suite green (334 passed).

### Config & dashboard

- **Dashboard Settings tab / runtime config** (`cbf13c6`) — seed tickers,
  discovery premium/cap, flow premium, stop-loss, and DTE floor editable at
  `/api/config`, validated, applied next cycle without restart, persisted to
  `logs/live_config.json` across restarts.
- **Fix blank drawer on ticker-card click** (`9742627`) — the Market tab's
  cards pointed at the decisions API and crashed on the 404; they now open a
  dedicated GEX-snapshot drawer (price ruler, regime, walls, freshness).

### Deployment

- **Tailscale instead of Cloudflare tunnel** (`3453a1a`, `ee32f83`) — dashboard
  served via `tailscale serve`, private to the tailnet; optional
  `DASHBOARD_TOKEN` auth layer retained; deploy + Tailscale steps documented
  in the README.

### Known issues / next

- See `TODO.md` — headline item: backtest against real captured UW history to
  measure strategy profitability; also selector window in Settings, holiday
  calendar refresh before 2028, kill-switch persistence, sector map.
