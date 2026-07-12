# Agent Decision Logic

How the agent discovers tickers, decides to buy options, and decides to sell them.

---

## Ticker Discovery

There is no static watchlist. Every hour the scanner asks: *which tickers are seeing unusual options premium right now?*

**How it works (runs once per hour during market hours):**

1. Fetches the latest 200 flow alerts from Unusual Whales (`get_flow_alerts`).
2. Groups alerts by ticker and sums their total premium.
3. Ranks tickers by total premium descending.
4. Keeps tickers above a minimum threshold (default **$250K**), capped at **20 tickers** per cycle.
5. Merges with any manually seeded tickers from `TICKERS` env var — seed tickers always appear first regardless of their premium rank.

```
Flow alerts (200)
  → group by ticker, sum premium
  → rank descending
  → drop below $250K threshold
  → cap at top 20
  → merge with TICKERS seed list
  → GEX scan universe for this hour
```

For each discovered ticker, the scanner fetches 7 data points in parallel:

| Data | UW Endpoint | Used for |
|---|---|---|
| Per-strike net GEX | `get_greek_exposure_by_strike` | GEX regime + walls |
| Darkpool prints | `get_dark_pool_trades` | Spot price + darkpool score |
| Net premium ticks | `get_flow_per_strike` | Flow pressure score |
| Options chain | `get_options_chain` | Contract selection |
| RSI | `get_extended_technical_indicator` | Technicals score |
| MACD | `get_extended_technical_indicator` | Technicals score |

Results are written into a shared in-memory cache. The flow watcher reads from this cache every 60 seconds — it does not re-fetch these endpoints on every tick.

Spot price for GEX detection is resolved in priority order:
1. Most recent darkpool print price (most granular for equities)
2. `underlying_price` field on the flow alert (covers index tickers with no darkpool activity)

---

## Buy Decision — 6 Sequential Gates

All 6 must pass. Failure at any gate stops that ticker for the current cycle.

---

### Gate 1 · GEX Setup Detection

**What it looks for:** Whether market makers are positioned to suppress or amplify price movement.

Market makers delta-hedge their options books. When they are net long gamma (positive GEX), they buy dips and sell rallies — suppressing moves. When net short gamma (negative GEX), they do the opposite — amplifying moves. The agent trades with this force.

**How it's computed from per-strike GEX data:**

| Output | Formula |
|---|---|
| Regime ratio | `sum(all net_GEX) / sum(abs(net_GEX))` — ranges [-1, +1] |
| Confidence | `min(top-3 strikes as % of total GEX, abs(regime_ratio))` — measures both concentration and directional clarity |
| Regime | Ratio ≥ threshold → **POSITIVE**. Ratio ≤ negative threshold → **NEGATIVE**. Otherwise → **MIXED** |
| Flip point | Strike where net GEX crosses zero (linear interpolation) |
| Call wall | Highest positive net-GEX strike above spot (dealers most long gamma here) |
| Put wall | Most negative net-GEX strike below spot (dealers most short gamma here) |

**Direction and trade type:**

| Regime | Position | Direction | Setup type | Target |
|---|---|---|---|---|
| POSITIVE | Any | Call | Pin — price gravitates toward call wall | Call wall |
| NEGATIVE | Spot < flip point | Put | Momentum — bearish, dealers amplify downside | Put wall |
| NEGATIVE | Spot > flip point | Call | Momentum — bullish squeeze above flip | Call wall |
| MIXED | Any | None | No trade | — |

**Kill condition:** MIXED regime or confidence below minimum threshold → dropped immediately.

---

### Gate 2 · Blend Score

**What it looks for:** Multi-factor conviction that the GEX direction is supported by current market data.

Five sub-signals, each scored 0–1 (higher = better for a long position in the given direction). All equally weighted at 20%.

#### Market Tide (20%)
**Source:** `get_market_tide` — aggregate net call and put premium flow across the entire market, last 30 ticks.

```
net_bias = (call_sum + put_sum) / (|call_sum| + |put_sum|)  →  [-1, +1]
call direction: score = (net_bias + 1) / 2
put  direction: score = (1 - net_bias) / 2
```

A call setup scores high when the broad market is call-heavy. A put setup scores high when the market is put-heavy.

#### Darkpool (20%)
**Source:** `get_dark_pool_trades` — last 100 institutional darkpool prints.

```
score = min(total_non_canceled_premium / $5M cap, 1)
```

Direction-agnostic — heavy institutional darkpool activity supports the thesis regardless of side. Large institutions rarely telegraph their direction via darkpool prints, but volume confirms there is conviction.

#### Flow Pressure (20%)
**Source:** `get_flow_alerts` + `get_flow_per_strike`

Two sub-signals combined:
- **Alert directional fraction (60%):** What percentage of this ticker's recent flow alerts match the candidate direction (call or put)?
- **Net-premium tick momentum (40%):** Of the last 20 net-premium ticks, how many show premium trending in the right direction?

```
score = 0.6 × (matching_alerts / total_alerts) + 0.4 × (confirming_ticks / 20)
```

#### IV Cost (20%)
**Source:** Interpolated IV term structure at 30 DTE.

Options are expensive relative to their history when IV percentile is high. The agent avoids entering when options are expensive.

