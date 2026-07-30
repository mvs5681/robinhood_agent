# Changelog

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
