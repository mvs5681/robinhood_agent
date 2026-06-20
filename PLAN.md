# GEX-Anchored Options Trading Agent — Implementation Plan

## Pre-flight note on endpoint verification

The UW OpenAPI spec was fetched via summarizing proxy (`WebFetch`), so the raw YAML was not saved locally. **Phase 1 begins by downloading the raw spec** (`curl https://api.unusualwhales.com/api/openapi -o api_spec.yaml`) and validating every path/param used below against it. Several paths in the prompt differ from the summarized spec (e.g., `/api/flow-alerts` vs `/api/option-trades/flow-alerts`, flat vs `{ticker}`-prefixed paths). The canonical source wins; this plan uses the prompt's paths as-written but flags all ambiguous ones in the Open Questions section.

---

## Directory / package structure

```
robinhood_agent/
├── pyproject.toml
├── .env.example
├── PLAN.md
├── api_spec.yaml                    # downloaded in Phase 1, gitignored or committed read-only
├── src/
│   └── trader/
│       ├── __init__.py
│       ├── uw/                      # Phase 1 — Unusual Whales client
│       │   ├── __init__.py
│       │   ├── client.py            # rate-aware async httpx client
│       │   ├── endpoints.py         # one typed method per whitelisted endpoint
│       │   ├── rate_limiter.py      # per-minute + daily quota tracker
│       │   └── schemas.py           # pydantic response models (raw UW shapes)
│       ├── gex/                     # Phase 2 — GEX setup detector
│       │   ├── __init__.py
│       │   ├── detector.py
│       │   └── schemas.py           # GEXWall, GEXSetup, GEXRegime
│       ├── scoring/                 # Phase 3 — multi-signal blend scorer
│       │   ├── __init__.py
│       │   ├── scorer.py
│       │   ├── features.py          # one function per signal feature
│       │   └── schemas.py           # BlendScores, CandidateSignal
│       ├── flow/                    # Phase 4 — flow trigger
│       │   ├── __init__.py
│       │   └── trigger.py
│       ├── contracts/               # Phase 5 — contract selector
│       │   ├── __init__.py
│       │   └── selector.py
│       ├── risk/                    # Phase 6a — hard risk gates
│       │   ├── __init__.py
│       │   ├── engine.py
│       │   └── schemas.py           # RiskParams, RiskVerdict
│       ├── exits/                   # Phase 6b — exit monitor
│       │   ├── __init__.py
│       │   ├── monitor.py
│       │   └── schemas.py           # ExitSignal, ExitReason
│       ├── executor/                # Phase 7 — order executor
│       │   ├── __init__.py
│       │   ├── executor.py
│       │   └── schemas.py           # OrderRequest, OrderResult, ExecutionMode
│       ├── backtest/                # Phase 8 — replay harness
│       │   ├── __init__.py
│       │   ├── harness.py
│       │   ├── policy.py            # PolicyAdapter ABC
│       │   ├── data_store.py        # historical UW fixture loader
│       │   └── metrics.py
│       └── telemetry/               # Phase 9 — structured logging
│           ├── __init__.py
│           └── logger.py
├── tests/
│   ├── conftest.py
│   ├── fixtures/                    # canned UW API responses (JSON)
│   │   ├── gex_positive.json
│   │   ├── gex_negative.json
│   │   ├── flow_alerts.json
│   │   └── ...
│   ├── unit/
│   │   ├── test_rate_limiter.py
│   │   ├── test_gex_detector.py
│   │   ├── test_scorer.py
│   │   ├── test_flow_trigger.py
│   │   ├── test_contract_selector.py
│   │   ├── test_risk_engine.py
│   │   └── test_exit_monitor.py
│   └── integration/
│       ├── test_uw_client_live.py   # skipped unless UW_API_TOKEN set
│       └── test_backtest_harness.py
└── scripts/
    ├── fetch_spec.sh                # downloads api_spec.yaml
    └── run_backtest.py              # CLI entry for backtest
```

---

## Key pydantic schemas

### UW raw layer (`uw/schemas.py`)

