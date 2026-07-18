"""
LangGraph agent graph for the GEX-anchored options trading pipeline.

Phase 1 delivers the data-fetch subgraph:
  START → fetch_market_data → fetch_ticker_data → END

Later phases will insert nodes between fetch_ticker_data and END:
  → detect_gex → score → check_flow → select_contract → risk_gate → execute

Tool calls are made directly in node functions (deterministic, no LLM routing).
The LangGraph ToolNode is wired for future LLM-assisted nodes (e.g. a supervisor
that decides which tickers to scan) but is NOT in the trade-selection path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from datetime import time as dtime
from datetime import timezone
from decimal import Decimal
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from trader.contracts.selector import ContractSelector, SelectorParams
from trader.executor.executor import Executor
from trader.executor.schemas import ExecutionMode, OrderRequest, OrderResult
from trader.flow.trigger import FlowTrigger
from trader.gex.detector import GEXDetector
from trader.gex.schemas import GEXDetectorParams, GEXSetup
from trader.risk.engine import RiskEngine
from trader.risk.schemas import PortfolioState, RiskParams
from trader.scoring.scorer import BlendScorer, DEFAULT_WEIGHTS
from trader.telemetry.logger import TelemetryLogger
from trader.uw.validators import (
    parse_darkpool_prints,
    parse_flow_alerts,
    parse_interpolated_iv,
    parse_market_tide,
    parse_net_prem_ticks,
    parse_option_contracts,
    parse_spot_gex_by_strike,
    parse_technical_indicator,
)

from .state import TradingAgentState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------


async def fetch_market_data(
    state: TradingAgentState,
    tools: dict[str, BaseTool],
    tel: TelemetryLogger | None = None,
) -> dict[str, Any]:
    """
    Fetch market-wide data: market tide and global flow alerts.
    Single API call each — no per-ticker loop.
    """
    updates: dict[str, Any] = {}

    t0 = time.monotonic()
    try:
        raw_tide = await tools["get_market_tide"].ainvoke({})
        updates["market_tide"] = parse_market_tide(raw_tide)
        count = len(updates["market_tide"])
        logger.info("market_tide: %d ticks", count)
        if tel:
            tel.uw_fetch(endpoint="get_market_tide", record_count=count,
                         duration_ms=round((time.monotonic() - t0) * 1000, 1))
    except Exception as exc:
        logger.error("fetch_market_data market_tide failed: %s", exc)
        updates["errors"] = state.errors + [f"market_tide: {exc}"]
        if tel:
            tel.uw_fetch(endpoint="get_market_tide", record_count=0,
                         duration_ms=round((time.monotonic() - t0) * 1000, 1), error=str(exc))

    t0 = time.monotonic()
    try:
        raw_flow = await tools["get_flow_alerts"].ainvoke({"limit": 100})
        updates["flow_alerts"] = parse_flow_alerts(raw_flow)
        count = len(updates["flow_alerts"])
        logger.info("flow_alerts: %d alerts", count)
        if tel:
            tel.uw_fetch(endpoint="get_flow_alerts", record_count=count,
                         duration_ms=round((time.monotonic() - t0) * 1000, 1))
    except Exception as exc:
        logger.error("fetch_market_data flow_alerts failed: %s", exc)
        updates.setdefault("errors", state.errors)
        updates["errors"] = updates["errors"] + [f"flow_alerts: {exc}"]
        if tel:
            tel.uw_fetch(endpoint="get_flow_alerts", record_count=0,
                         duration_ms=round((time.monotonic() - t0) * 1000, 1), error=str(exc))

    return updates


async def fetch_ticker_data(
    state: TradingAgentState,
    tools: dict[str, BaseTool],
    tel: TelemetryLogger | None = None,
) -> dict[str, Any]:
    """
    Fetch per-ticker data in parallel: spot GEX, darkpool, net-prem ticks.
    Results are merged into keyed dicts on state.
    """
    spot_gex: dict = dict(state.spot_gex)
    darkpool: dict = dict(state.darkpool)
    net_prem_ticks: dict = dict(state.net_prem_ticks)
    option_contracts: dict = dict(state.option_contracts)
    interpolated_iv: dict = dict(state.interpolated_iv)
    technicals: dict = dict(state.technicals)
    errors: list[str] = list(state.errors)

    async def _fetch_one(ticker: str) -> None:
        for endpoint, invoke_kwargs, store, parser in [
            ("get_greek_exposure_by_strike", {"ticker": ticker}, spot_gex, parse_spot_gex_by_strike),
            ("get_dark_pool_trades",         {"ticker_symbol": ticker, "limit": 100}, darkpool, parse_darkpool_prints),
            ("get_flow_per_strike",          {"ticker": ticker}, net_prem_ticks, parse_net_prem_ticks),
            ("get_options_chain",            {"ticker": ticker, "limit": 50}, option_contracts, parse_option_contracts),
        ]:
            t0 = time.monotonic()
            try:
                raw = await tools[endpoint].ainvoke(invoke_kwargs)
                store[ticker] = parser(raw)
                count = len(store[ticker])
                logger.info("%s %s: %d records", ticker, endpoint, count)
                if tel:
                    tel.uw_fetch(ticker=ticker, endpoint=endpoint, record_count=count,
                                 duration_ms=round((time.monotonic() - t0) * 1000, 1))
            except Exception as exc:
                logger.error("%s %s failed: %s", ticker, endpoint, exc)
                errors.append(f"{ticker}.{endpoint}: {exc}")
                if tel:
                    tel.uw_fetch(ticker=ticker, endpoint=endpoint, record_count=0,
                                 duration_ms=round((time.monotonic() - t0) * 1000, 1),
                                 error=str(exc))

        ticker_technicals: dict = {}
        for fn in ("RSI", "MACD"):
            t0 = time.monotonic()
            try:
                raw_tech = await tools["get_extended_technical_indicator"].ainvoke(
                    {"ticker": ticker, "function": fn, "interval": "daily"}
                )
                ticker_technicals[fn] = parse_technical_indicator(raw_tech, fn)
                count = len(ticker_technicals[fn])
                logger.info("%s %s: %d points", ticker, fn, count)
                if tel:
                    tel.uw_fetch(ticker=ticker, endpoint=f"get_extended_technical_indicator/{fn}",
                                 record_count=count,
                                 duration_ms=round((time.monotonic() - t0) * 1000, 1))
            except Exception as exc:
                logger.error("%s %s failed: %s", ticker, fn, exc)
                errors.append(f"{ticker}.{fn}: {exc}")
                if tel:
                    tel.uw_fetch(ticker=ticker, endpoint=f"get_extended_technical_indicator/{fn}",
                                 record_count=0,
                                 duration_ms=round((time.monotonic() - t0) * 1000, 1),
                                 error=str(exc))
        technicals[ticker] = ticker_technicals

    await asyncio.gather(*[_fetch_one(t) for t in state.tickers])

    return {
        "spot_gex": spot_gex,
        "darkpool": darkpool,
        "net_prem_ticks": net_prem_ticks,
        "option_contracts": option_contracts,
        "interpolated_iv": interpolated_iv,
        "technicals": technicals,
        "errors": errors,
    }


def detect_gex(
    state: TradingAgentState,
    detector: GEXDetector,
    tel: TelemetryLogger | None = None,
) -> dict[str, Any]:
    """
    Phase 2 node: run GEXDetector over every ticker's spot_gex data.

    Spot price is resolved from existing state in priority order:
      1. flow_alerts[ticker].underlying_price (most timely)
      2. darkpool[ticker] most recent print price
      3. Skip ticker if neither is available
    """
    spot_lookup: dict[str, Decimal] = {}

    for alert in state.flow_alerts:
        if alert.underlying_price and alert.ticker not in spot_lookup:
            spot_lookup[alert.ticker] = alert.underlying_price

    for ticker, prints in state.darkpool.items():
        if ticker not in spot_lookup and prints:
            latest = max(prints, key=lambda p: p.executed_at)
            spot_lookup[ticker] = latest.price

    gex_setups: dict[str, GEXSetup] = {}
    errors: list[str] = list(state.errors)

    # Anchor GEXSetup.as_of to pipeline_date in backtest replay — the contract
    # selector derives DTE from it, so wall-clock now() would make historical
    # contracts look expired
    detect_as_of: datetime | None = None
    if state.pipeline_date is not None:
        detect_as_of = datetime.combine(state.pipeline_date, dtime(16, 0), tzinfo=timezone.utc)

    for ticker in state.tickers:
        t0 = time.monotonic()
        gex_data = state.spot_gex.get(ticker, [])
        if not gex_data:
            logger.warning("%s: no GEX data, skipping detector", ticker)
            if tel:
                tel.gex_setup(ticker=ticker, regime="unknown", direction="none",
                               setup_type="none", confidence=0.0, flip_point=None,
                               target_level=None,
                               duration_ms=round((time.monotonic() - t0) * 1000, 1),
                               skipped=True, reason="no GEX data")
            continue

        spot = spot_lookup.get(ticker)
        if spot is None:
            logger.warning("%s: no spot price available, skipping detector", ticker)
            errors.append(f"{ticker}.detect_gex: no spot price")
            if tel:
                tel.gex_setup(ticker=ticker, regime="unknown", direction="none",
                               setup_type="none", confidence=0.0, flip_point=None,
                               target_level=None,
                               duration_ms=round((time.monotonic() - t0) * 1000, 1),
                               skipped=True, reason="no spot price")
            continue

        try:
            setup = detector.detect(ticker, gex_data, spot, as_of=detect_as_of)
            gex_setups[ticker] = setup
            logger.info(
                "%s: regime=%s confidence=%.2f direction=%s",
                ticker, setup.regime, setup.structure_confidence, setup.candidate_direction,
            )
            if tel:
                tel.gex_setup(
                    ticker=ticker,
                    regime=setup.regime.value,
                    direction=setup.candidate_direction,
                    setup_type=setup.setup_type,
                    confidence=setup.structure_confidence,
                    flip_point=float(setup.flip_point) if setup.flip_point else None,
                    target_level=float(setup.target_level) if setup.target_level else None,
                    duration_ms=round((time.monotonic() - t0) * 1000, 1),
                )
        except Exception as exc:
            logger.error("%s detect_gex failed: %s", ticker, exc)
            errors.append(f"{ticker}.detect_gex: {exc}")
            if tel:
                tel.gex_setup(ticker=ticker, regime="unknown", direction="none",
                               setup_type="none", confidence=0.0, flip_point=None,
                               target_level=None,
                               duration_ms=round((time.monotonic() - t0) * 1000, 1),
                               skipped=True, reason=str(exc))

    return {"gex_setups": gex_setups, "errors": errors}


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def score_candidates(
    state: TradingAgentState,
    scorer: BlendScorer,
    tel: TelemetryLogger | None = None,
) -> dict[str, Any]:
    """
    Phase 3 node: score every GEXSetup and rank the resulting CandidateSignals.
    MIXED setups are included in output with execution_status='skipped_no_structure'.
    """
    candidates = []
    for ticker, setup in state.gex_setups.items():
        t0 = time.monotonic()
        candidate = scorer.score(
            setup=setup,
            market_tide=state.market_tide,
            darkpool=state.darkpool.get(ticker, []),
            flow_alerts=state.flow_alerts,
            net_prem_ticks=state.net_prem_ticks.get(ticker, []),
            iv_entries=state.interpolated_iv.get(ticker, []),
            rsi_data=state.technicals.get(ticker, {}).get("RSI", []),
            macd_data=state.technicals.get(ticker, {}).get("MACD", []),
        )
        candidates.append(candidate)
        logger.info(
            "%s: composite=%.3f status=%s",
            ticker, candidate.blend_scores.composite, candidate.execution_status,
        )
        if tel:
            s = candidate.blend_scores
            tel.blend_score(
                ticker=ticker,
                composite=s.composite,
                market_tide=s.market_tide,
                darkpool=s.darkpool,
                flow_pressure=s.flow_pressure,
                iv_cost=s.iv_cost,
                technicals=s.technicals,
                rank=candidate.rank,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
            )

    ranked = scorer.rank(candidates)
    return {"candidates": ranked}


def select_contracts(
    state: TradingAgentState,
    selector: ContractSelector,
    tel: TelemetryLogger | None = None,
) -> dict[str, Any]:
    """
    Phase 5 node: pick the best OptionContract for each flow-confirmed candidate.
    Non-proposed candidates pass through. Candidates with no eligible contract
    become not_executable_long_only.
    """
    updated = []
    for candidate in state.candidates:
        t0 = time.monotonic()
        contracts = state.option_contracts.get(candidate.ticker, [])
        result = selector.select(candidate, contracts)
        updated.append(result)
        c = result.selected_contract
        logger.info(
            "%s: contract=%s status=%s",
            candidate.ticker,
            c.strike if c else None,
            result.execution_status,
        )
        if tel:
            from datetime import date as _date
            spread_pct = (
                float((c.ask - c.bid) / c.ask) if c and c.ask else None
            )
            dte = (
                (c.expiry - _date.today()).days if c else None
            )
            tel.contract_select(
                ticker=candidate.ticker,
                selected=c is not None,
                strike=float(c.strike) if c else None,
                expiry=c.expiry.isoformat() if c else None,
                delta=float(c.delta) if c and c.delta else None,
                dte=dte,
                spread_pct=spread_pct,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
                reason=result.skip_reason if c is None else None,
            )
    return {"candidates": updated}


async def execute_orders(
    state: TradingAgentState,
    executor: Executor,
    tel: TelemetryLogger | None = None,
) -> dict[str, Any]:
    """
    Phase 7 node: dispatch risk-approved candidates via the configured ExecutionMode.

    Only candidates with execution_status='proposed' AND a selected_contract are sent
    to the executor. All others pass through unchanged.

    propose_only  — no orders placed; candidates stay 'proposed'
    rh_approval   — graph interrupts per candidate for human confirmation
    autonomous    — places immediately if review passes
    """
    updated: list = []
    results: list[OrderResult] = []

    for candidate in state.candidates:
        if candidate.execution_status != "proposed" or candidate.selected_contract is None:
            updated.append(candidate)
            continue

        t0 = time.monotonic()
        try:
            result = await executor.execute(candidate)
            results.append(result)

            if result.placed:
                new_status = "executed"
            elif executor.mode == ExecutionMode.PROPOSE_ONLY:
                new_status = "proposed"
            else:
                new_status = "rejected_by_approval"

            updated.append(candidate.model_copy(update={
                "execution_status": new_status,
                "skip_reason": result.rejection_reason,
            }))
            logger.info(
                "%s: execute_orders placed=%s status=%s",
                candidate.ticker, result.placed, new_status,
            )
            if tel:
                lp = result.request.limit_price
                tel.order_attempt(
                    ticker=candidate.ticker,
                    mode=executor.mode.value,
                    action=result.request.action,
                    quantity=result.request.quantity,
                    limit_price=float(lp) if lp is not None else None,
                    placed=result.placed,
                    order_id=result.order_id,
                    account_number=executor.account_number or None,
                    rejection_reason=result.rejection_reason,
                    review_summary=result.review_summary,
                    duration_ms=round((time.monotonic() - t0) * 1000, 1),
                )
        except Exception as exc:
            logger.error("%s execute_orders failed: %s", candidate.ticker, exc)
            results.append(OrderResult(
                request=OrderRequest(
                    candidate=candidate,
                    action="buy_to_open",
                    quantity=executor.quantity,
                    limit_price=None,
                    mode=executor.mode,
                ),
                placed=False,
                rejection_reason=str(exc),
                timestamp=datetime.now(timezone.utc),
            ))
            updated.append(candidate)
            if tel:
                tel.order_attempt(
                    ticker=candidate.ticker,
                    mode=executor.mode.value,
                    action="buy_to_open",
                    quantity=executor.quantity,
                    limit_price=None,
                    placed=False,
                    order_id=None,
                    account_number=executor.account_number or None,
                    rejection_reason=str(exc),
                    review_summary=None,
                    duration_ms=round((time.monotonic() - t0) * 1000, 1),
                )

    return {"candidates": updated, "order_results": results}


def risk_gate(
    state: TradingAgentState,
    engine: RiskEngine,
    tel: TelemetryLogger | None = None,
) -> dict[str, Any]:
    """
    Phase 6 node: apply hard risk gates to every proposed candidate.
    Only candidates with a selected_contract are checked; others pass through.
    Rejected candidates get execution_status="skipped_risk_gate".
    """
    updated = []
    for candidate in state.candidates:
        if candidate.execution_status != "proposed" or candidate.selected_contract is None:
            updated.append(candidate)
            continue

        t0 = time.monotonic()
        verdict = engine.check(candidate)
        if verdict.approved:
            updated.append(candidate)
        else:
            updated.append(candidate.model_copy(update={
                "execution_status": "skipped_risk_gate",
                "skip_reason": "; ".join(verdict.reasons),
            }))
        logger.info("%s: risk_gate approved=%s", candidate.ticker, verdict.approved)
        if tel:
            tel.risk_check(
                ticker=candidate.ticker,
                approved=verdict.approved,
                reasons=verdict.reasons,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
            )
    return {"candidates": updated}


def check_flow(
    state: TradingAgentState,
    trigger: FlowTrigger,
    tel: TelemetryLogger | None = None,
) -> dict[str, Any]:
    """
    Phase 4 node: confirm each proposed candidate against live flow alerts.
    Candidates without a matching whale print become skipped_no_flow.
    pipeline_date is used as the as_of anchor when set (backtest replay).
    """
    if state.pipeline_date is not None:
        as_of = datetime.combine(state.pipeline_date, dtime(16, 0), tzinfo=timezone.utc)
    else:
        as_of = datetime.now(timezone.utc)

    confirmed = []
    for c_in in state.candidates:
        t0 = time.monotonic()
        c = trigger.check(c_in, state.flow_alerts, as_of=as_of)
        confirmed.append(c)
        logger.info("%s: flow_confirmed=%s status=%s", c.ticker, c.flow_confirmed, c.execution_status)
        if tel:
            alert_premium = float(c.flow_trigger.total_premium) if c.flow_trigger else None
            tel.flow_check(
                ticker=c.ticker,
                confirmed=c.flow_confirmed,
                direction=c.gex_setup.candidate_direction,
                alert_premium=alert_premium,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
            )
    return {"candidates": confirmed}


def build_graph(
    tools: list[BaseTool],
    detector_params: GEXDetectorParams | None = None,
    blend_weights: dict[str, float] | None = None,
    flow_min_premium: Decimal = Decimal("100_000"),
    flow_lookback_hours: int = 4,
    selector_params: SelectorParams | None = None,
    risk_params: RiskParams | None = None,
    portfolio: PortfolioState | None = None,
    sector_map: dict[str, str] | None = None,
    execution_mode: ExecutionMode = ExecutionMode.PROPOSE_ONLY,
    account_number: str = "",
    rh_tools: list[BaseTool] | None = None,
    order_quantity: int = 1,
    telemetry: TelemetryLogger | None = None,
) -> Any:
    """
    Construct and compile the LangGraph StateGraph.

    Pipeline (Phases 1–7):
      START → fetch_market_data → fetch_ticker_data → detect_gex
           → score_candidates → check_flow → select_contracts → risk_gate
           → execute_orders → END

    execution_mode defaults to PROPOSE_ONLY (no real orders).
    rh_tools is the list of Robinhood MCP tools injected for non-propose modes;
    only get_option_instruments, review_option_order, and place_option_order are used.
    """
    tool_map = {t.name: t for t in tools}
    rh_tool_map = {t.name: t for t in (rh_tools or [])}

    detector = GEXDetector(detector_params)
    scorer = BlendScorer(blend_weights)
    trigger = FlowTrigger(min_premium=flow_min_premium, lookback_hours=flow_lookback_hours)
    selector = ContractSelector(selector_params)
    engine = RiskEngine(params=risk_params, portfolio=portfolio, sector_map=sector_map)
    executor = Executor(
        mode=execution_mode,
        account_number=account_number,
        rh_tools=rh_tool_map,
        quantity=order_quantity,
    )

    tel = telemetry

    async def _fetch_market(state: TradingAgentState) -> dict[str, Any]:
        return await fetch_market_data(state, tool_map, tel)

    async def _fetch_ticker(state: TradingAgentState) -> dict[str, Any]:
        return await fetch_ticker_data(state, tool_map, tel)

    def _detect_gex(state: TradingAgentState) -> dict[str, Any]:
        return detect_gex(state, detector, tel)

    def _score_candidates(state: TradingAgentState) -> dict[str, Any]:
        return score_candidates(state, scorer, tel)

    def _check_flow(state: TradingAgentState) -> dict[str, Any]:
        return check_flow(state, trigger, tel)

    def _select_contracts(state: TradingAgentState) -> dict[str, Any]:
        return select_contracts(state, selector, tel)

    def _risk_gate(state: TradingAgentState) -> dict[str, Any]:
        return risk_gate(state, engine, tel)

    async def _execute_orders(state: TradingAgentState) -> dict[str, Any]:
        return await execute_orders(state, executor, tel)

    builder: StateGraph = StateGraph(TradingAgentState)

    builder.add_node("fetch_market_data", _fetch_market)
    builder.add_node("fetch_ticker_data", _fetch_ticker)
    builder.add_node("detect_gex", _detect_gex)
    builder.add_node("score_candidates", _score_candidates)
    builder.add_node("check_flow", _check_flow)
    builder.add_node("select_contracts", _select_contracts)
    builder.add_node("risk_gate", _risk_gate)
    builder.add_node("execute_orders", _execute_orders)

    try:
        builder.add_node("tools", ToolNode(tools))
    except Exception:
        pass

    # Phase 1 → 2 → 3 → 4 → 5 → 6 → 7 pipeline
    builder.add_edge(START, "fetch_market_data")
    builder.add_edge("fetch_market_data", "fetch_ticker_data")
    builder.add_edge("fetch_ticker_data", "detect_gex")
    builder.add_edge("detect_gex", "score_candidates")
    builder.add_edge("score_candidates", "check_flow")
    builder.add_edge("check_flow", "select_contracts")
    builder.add_edge("select_contracts", "risk_gate")
    builder.add_edge("risk_gate", "execute_orders")
    builder.add_edge("execute_orders", END)

    return builder.compile()


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


async def run_pipeline(
    tickers: list[str],
    tools: list[BaseTool],
    detector_params: GEXDetectorParams | None = None,
    blend_weights: dict[str, float] | None = None,
    flow_min_premium: Decimal = Decimal("100_000"),
    flow_lookback_hours: int = 4,
    selector_params: SelectorParams | None = None,
    risk_params: RiskParams | None = None,
    portfolio: PortfolioState | None = None,
    sector_map: dict[str, str] | None = None,
    execution_mode: ExecutionMode = ExecutionMode.PROPOSE_ONLY,
    account_number: str = "",
    rh_tools: list[BaseTool] | None = None,
    order_quantity: int = 1,
    pipeline_date: date | None = None,
    telemetry: TelemetryLogger | None = None,
) -> TradingAgentState:
    """Run the full graph for a given ticker list and return final state.

    pipeline_date: when set, the flow-trigger gate uses this date (at 16:00 UTC) as
    the as_of anchor instead of datetime.now(). Required for deterministic backtest replay.
    telemetry: optional TelemetryLogger; if None, no structured events are emitted.
    """
    graph = build_graph(
        tools,
        detector_params,
        blend_weights,
        flow_min_premium=flow_min_premium,
        flow_lookback_hours=flow_lookback_hours,
        selector_params=selector_params,
        risk_params=risk_params,
        portfolio=portfolio,
        sector_map=sector_map,
        execution_mode=execution_mode,
        account_number=account_number,
        rh_tools=rh_tools,
        order_quantity=order_quantity,
        telemetry=telemetry,
    )
    initial = TradingAgentState(tickers=tickers, pipeline_date=pipeline_date)
    result = await graph.ainvoke(initial)
    return TradingAgentState.model_validate(result)
