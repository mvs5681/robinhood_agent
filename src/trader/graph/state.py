"""
Shared state for the LangGraph trading agent.

Each phase of the pipeline reads from and writes to TradingAgentState.
The graph is deterministic — no LLM calls in the trade-selection path.
LangGraph manages state checkpointing and conditional routing.
"""

from __future__ import annotations

from typing import Annotated, Any

from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict

from trader.uw.schemas import (
    DarkpoolPrint,
    FlowAlert,
    MarketTide,
    NetPremTick,
    OptionContract,
    SpotGEXByStrike,
)


class TradingAgentState(BaseModel):
    """
    Immutable-ish state passed between graph nodes.
    LangGraph merges node return dicts into this via model_copy(update=...).
    """

    # Input
    tickers: list[str] = []

    # Data fetched from UW (keyed by ticker where applicable)
    spot_gex: dict[str, list[SpotGEXByStrike]] = {}
    flow_alerts: list[FlowAlert] = []
    market_tide: list[MarketTide] = []
    darkpool: dict[str, list[DarkpoolPrint]] = {}
    net_prem_ticks: dict[str, list[NetPremTick]] = {}
    option_contracts: dict[str, list[OptionContract]] = {}

    # Pipeline artefacts (populated by later phases)
    candidates: list[Any] = []   # list[CandidateSignal] — typed in Phase 3
    errors: list[str] = []

    # LangGraph message log (tool call history for ToolNode)
    messages: Annotated[list[Any], add_messages] = []

    model_config = ConfigDict(arbitrary_types_allowed=True)