```python
class FlowAlert(BaseModel):
    ticker: str
    expiry: date
    strike: float
    is_call: bool
    total_premium: Decimal
    total_size: int
    volume: int
    open_interest: int
    alert_rule: str
    trade_count: int

class GEXByStrike(BaseModel):
    strike: float
    call_gex: float
    put_gex: float
    net_gex: float          # call_gex + put_gex (sign convention: TBD from spec)

class MarketTide(BaseModel):
    timestamp: datetime
    net_call_premium: Decimal
    net_put_premium: Decimal
    net_volume: int

class DarkpoolPrint(BaseModel):
    price: float
    size: int
    premium: Decimal
    executed_at: datetime
    market_center: str

class NetPremTick(BaseModel):
    timestamp: datetime
    net_premium_delta: Decimal

class OptionContract(BaseModel):
    ticker: str
    expiry: date
    strike: float
    is_call: bool
    bid: float
    ask: float
    mid: float
    open_interest: int
    volume: int
    implied_volatility: float
    delta: float
    gamma: float
    theta: float
    vega: float
```

### GEX layer (`gex/schemas.py`)

```python
class GEXRegime(str, Enum):
    POSITIVE = "positive"   # dealers suppress → pin/mean-revert
    NEGATIVE = "negative"   # dealers amplify → momentum/squeeze
    MIXED    = "mixed"      # no clean structure → skip

class GEXWall(BaseModel):
    strike: float
    net_gex: float
    distance_pct: float     # abs((wall - spot) / spot)
    side: Literal["call_wall", "put_wall", "flip_point"]

class GEXSetup(BaseModel):
    ticker: str
    as_of: datetime
    spot_price: float
    regime: GEXRegime
    flip_point: float | None        # strike where net GEX crosses zero
    nearest_call_wall: GEXWall | None
    nearest_put_wall: GEXWall | None
    target_level: float | None      # nearest large wall = exit target
    candidate_direction: Literal["call", "put", "none"]
    setup_type: Literal["pin", "squeeze", "momentum", "none"]
    structure_confidence: float     # 0–1; drives whether candidate survives to scorer
    raw_gex_by_strike: list[GEXByStrike]
```

### Scoring layer (`scoring/schemas.py`)

```python
class BlendScores(BaseModel):
    market_tide: float      # 0–1; direction alignment with net market flow
    darkpool: float         # 0–1; recent dark-pool accumulation pressure
    flow_pressure: float    # 0–1; net premium + flow-recent on this ticker
    iv_cost: float          # 0–1; 1 = cheap vol (low IV rank), 0 = expensive
    technicals: float       # 0–1; RSI/MACD/BBANDS timing alignment
    composite: float        # weighted sum (weights from RiskParams.blend_weights)

class CandidateSignal(BaseModel):
    ticker: str
    as_of: datetime
    gex_setup: GEXSetup
    blend_scores: BlendScores
    rank: int                       # 1 = top
    flow_confirmed: bool
    flow_trigger: FlowAlert | None
    selected_contract: OptionContract | None
    execution_status: Literal[
        "proposed",
        "pending_approval",
        "executed",
        "skipped_no_flow",
        "skipped_risk_gate",
        "skipped_no_structure",
        "not_executable_long_only",
        "rejected_by_approval",
    ]
    skip_reason: str | None
```

### Risk layer (`risk/schemas.py`)

```python
class RiskParams(BaseModel):
    max_concurrent_positions: int
    max_premium_per_trade: Decimal
    daily_loss_kill_pct: float      # % of account NAV
    max_sector_concentration: int   # max open trades in same sector
    blend_weights: dict[str, float] # keys: market_tide, darkpool, flow_pressure, iv_cost, technicals
    delta_target_min: float
    delta_target_max: float
    dte_min: int
    dte_max: int

class RiskVerdict(BaseModel):
    approved: bool
    reasons: list[str]              # populated when approved=False
```

### Exit layer (`exits/schemas.py`)