```
score = 1 - (IV_percentile_at_30DTE / 100)
```

Score is 1 when options are historically cheap, 0 when at the top of their range.

#### Technicals (20%)
**Source:** RSI and MACD daily data.

RSI mapped to [0, 1] by zone:

| RSI | Call score | Put score |
|---|---|---|
| < 30 (extreme oversold) | 0.3 | 0.1 |
| 30–50 (oversold to neutral) | 0.9 | 0.2 |
| 50–60 (mild momentum) | 0.7 | 0.4 |
| 60–70 (approaching overbought) | 0.4 | 0.7 |
| > 70 (overbought) | 0.1 | 0.9 |

MACD: bullish crossover (MACD > signal line) → 0.8 for calls, 0.2 for puts. Bearish crossover → inverse.

**Final composite** = weighted average of all 5. Candidates are ranked by composite; the highest scoring tickers are processed first.

---

### Gate 3 · Flow Trigger (Whale Print Confirmation)

**What it looks for:** Evidence that a real large-money options order was placed in the market recently — not just modeled conviction, but actual execution.

Checks the live flow alerts for:
- Ticker matches
- Direction matches (call or put)
- Premium ≥ `FLOW_MIN_PREMIUM` (default $100K)
- Alert is within the last 4 hours

No qualifying print → `skipped_no_flow`. The GEX setup and score can be strong, but without a whale confirming the move with real money, no trade is placed.

---

### Gate 4 · Contract Selection

**What it looks for:** The best liquid contract to express the thesis.

Filters the full options chain:

| Filter | Default |
|---|---|
| Direction | Must match call or put from GEX setup |
| DTE | 21–30 days (sweet spot between theta decay and leverage) |
| Delta | 0.30–0.45 (slightly out of the money — defined risk, real leverage) |

Among contracts that survive, sorted by:
1. Strike closest to the GEX target level (call wall or put wall)
2. Tightest bid-ask spread percentage (liquidity)
3. Highest open interest (deeper market)

No eligible contract → `not_executable`.

---

### Gate 5 · Risk Engine

Hard portfolio-level guards applied before any order is sent:

| Guard | What it checks |
|---|---|
| **Kill switch** | If daily P&L loss exceeds a % of account NAV, all new entries are blocked for the rest of the session. Permanent once tripped. |
| **Max concurrent positions** | Total open positions must be below the cap. |
| **Premium cap** | `contract.mid × 100` (cost per contract) must not exceed `max_premium_per_trade`. |
| **Sector concentration** | No more than N open trades in the same GICS sector simultaneously. |

Any failed guard → `skipped_risk_gate`.

---

### Gate 6 · Human Approval (rh_approval mode)

A Telegram message is sent with the full trade proposal — ticker, direction, GEX regime, strike, expiry, delta, limit price, and blend score. Two buttons: **Approve** or **Reject**.

On approve → limit buy order placed at the option mid price. Quantity is dynamic:

```
contracts = floor(MAX_TRADE_SPEND / (mid_price × 100))
```

Capped at `MAX_CONTRACTS`. Falls back to `ORDER_QUANTITY` if `MAX_TRADE_SPEND` is not set.

---

## Sell Decision — Automatic, No Approval

The exit loop runs every 60 seconds during market hours. For each open position it fetches the current underlying price (`get_equity_quotes`) and current option mid price (`get_option_quotes`), then checks three conditions in priority order.

| # | Condition | Trigger | Why |
|---|---|---|---|
| 1 | **Profit target** | Underlying spot ≥ GEX gamma wall stored at entry | The market maker force that drove the move has been reached. This is the structural target the trade was built around. |
| 2 | **Stop loss** | Option premium dropped ≥ 35% from entry (configurable via `STOP_LOSS_PCT`) | Cuts losses before the position becomes worthless. Limit price set 5% below mid to improve fill probability under stress. |
| 3 | **DTE stop** | Days to expiry ≤ 7 (configurable via `DTE_FLOOR`) | Avoids holding through the final week where theta decay accelerates sharply and the option can lose most of its value even if the underlying moves favourably. |

If a sell order fails (e.g. market closed, API error), the position stays in the store and retries on the next 60-second tick.

On any exit, a Telegram notification is sent with the reason and realised P&L percentage.

---

## Configuration Reference

```env
# Ticker discovery
DISCOVERY_MIN_PREMIUM=250000   # minimum total premium for a ticker to be scanned
MAX_DISCOVERED_TICKERS=20      # cap on tickers per hourly scan cycle
TICKERS=AAPL,SPY               # optional seed list — always scanned regardless of premium

# Trade entry
FLOW_MIN_PREMIUM=100000        # minimum single whale print to confirm a setup
EXECUTION_MODE=rh_approval     # propose_only | rh_approval | autonomous

# Position sizing
MAX_TRADE_SPEND=500            # max dollars per trade; drives dynamic quantity
MAX_CONTRACTS=20               # hard cap on contracts per trade
ORDER_QUANTITY=1               # fallback when MAX_TRADE_SPEND is not set

# Trade exit (automatic)
STOP_LOSS_PCT=0.35             # close if premium drops 35% from entry
DTE_FLOOR=7                    # close if ≤7 days to expiration
```
