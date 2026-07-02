# GEX Trading Agent

An event-driven options trading agent that uses Gamma Exposure (GEX) structure and unusual options flow to identify and execute high-conviction trades on Robinhood.

---

## How it works

The agent runs two async loops in a single process:

```
┌─────────────────────────────────────────────────────────────┐
│                        Every hour                           │
│  GEX Scanner  ──►  fetch slow data  ──►  detect setup       │
│                     (per ticker)          ──►  GEX Cache    │
└─────────────────────────────────────────────────────────────┘
                              │ cache ready
┌─────────────────────────────▼───────────────────────────────┐
│                       Every 60 seconds                      │
│  Flow Watcher  ──►  get_flow_alerts  ──►  new whale print?  │
│                                               │ yes          │
│                                         run pipeline         │
└─────────────────────────────────────────────────────────────┘
```

**GEX Scanner** fetches data that changes slowly (option chains, darkpool prints, IV surface, technicals) and runs the GEX detector per ticker. Results go into an in-memory cache.

**Flow Watcher** polls for new large options prints every 60 seconds. When a whale print arrives for a watched ticker that has a valid GEX setup, it triggers the scoring pipeline using cached data — no redundant API calls.

---

## Ticker selection

Each hour the scanner calls `get_flow_alerts` (Unusual Whales) to discover which tickers have significant options premium volume. Tickers above the `DISCOVERY_MIN_PREMIUM` threshold (default $500K) are ranked by total premium and scanned for GEX structure. No static watchlist is required.

You can optionally set `TICKERS` as a seed list — those tickers are always scanned regardless of flow activity that hour.

```
get_flow_alerts (UW MCP, every hour)
         │
         ▼
  Rank by total_premium → top 20 tickers above $500K threshold
         │
         + merge with TICKERS seed list (optional)
         │
         ▼
  ┌─────────────┐
  │  GEX Setup  │  Does the ticker have a positive or negative GEX regime?
  │  Detection  │  Is there a clear call/put wall with a flip point?
  └──────┬──────┘
         │ structure_confidence ≥ threshold
         ▼
  ┌─────────────┐
  │    Blend    │  5-signal composite score (0–1):
  │   Scorer    │  · Market tide (net options flow direction)
  └──────┬──────┘  · Darkpool  (institutional accumulation)
         │          · Flow pressure (directional alert fraction)
         │ composite ≥ min_composite  · IV cost (cheap vol = high score)
         ▼          · Technicals (RSI + MACD timing)
  ┌─────────────┐
  │    Flow     │  Is there a new whale print (≥ $100K premium)
  │   Trigger   │  in the right direction within the last 4 hours?
  └──────┬──────┘
         │ confirmed
         ▼
  ┌─────────────┐
  │  Contract   │  Select optimal strike and expiry:
  │  Selector   │  target delta 0.30–0.55, DTE 7–45 days,
  └──────┬──────┘  spread < 15%, liquidity filters
         │
         ▼
  ┌─────────────┐
  │  Risk Gate  │  Hard limits:
  │             │  · Max delta exposure per ticker/sector
  └──────┬──────┘  · Max concurrent positions
         │ approved
         ▼
     Proposal
```

---

## GEX regime explained

GEX (Gamma Exposure) measures how much market-makers need to hedge as price moves. The detector classifies each ticker into one of three regimes:

| Regime | What it means | Trade bias |
|--------|--------------|------------|
| **Negative** | Dealers amplify moves — momentum and squeezes occur | Follow the flow direction |
| **Positive** | Dealers suppress moves — price pins near walls | Fade extremes, target the wall |
| **Mixed** | No clean structure | Skip |

The flip point (where net GEX crosses zero) and the nearest call/put wall set the target level for the trade.

---

## Execution modes

| Mode | Behaviour |
|------|-----------|
| `rh_approval` | Agent proposes, you approve via **Telegram bot** or the web dashboard. Default. |
| `autonomous` | Agent executes immediately when all gates pass. |
| `propose_only` | Proposals are stored but never executed. Good for paper-trading. |

Switch modes by setting `EXECUTION_MODE` in `.env`.

---

## Approval flow (rh_approval mode)

```
Pipeline passes risk gate
         │
         ▼
  ProposalStore (30 min TTL)
         │
         ├──► Telegram message with [Approve] [Reject] buttons
         │         │
         │    tap Approve ──► Executor ──► Robinhood MCP ──► order placed
         │    tap Reject  ──► marked rejected, no order
         │
         └──► Web dashboard at :8080/  (same Approve/Reject UI)
```

---

## Setup

### 1. Install

```bash
pip install -e ".[live]"
```

### 2. Robinhood OAuth (one-time)

```bash
python scripts/auth_robinhood.py
# Opens browser → log in → tokens written to .env
```

### 3. Configure `.env`

```bash
cp .env.example .env
# Fill in: UW_API_TOKEN, TICKERS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

### 4. Telegram bot (for approvals)

1. Message `@BotFather` → `/newbot` → copy the token
2. Send your bot any message, then visit:  
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → copy `chat.id`
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`

### 5. Run

```bash
# Local
python scripts/run_live.py

# Docker
docker compose up --build

# Preview dashboard with fake data
python scripts/demo_dashboard.py
```

---

## Dashboard

Live at `:8080/` — auto-refreshes every 10 seconds.

| Panel | Shows |
|-------|-------|
| Overview | ok / skipped / error counts for the last hour |
| Pipeline funnel | Drop-off at each gate for today |
| Live proposals | Pending trades with Approve / Reject buttons |
| Recent events | Last 50 telemetry events, colour-coded |
| P&L timeline | Exit signal outcomes over time |

---

## Backtest

```bash
python -m trader.backtest.cli \
  --tickers AAPL SPY \
  --start 2026-01-02 \
  --end 2026-01-31 \
  --fixtures tests/fixtures/history
```

---

## Project structure

```
src/trader/
├── uw/          Unusual Whales MCP client + schemas
├── gex/         GEX regime detector
├── scoring/     5-signal blend scorer
├── flow/        Flow alert trigger (whale print gate)
├── contracts/   Strike / expiry selector
├── risk/        Hard risk gates + exit monitor
├── executor/    Robinhood order execution
├── graph/       LangGraph pipeline wiring
├── live/        Scanner, watcher, proposals, dashboard, Telegram bot
├── backtest/    Historical replay harness
└── telemetry/   Structured JSON event logger
```