```python
class ExitReason(str, Enum):
    PROFIT_TARGET  = "profit_target"    # underlying reached gamma wall
    STOP_LOSS      = "stop_loss"        # premium lost > threshold
    DTE_STOP       = "dte_stop"         # DTE fell below minimum
    MANUAL         = "manual"

class ExitSignal(BaseModel):
    position_id: str
    ticker: str
    contract: OptionContract
    reason: ExitReason
    current_premium: float
    entry_premium: float
    pnl_pct: float
    dte_remaining: int
    as_of: datetime
```

### Executor layer (`executor/schemas.py`)

```python
class ExecutionMode(str, Enum):
    PROPOSE_ONLY  = "propose_only"
    RH_APPROVAL   = "rh_approval"
    AUTONOMOUS    = "autonomous"

class OrderRequest(BaseModel):
    candidate: CandidateSignal
    action: Literal["buy_to_open", "sell_to_close"]
    quantity: int
    limit_price: float | None       # None = market (avoid for options)
    mode: ExecutionMode

class OrderResult(BaseModel):
    request: OrderRequest
    placed: bool
    order_id: str | None
    rejection_reason: str | None
    timestamp: datetime
```

---

## Module public interfaces

### `uw.client.UWClient`

```python
class UWClient:
    def __init__(self, token: str, client_api_id: str = "100001"): ...

    # rate-limit-aware transport; all methods are async
    async def get_flow_alerts(self, **filters) -> list[FlowAlert]: ...
    async def get_spot_gex_by_strike(self, ticker: str, date: date | None) -> list[GEXByStrike]: ...
    async def get_static_gex_by_strike(self, ticker: str) -> list[GEXByStrike]: ...
    async def get_option_contracts_screener(self, **filters) -> list[OptionContract]: ...
    async def get_darkpool(self, ticker: str, date: date | None) -> list[DarkpoolPrint]: ...
    async def get_market_tide(self) -> list[MarketTide]: ...
    async def get_net_prem_ticks(self, ticker: str) -> list[NetPremTick]: ...
    async def get_flow_recent(self, ticker: str) -> list[FlowAlert]: ...
    async def get_interpolated_iv(self, ticker: str) -> dict: ...
    async def get_technical_indicator(self, ticker: str, function: str) -> dict: ...
    async def get_option_contracts(self, ticker: str, **filters) -> list[OptionContract]: ...
    async def get_greeks(self, ticker: str, expiry: date, strike: float) -> OptionContract: ...

    @property
    def quota_status(self) -> QuotaStatus: ...  # current daily/per-min usage
```

### `uw.rate_limiter.RateLimiter`

```python
class RateLimiter:
    def update_from_headers(self, headers: dict) -> None: ...
    async def acquire(self) -> None: ...          # blocks if per-min exhausted
    @property
    def daily_remaining(self) -> int: ...
    @property
    def per_minute_remaining(self) -> int: ...
```

### `gex.detector.GEXDetector`

```python
class GEXDetector:
    def __init__(self, params: GEXDetectorParams): ...

    async def detect(self, ticker: str, client: UWClient) -> GEXSetup: ...
    # Internally: fetches spot + static GEX, sums net GEX by strike,
    # finds flip point, identifies largest walls, classifies regime.
    # Returns GEXSetup with regime=MIXED and candidate_direction="none"
    # when structure_confidence < params.min_confidence_threshold.
```

### `scoring.scorer.BlendScorer`

```python
class BlendScorer:
    def __init__(self, weights: dict[str, float]): ...

    async def score(self, setup: GEXSetup, client: UWClient) -> CandidateSignal: ...
    async def rank(self, setups: list[GEXSetup], client: UWClient) -> list[CandidateSignal]: ...
```

### `flow.trigger.FlowTrigger`

```python
class FlowTrigger:
    def __init__(self, min_premium: Decimal, lookback_hours: int): ...

    async def check(
        self,
        candidate: CandidateSignal,
        client: UWClient,
    ) -> CandidateSignal:
        # Returns candidate with flow_confirmed=True and flow_trigger set
        # if a same-direction whale print exists within lookback window.
        ...
```

### `contracts.selector.ContractSelector`

