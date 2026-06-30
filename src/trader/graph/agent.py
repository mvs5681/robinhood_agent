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
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from trader.gex.detector import GEXDetector
from trader.gex.schemas import GEXDetectorParams, GEXSetup
from trader.uw.validators import (
    parse_darkpool_prints,
    parse_flow_alerts,
    parse_market_tide,
    parse_net_prem_ticks,
    parse_spot_gex_by_strike,
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
    errors: list[str] = list(state.errors)

    async def _fetch_one(ticker: str) -> None:
        try:
            raw_gex = await tools["get_spot_exposures_by_strike"].ainvoke(
                {"ticker": ticker}
            )
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

    await asyncio.gather(*[_fetch_one(t) for t in state.tickers])

    return {
        "spot_gex": spot_gex,
        "darkpool": darkpool,
        "net_prem_ticks": net_prem_ticks,
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


def build_graph(
    tools: list[BaseTool],
    detector_params: GEXDetectorParams | None = None,
) -> Any:
    """
    Construct and compile the LangGraph StateGraph.

    tools:           list of BaseTool from load_uw_tools() or MockUWTools adapter.
    detector_params: optional overrides for GEXDetector thresholds.

    Pipeline (Phase 1 + 2):
      START → fetch_market_data → fetch_ticker_data → detect_gex → END
    """
    tool_map = {t.name: t for t in tools}
    detector = GEXDetector(detector_params)

    async def _fetch_market(state: TradingAgentState) -> dict[str, Any]:
        return await fetch_market_data(state, tool_map)

    async def _fetch_ticker(state: TradingAgentState) -> dict[str, Any]:
        return await fetch_ticker_data(state, tool_map)

    def _detect_gex(state: TradingAgentState) -> dict[str, Any]:
        return detect_gex(state, detector)

    builder: StateGraph = StateGraph(TradingAgentState)

    builder.add_node("fetch_market_data", _fetch_market)
    builder.add_node("fetch_ticker_data", _fetch_ticker)
    builder.add_node("detect_gex", _detect_gex)

    # Tool node for future LLM-assisted supervisor (not in trade path).
    try:
        builder.add_node("tools", ToolNode(tools))
    except Exception:
        pass

    builder.add_edge(START, "fetch_market_data")
    builder.add_edge("fetch_market_data", "fetch_ticker_data")
    builder.add_edge("fetch_ticker_data", "detect_gex")
    builder.add_edge("detect_gex", END)

    return builder.compile()


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


async def run_pipeline(
    tickers: list[str],
    tools: list[BaseTool],
    detector_params: GEXDetectorParams | None = None,
) -> TradingAgentState:
    """Run the full graph for a given ticker list and return final state."""
    graph = build_graph(tools, detector_params)
    initial = TradingAgentState(tickers=tickers)
    result = await graph.ainvoke(initial)
    return TradingAgentState.model_validate(result)
