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
from datetime import datetime, timezone
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
) -> dict[str, Any]:
    """
    Fetch market-wide data: market tide and global flow alerts.
    Single API call each — no per-ticker loop.
    """
    updates: dict[str, Any] = {}

    try:
        raw_tide = await tools["get_market_tide"].ainvoke({})
        updates["market_tide"] = parse_market_tide(raw_tide)
        logger.info("market_tide: %d ticks", len(updates["market_tide"]))
    except Exception as exc:
        logger.error("fetch_market_data market_tide failed: %s", exc)
        updates["errors"] = state.errors + [f"market_tide: {exc}"]

    try:
        raw_flow = await tools["get_flow_alerts"].ainvoke({"limit": 100})
        updates["flow_alerts"] = parse_flow_alerts(raw_flow)
        logger.info("flow_alerts: %d alerts", len(updates["flow_alerts"]))
    except Exception as exc:
        logger.error("fetch_market_data flow_alerts failed: %s", exc)
        updates.setdefault("errors", state.errors)
        updates["errors"] = updates["errors"] + [f"flow_alerts: {exc}"]

    return updates


async def fetch_ticker_data(
    state: TradingAgentState,
    tools: dict[str, BaseTool],
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
        try:
            raw_gex = await tools["get_spot_exposures_by_strike"].ainvoke({"ticker": ticker})
            spot_gex[ticker] = parse_spot_gex_by_strike(raw_gex)
            logger.info("%s spot_gex: %d strikes", ticker, len(spot_gex[ticker]))
        except Exception as exc:
            logger.error("%s spot_gex failed: %s", ticker, exc)
            errors.append(f"{ticker}.spot_gex: {exc}")

        try:
            raw_dp = await tools["get_darkpool_ticker"].ainvoke({"ticker": ticker})
            darkpool[ticker] = parse_darkpool_prints(raw_dp)
            logger.info("%s darkpool: %d prints", ticker, len(darkpool[ticker]))
        except Exception as exc:
            logger.error("%s darkpool failed: %s", ticker, exc)
            errors.append(f"{ticker}.darkpool: {exc}")

        try:
            raw_ticks = await tools["get_net_prem_ticks"].ainvoke({"ticker": ticker})
            net_prem_ticks[ticker] = parse_net_prem_ticks(raw_ticks)
            logger.info("%s net_prem_ticks: %d ticks", ticker, len(net_prem_ticks[ticker]))
        except Exception as exc:
            logger.error("%s net_prem_ticks failed: %s", ticker, exc)
            errors.append(f"{ticker}.net_prem_ticks: {exc}")

        try:
            raw_contracts = await tools["get_option_contracts"].ainvoke({"ticker": ticker})
            option_contracts[ticker] = parse_option_contracts(raw_contracts)
            logger.info("%s option_contracts: %d contracts", ticker, len(option_contracts[ticker]))
        except Exception as exc:
            logger.error("%s option_contracts failed: %s", ticker, exc)
            errors.append(f"{ticker}.option_contracts: {exc}")

        try:
            raw_iv = await tools["get_interpolated_iv"].ainvoke({"ticker": ticker})
            interpolated_iv[ticker] = parse_interpolated_iv(raw_iv)
            logger.info("%s interpolated_iv: %d entries", ticker, len(interpolated_iv[ticker]))
        except Exception as exc:
            logger.error("%s interpolated_iv failed: %s", ticker, exc)
            errors.append(f"{ticker}.interpolated_iv: {exc}")

        ticker_technicals: dict = {}
        for fn in ("RSI", "MACD"):
            try:
                raw_tech = await tools["get_technical_indicator"].ainvoke(
                    {"ticker": ticker, "function": fn, "interval": "daily"}
                )
                ticker_technicals[fn] = parse_technical_indicator(raw_tech, fn)
                logger.info("%s %s: %d points", ticker, fn, len(ticker_technicals[fn]))
            except Exception as exc:
                logger.error("%s %s failed: %s", ticker, fn, exc)
                errors.append(f"{ticker}.{fn}: {exc}")
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
) -> dict[str, Any]:
    """
    Phase 2 node: run GEXDetector over every ticker's spot_gex data.

    Spot price is resolved from existing state in priority order:
      1. flow_alerts[ticker].underlying_price (most timely)
      2. darkpool[ticker] most recent print price
      3. Skip ticker if neither is available
    """
    # Build a spot price lookup from whatever data we already have
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

    for ticker in state.tickers:
        gex_data = state.spot_gex.get(ticker, [])
        if not gex_data:
            logger.warning("%s: no GEX data, skipping detector", ticker)
            continue

        spot = spot_lookup.get(ticker)
        if spot is None:
            logger.warning("%s: no spot price available, skipping detector", ticker)
            errors.append(f"{ticker}.detect_gex: no spot price")
            continue

        try:
            setup = detector.detect(ticker, gex_data, spot)
            gex_setups[ticker] = setup
            logger.info(
                "%s: regime=%s confidence=%.2f direction=%s",
                ticker,
                setup.regime,
                setup.structure_confidence,
                setup.candidate_direction,
            )
        except Exception as exc:
            logger.error("%s detect_gex failed: %s", ticker, exc)
            errors.append(f"{ticker}.detect_gex: {exc}")

    return {"gex_setups": gex_setups, "errors": errors}


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def score_candidates(
    state: TradingAgentState,
    scorer: BlendScorer,
) -> dict[str, Any]:
    """
    Phase 3 node: score every GEXSetup and rank the resulting CandidateSignals.
    MIXED setups are included in output with execution_status='skipped_no_structure'.
    """
    candidates = []
    for ticker, setup in state.gex_setups.items():
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
            ticker,
            candidate.blend_scores.composite,
            candidate.execution_status,
        )

    ranked = scorer.rank(candidates)
    return {"candidates": ranked}