```python
class ContractSelector:
    def __init__(self, params: RiskParams): ...

    async def select(
        self,
        candidate: CandidateSignal,
        client: UWClient,
    ) -> OptionContract | None:
        # Filters by direction, DTE band, delta range;
        # anchors to GEX target_level strike;
        # picks best liquidity (bid/ask spread, OI);
        # returns None if no contract meets criteria → sets not_executable_long_only
        # when setup requires premium selling.
        ...
```

### `risk.engine.RiskEngine`

```python
class RiskEngine:
    def __init__(self, params: RiskParams, portfolio: PortfolioState): ...

    def check(self, candidate: CandidateSignal) -> RiskVerdict: ...
    # Checks: max concurrent positions, max premium, daily loss kill-switch,
    # sector concentration. All synchronous — no I/O, no LLM.
    # portfolio state injected; engine never mutates it.

    def record_fill(self, result: OrderResult) -> None: ...
    def record_pnl(self, pnl: Decimal) -> None: ...
    @property
    def kill_switch_active(self) -> bool: ...
```

### `exits.monitor.ExitMonitor`

```python
class ExitMonitor:
    def __init__(self, stop_loss_pct: float, dte_floor: int): ...

    def evaluate(
        self,
        position: Position,
        current_price: float,
        current_premium: float,
        dte: int,
    ) -> ExitSignal | None:
        # Checks in order: profit target (price >= wall), stop loss (premium drop),
        # DTE floor. Returns first triggered reason, else None.
        # Fully synchronous — called by executor's monitoring loop.
```

### `executor.executor.Executor`

```python
class Executor:
    def __init__(
        self,
        mode: ExecutionMode,
        risk_engine: RiskEngine,
        exit_monitor: ExitMonitor,
        rh_mcp_client,           # injected; type TBD pending RH MCP schema
        telemetry: TelemetryLogger,
    ): ...

    async def execute(self, candidate: CandidateSignal) -> OrderResult: ...
    async def monitor_positions(self, client: UWClient) -> list[ExitSignal]: ...
    # propose_only: logs + returns OrderResult(placed=False)
    # rh_approval: calls RH MCP preview-approval flow, waits for response
    # autonomous: places directly within risk limits
```

### `backtest.policy.PolicyAdapter` (ABC)

```python
class PolicyAdapter(ABC):
    @abstractmethod
    async def generate_candidates(
        self,
        tickers: list[str],
        data: BacktestDataSlice,     # historical UW responses for this timestamp
    ) -> list[GEXSetup]: ...

    @abstractmethod
    async def score_and_rank(
        self,
        setups: list[GEXSetup],
        data: BacktestDataSlice,
    ) -> list[CandidateSignal]: ...

    @abstractmethod
    async def should_enter(self, candidate: CandidateSignal) -> bool: ...

    @abstractmethod
    def should_exit(
        self,
        position: BacktestPosition,
        data: BacktestDataSlice,
    ) -> ExitSignal | None: ...
```

Live `LivePolicy` wraps the real pipeline. `BacktestPolicy` swaps in historical data slices for every UW API call — the scoring functions themselves run unchanged.

### `backtest.harness.BacktestHarness`

```python
class BacktestHarness:
    def __init__(
        self,
        policy: PolicyAdapter,
        data_store: DataStore,          # loads historical UW fixture data
        risk_params: RiskParams,
        start_date: date,
        end_date: date,
    ): ...

    async def run(self) -> BacktestResult: ...
    # Temporal replay: for each trading day in [start, end]:
    #   1. Load data slice (pre-fetched UW responses)
    #   2. Run policy → candidates → entry decisions
    #   3. Track positions; evaluate exits on subsequent days
    #   4. Record all decisions in TelemetryLogger
    # Returns BacktestResult with metrics sliced by regime + setup_type.
```

---

## Phase breakdown

### Phase 1 — UW Client

