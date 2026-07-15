# Changelog

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