def select_contracts(
    state: TradingAgentState,
    selector: ContractSelector,
) -> dict[str, Any]:
    """
    Phase 5 node: pick the best OptionContract for each flow-confirmed candidate.
    Non-proposed candidates pass through. Candidates with no eligible contract
    become not_executable_long_only.
    """
    updated = []
    for candidate in state.candidates:
        contracts = state.option_contracts.get(candidate.ticker, [])
        result = selector.select(candidate, contracts)
        updated.append(result)
        logger.info(
            "%s: contract=%s status=%s",
            candidate.ticker,
            result.selected_contract.strike if result.selected_contract else None,
            result.execution_status,
        )
    return {"candidates": updated}


async def execute_orders(
    state: TradingAgentState,
    executor: Executor,
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
                candidate.ticker,
                result.placed,
                new_status,
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

    return {"candidates": updated, "order_results": results}


def risk_gate(
    state: TradingAgentState,
    engine: RiskEngine,
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

        verdict = engine.check(candidate)
        if verdict.approved:
            updated.append(candidate)
        else:
            updated.append(candidate.model_copy(update={
                "execution_status": "skipped_risk_gate",
                "skip_reason": "; ".join(verdict.reasons),
            }))
        logger.info(
            "%s: risk_gate approved=%s",
            candidate.ticker,
            verdict.approved,
        )
    return {"candidates": updated}


def check_flow(
    state: TradingAgentState,
    trigger: FlowTrigger,
) -> dict[str, Any]:
    """
    Phase 4 node: confirm each proposed candidate against live flow alerts.
    Candidates without a matching whale print become skipped_no_flow.
    """
    as_of = datetime.now(timezone.utc)
    confirmed = [trigger.check(c, state.flow_alerts, as_of=as_of) for c in state.candidates]
    for c in confirmed:
        logger.info(
            "%s: flow_confirmed=%s status=%s",
            c.ticker,
            c.flow_confirmed,
            c.execution_status,
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

    async def _fetch_market(state: TradingAgentState) -> dict[str, Any]:
        return await fetch_market_data(state, tool_map)

    async def _fetch_ticker(state: TradingAgentState) -> dict[str, Any]:
        return await fetch_ticker_data(state, tool_map)

    def _detect_gex(state: TradingAgentState) -> dict[str, Any]:
        return detect_gex(state, detector)

    def _score_candidates(state: TradingAgentState) -> dict[str, Any]:
        return score_candidates(state, scorer)

    def _check_flow(state: TradingAgentState) -> dict[str, Any]:
        return check_flow(state, trigger)

    def _select_contracts(state: TradingAgentState) -> dict[str, Any]:
        return select_contracts(state, selector)

    def _risk_gate(state: TradingAgentState) -> dict[str, Any]:
        return risk_gate(state, engine)

    async def _execute_orders(state: TradingAgentState) -> dict[str, Any]:
        return await execute_orders(state, executor)

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
) -> TradingAgentState:
    """Run the full graph for a given ticker list and return final state."""
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
    )
    initial = TradingAgentState(tickers=tickers)
    result = await graph.ainvoke(initial)
    return TradingAgentState.model_validate(result)