**Deliverables:**
- Download + commit `api_spec.yaml`; reconcile all whitelisted endpoint paths and params against it
- `uw/client.py`, `uw/rate_limiter.py`, `uw/endpoints.py`, `uw/schemas.py`
- `pyproject.toml` with deps (`httpx`, `pydantic`, `python-dotenv`, `pytest`, `pytest-asyncio`, `respx`)
- `.env.example`
- `tests/unit/test_rate_limiter.py` (mocked headers), `tests/integration/test_uw_client_live.py` (skipped w/o token)

**Acceptance criteria:**
- Every whitelisted endpoint callable; params match spec
- Rate limiter correctly blocks when per-min remaining < 5 and resumes after reset header
- Daily quota warnings logged at 50% / 80% thresholds
- 401/429/403 each produce a distinct, typed exception
- `pytest -m "not live"` passes with no real token

---

### Phase 2 — GEX Setup Detector

**Deliverables:**
- `gex/detector.py`, `gex/schemas.py`
- `tests/fixtures/gex_positive.json`, `gex_negative.json`, `gex_mixed.json`
- `tests/unit/test_gex_detector.py`

**Acceptance criteria:**
- Given a positive-GEX fixture (large net positive at OTM call strikes): regime=POSITIVE, correct flip point, call wall identified as target, direction="call", setup_type="pin"
- Given a negative-GEX fixture: regime=NEGATIVE, direction="momentum" with put wall as target, direction depends on flip point vs. spot
- Given noisy/flat fixture: regime=MIXED, candidate_direction="none", no trade
- Flip point location correct to ±1 strike width
- structure_confidence reflects wall magnitude and clarity

---

### Phase 3 — Blend Scorer

**Deliverables:**
- `scoring/scorer.py`, `scoring/features.py`, `scoring/schemas.py`
- `tests/unit/test_scorer.py`

**Acceptance criteria:**
- Each of the 5 features is an independently testable function with a fixture
- Weights configurable via `RiskParams.blend_weights`; must sum to 1.0 (validated)
- Composite score monotonically reflects component scores at extreme inputs
- `rank()` returns list sorted by composite desc, ranks assigned 1..N

---

### Phase 4 — Flow Trigger

**Deliverables:**
- `flow/trigger.py`
- `tests/fixtures/flow_alerts.json`
- `tests/unit/test_flow_trigger.py`

**Acceptance criteria:**
- Confirms only when alert is same ticker, same direction (call/put), within lookback window, premium above min_premium
- Multiple matching alerts → picks highest premium print, populates `flow_trigger`
- No matching alerts → `flow_confirmed=False`, status `skipped_no_flow`

---

### Phase 5 — Contract Selector

**Deliverables:**
- `contracts/selector.py`
- `tests/unit/test_contract_selector.py`

**Acceptance criteria:**
- Selects contract within delta/DTE band (open question: exact values — see below)
- Anchors strike selection to `GEXSetup.target_level` (picks closest in-band strike to wall)
- Ranks survivors by liquidity (mid spread pct, then OI)
- Returns `None` and sets status `not_executable_long_only` for setups requiring net short structures

---

### Phase 6 — Risk Engine + Exit Monitor

**Deliverables:**
- `risk/engine.py`, `risk/schemas.py`
- `exits/monitor.py`, `exits/schemas.py`
- `tests/unit/test_risk_engine.py`, `tests/unit/test_exit_monitor.py`

**Acceptance criteria:**
- Risk engine: each gate (position count, premium cap, kill-switch, sector) has an isolated unit test that trips it
- Kill switch: once active, `check()` returns `approved=False` for every subsequent call in that session, regardless of params
- Exit monitor: profit target fires when `current_price >= target_level`; stop fires at correct premium threshold; DTE stop fires when `dte <= dte_floor`; only first-triggered reason returned
- No network I/O in either module — fully synchronous and testable with pure Python

---

### Phase 7 — Executor

**Deliverables:**
- `executor/executor.py`, `executor/schemas.py`
- End-to-end `propose_only` integration test (no real RH call)
- Stub for `rh_approval` and `autonomous` modes (interface defined, body raises `NotImplementedError` until RH MCP schema is confirmed)

