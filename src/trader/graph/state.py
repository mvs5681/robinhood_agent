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

from trader.gex.schemas import GEXSetup
from trader.scoring.schemas import CandidateSignal
from trader.uw.schemas import (
    DarkpoolPrint,
    FlowAlert,
    InterpolatedIVEntry,
    MarketTide,
    NetPremTick,
    OptionContract,
    SpotGEXByStrike,
    TechnicalPoint,
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

    # Phase 2: GEX setups keyed by ticker
    gex_setups: dict[str, GEXSetup] = {}

    # Phase 3: IV and technical data (ticker → data)
    interpolated_iv: dict[str, list[InterpolatedIVEntry]] = {}
    technicals: dict[str, dict[str, list[TechnicalPoint]]] = {}

    # Pipeline artefacts
    candidates: list[CandidateSignal] = []
    errors: list[str] = []

    # LangGraph message log (tool call history for ToolNode)
    messages: Annotated[list[Any], add_messages] = []

    model_config = ConfigDict(arbitrary_types_allowed=True)