**Acceptance criteria:**
- `propose_only` logs `OrderResult(placed=False)` with full candidate detail; returns immediately
- Every execution attempt (placed or not) emits a telemetry event with: ticker, contract, scores, mode, verdict, timestamp
- `ExecutionMode` can only be promoted via config — no code path allows the agent to self-promote
- Long-only constraint: executor refuses to send any order that is a sell-to-open; this is enforced in `check_order_type()` called before any mode's dispatch

---

### Phase 8 — Backtest Harness

**Deliverables:**
- `backtest/policy.py` (ABC + `LivePolicy` wrapper + `BacktestPolicy`)
- `backtest/harness.py`, `backtest/data_store.py`, `backtest/metrics.py`
- `scripts/run_backtest.py` (CLI)
- `tests/integration/test_backtest_harness.py` (runs on local fixtures, no live API)

**Acceptance criteria:**
- Temporal split enforced: harness constructor rejects `start_date` within train window
- `BacktestPolicy` calls no live UW endpoints; all data sourced from `DataStore` (pre-fetched JSON)
- `BacktestResult.metrics` includes: win rate, avg P&L, max drawdown, sliced by `GEXRegime` × `setup_type`
- Telemetry records every candidate considered (not just entries) with reason for rejection
- `LivePolicy` and `BacktestPolicy` both satisfy the `PolicyAdapter` ABC; swap is a one-line config change

---

### Phase 9 — Telemetry

**Deliverables:**
- `telemetry/logger.py`
- Structured JSON to stdout (and optionally a log file)

**Acceptance criteria:**
- Every pipeline stage emits an event: `uw_fetch`, `gex_setup`, `blend_score`, `flow_check`, `contract_select`, `risk_check`, `order_attempt`, `exit_signal`
- Each event includes: `timestamp`, `ticker`, `stage`, `result`, `reason` (for skips/rejects), `duration_ms`
- Rejected candidates logged with full score breakdown — not just the winner
- Log level configurable via `LOG_LEVEL` env var; sensitive fields (account IDs) masked

---

## Backtest adapter: live vs. replay swap

```
live run:
  UWClient (real httpx) → LivePolicy.generate_candidates() → LivePolicy.score_and_rank()
  ↓
  Executor → RH MCP

backtest run:
  DataStore.load_slice(date) → BacktestPolicy.generate_candidates() → BacktestPolicy.score_and_rank()
  ↓
  Executor(mode=propose_only) → BacktestResult accumulator
```

`BacktestPolicy.__init__` takes a `LivePolicy` instance and a `DataStore`. It wraps every UW call with a lookup into the pre-fetched historical slice — the scoring logic (GEXDetector, BlendScorer, FlowTrigger, ContractSelector) runs **identical code** in both paths. This means a backtest regression on a known fixture directly validates the live pipeline.

Pre-fetching historical data: a `scripts/prefetch_history.py` script will call the UW API with date params over a window and save responses as structured JSON under `tests/fixtures/history/YYYY-MM-DD/`. This runs once and the data is committed (or stored externally if large). The exact date-range support of each endpoint is a known unknown — see Open Questions.

---

## Open questions / assumptions that need your answer before coding

These are flagged rather than hardcoded. I'll use placeholder constants in code marked `# CONFIG: open question` until you decide.

### 1. Endpoint paths (blocking for Phase 1)

The prompt specifies paths like `/api/option-trades/flow-alerts` and `/api/stock/{ticker}/spot-exposures/strike`, but the summarized OpenAPI spec shows `/api/flow-alerts` and `/api/spot-exposures/strike` (no `{ticker}` prefix on some). **The raw spec download in Phase 1 will resolve this authoritatively.** I will not write any endpoint call until the spec is verified. Flagging now so you know a small number of path corrections may affect Phase 1 scope.

### 2. GEX regime classification threshold

What makes a GEX structure "clean"? My working assumption: compute total absolute net GEX across all strikes; if the top-3 strikes account for > 60% of it, the structure is clean. If no single wall is > $X in net GEX, it's mixed. **What is the dollar threshold for a "meaningful" wall?** (This is market-cap and contract-unit dependent — SPY walls are in billions, single-stock walls are smaller.)

### 3. Blend weights

Starting assumption: equal weights (0.20 each across 5 signals). Do you want to start equal and tune, or do you have a prior? These should live in a config file, not code.

### 4. Delta target range

Assumption: **0.30–0.45 delta** for directional long options plays. Please confirm or adjust. Below 0.25 is lottery territory; above 0.50 starts competing with stock.

### 5. DTE band

Assumption: **21–45 DTE** at entry (avoids front-week theta decay, avoids LEAPS illiquidity). Please confirm.

### 6. Premium stop-loss %

**Decided:** close position if premium drops **-35%** from entry.

### 7. Profit target mechanics

Two options:
- (a) Close when **underlying price reaches target_level** (the gamma wall strike)
- (b) Close when **option premium reaches +X%** from entry

Assumption: (a) is the GEX-logical trigger; (b) is secondary. Do you want both active, or just (a)?

### 8. DTE stop (floor)

Assumption: close if **DTE ≤ 7** regardless of P&L. This avoids gamma/theta crisis in the final week. Confirm.

### 9. Max concurrent positions

Assumption: **3** open positions max. Adjust based on account size and desired concentration.

### 10. Max premium per trade

Assumption: **$500** per contract (cost basis). This is account-size dependent. What's your budget per trade?

### 11. Daily loss kill-switch threshold

Assumption: **-5% of account NAV** triggers kill-switch for the day. Confirm.

### 12. Sector concentration

Assumption: max **2** open trades in the same GICS sector. Do you want sector tracking, or is correlation-based capping out of scope for v1?

### 13. Flow confirmation window

How recent must the confirming whale print be? Assumption: **same trading day** (from market open to now). Or should it be last N hours (e.g., 4 hours)?

### 14. Min flow premium for trigger

What minimum total premium on a whale print counts as confirming flow? Assumption: **$100K** total premium on the alert. Please confirm.

### 15. Polling / run cadence

Does the agent run on a schedule (e.g., every 15 min during market hours) or is it event-driven? Assumption: **scheduled poll every 15 minutes**, 9:45 AM – 3:30 PM ET (avoiding open/close volatility).

### 16. Historical UW data availability for backtest

Do the whitelisted endpoints support a `date` query parameter for historical replay? The darkpool and flow-alerts endpoints appear to. GEX endpoints — unclear. If GEX history isn't available, the backtest harness will need to proxy it through whatever historical data the API exposes. **Do you have historical UW data already, or does the prefetch script need to collect it going forward?**

### 17. Robinhood MCP interface

The RH MCP schema/tools are not documented here. Specifically needed: what tool calls are available for placing options orders, checking portfolio state, and getting approval flows? Is options trading currently enabled on your agentic account? The executor in Phase 7 will be stubbed until this is answered.

### 18. Candidate universe / ticker list

Where does the initial ticker list come from? Options:
- (a) Top N results from `/api/screener/option-contracts` by activity volume
- (b) A fixed watchlist you provide
- (c) Both — screener + watchlist union

Assumption: (a), taking top 20 by option volume from the screener, then filtering by GEX structure.

### 19. GEX sign convention

Net GEX sign convention varies by source (some define dealer GEX as call_gex + put_gex where put_gex is negative; others flip). **The raw spec download will determine this.** Flagging because it directly affects which side of zero is "positive gamma" for dealer positioning.

---

## Dependencies (proposed)

```toml
[project]
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "respx>=0.21",      # mock httpx in unit tests
    "ruff",
    "mypy",
]
```

No new dependencies will be added without asking first.

---

## What I will NOT do without your explicit go-ahead

- Use any UW endpoint not on the whitelist
- Hardcode any of the open-question values above
- Enable `autonomous` execution mode (it will raise `NotImplementedError` until you explicitly unlock it post-backtest)
- Add premium-selling, spreads, or any short structure to the executor
- Use an LLM call anywhere in the trade-selection path

---

*Awaiting your approval and answers to open questions before starting Phase 1.*
