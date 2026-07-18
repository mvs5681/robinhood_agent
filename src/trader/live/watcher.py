"""Flow Watcher — polls for new whale prints every 60 s and triggers the pipeline.

When a new high-premium flow alert arrives for a ticker with a valid GEX setup:
  1. Build TradingAgentState from cached slow data + fresh alerts
  2. Run score → flow_check → select_contracts → risk_gate
  3. Proposed candidates go into ProposalStore for human approval (rh_approval mode)
     or are executed immediately (autonomous mode)
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

from langchain_core.tools import BaseTool

from trader.contracts.selector import ContractSelector, SelectorParams
from trader.executor.executor import Executor
from trader.executor.schemas import ExecutionMode
from trader.flow.trigger import FlowTrigger
from trader.graph.agent import check_flow, risk_gate, score_candidates, select_contracts
from trader.graph.state import TradingAgentState
from trader.gex.schemas import GEXDetectorParams
from trader.risk.engine import RiskEngine
from trader.risk.schemas import PortfolioState, RiskParams
from trader.scoring.scorer import BlendScorer
from trader.telemetry.logger import TelemetryLogger
from trader.uw.validators import parse_flow_alerts

from .cache import GEXCache
from .market_hours import is_market_hours
from .notifier import TelegramNotifier
from .position_store import PositionStore, make_position
from .proposals import Proposal, ProposalStore

if TYPE_CHECKING:
    from trader.uw.schemas import FlowAlert
    from .config import LiveConfig
    from .order_manager import OrderLifecycleManager

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 60     # seconds between flow alert polls
_IDLE_SLEEP = 120       # seconds to sleep when market is closed


def _apply(state: TradingAgentState, updates: dict) -> TradingAgentState:
    return state.model_copy(update=updates)


class FlowWatcher:
    """
    Polls get_flow_alerts every 60 s. On a new high-premium print for a
    watched ticker: runs the scoring pipeline and either sends the candidate
    to ProposalStore (rh_approval) or executes it directly (autonomous).
    """

    def __init__(
        self,
        uw_tools: dict[str, BaseTool],
        cache: GEXCache,
        proposal_store: ProposalStore,
        execution_mode: ExecutionMode,
        executor: Executor,
        flow_min_premium: Decimal = Decimal("100_000"),
        flow_lookback_hours: int = 4,
        blend_weights: dict[str, float] | None = None,
        selector_params: SelectorParams | None = None,
        risk_params: RiskParams | None = None,
        sector_map: dict[str, str] | None = None,
        tel: TelemetryLogger | None = None,
        poll_interval: int = _POLL_INTERVAL,
        notifier: TelegramNotifier | None = None,
        position_store: PositionStore | None = None,
        risk_engine: RiskEngine | None = None,
        config: "LiveConfig | None" = None,
        order_manager: "OrderLifecycleManager | None" = None,
    ) -> None:
        self.uw_tools = uw_tools
        self.cache = cache
        self.proposal_store = proposal_store
        self.mode = execution_mode
        self.executor = executor
        self.tel = tel
        self.poll_interval = poll_interval

        self._scorer = BlendScorer(blend_weights)
        self._trigger = FlowTrigger(
            min_premium=flow_min_premium, lookback_hours=flow_lookback_hours
        )
        self._selector = ContractSelector(selector_params)
        self._engine = risk_engine or RiskEngine(
            params=risk_params, portfolio=PortfolioState(), sector_map=sector_map
        )
        self._notifier = notifier
        self._position_store = position_store
        self._config = config
        self._order_manager = order_manager
        # dedup by (ticker, expiry, strike, type, created_at) — dict preserves
        # insertion order so trimming drops the oldest keys, not arbitrary ones
        self._seen: dict[str, None] = {}

    async def run(self) -> None:
        logger.info("FlowWatcher started — mode=%s (tracks GEXScanner cache dynamically)", self.mode.value)
        while True:
            if not is_market_hours():
                await asyncio.sleep(_IDLE_SLEEP)
                continue
            if not self.cache.ready:
                logger.debug("FlowWatcher: GEX cache not ready yet, waiting…")
                await asyncio.sleep(10)
                continue
            try:
                await self._poll()
            except Exception as exc:
                logger.error("FlowWatcher poll error: %s", exc)
            await asyncio.sleep(self.poll_interval)

    async def _poll(self) -> None:
        if self._config is not None:
            self._trigger.min_premium = self._config.flow_min_premium
        t0 = _time.monotonic()
        try:
            # Server-side premium filter: only prints that could actually
            # confirm a trade trigger a pipeline run, instead of every small
            # alert producing a guaranteed "no matching whale print" skip.
            raw = await self.uw_tools["get_flow_alerts"].ainvoke(
                {"limit": 100, "min_premium": str(self._trigger.min_premium)}
            )
            alerts: list[FlowAlert] = parse_flow_alerts(raw)
        except Exception as exc:
            logger.error("get_flow_alerts failed: %s", exc)
            if self.tel:
                self.tel.uw_fetch(endpoint="get_flow_alerts", record_count=0,
                                  duration_ms=round((_time.monotonic() - t0) * 1000, 1),
                                  error=str(exc))
            return

        ms = round((_time.monotonic() - t0) * 1000, 1)
        if self.tel:
            self.tel.uw_fetch(endpoint="get_flow_alerts",
                              record_count=len(alerts), duration_ms=ms)

        new_alerts = self._filter_new(alerts)
        if not new_alerts:
            return

        logger.info("FlowWatcher: %d new qualifying alerts", len(new_alerts))

        # Only process tickers the GEXScanner has already cached + validated
        cached_tickers = set(self.cache.tickers.keys())
        affected = {a.ticker for a in new_alerts} & cached_tickers
        if not affected:
            logger.debug("FlowWatcher: new alerts for %s but none in GEX cache yet",
                         {a.ticker for a in new_alerts})
            return
        affected_ordered = sorted(affected)
        tasks = [self._run_pipeline(ticker, alerts) for ticker in affected_ordered]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for ticker, result in zip(affected_ordered, results):
            if isinstance(result, Exception):
                logger.error("%s pipeline failed: %s", ticker, result)

    def _alert_key(self, alert: FlowAlert) -> str:
        return f"{alert.ticker}:{alert.expiry}:{alert.strike}:{alert.type}:{alert.created_at}"

    def _filter_new(self, alerts: list[FlowAlert]) -> list[FlowAlert]:
        """Return alerts we haven't processed yet, for tickers we watch."""
        new = []
        for a in alerts:
            if a.ticker not in self.cache.tickers:
                continue
            key = self._alert_key(a)
            if key not in self._seen:
                self._seen[key] = None
                new.append(a)
        # Prevent unbounded growth — keep the most recent half
        if len(self._seen) > 10_000:
            self._seen = dict(list(self._seen.items())[-5_000:])
        return new

    async def _run_pipeline(self, ticker: str, all_alerts: list[FlowAlert]) -> None:
        snap = await self.cache.snapshot(ticker)
        if snap is None or snap.gex_setup is None:
            logger.debug("%s: no GEX setup in cache, skipping", ticker)
            return
        if snap.is_stale:
            logger.warning("%s: GEX cache stale (refreshed_at=%s), skipping pipeline",
                           ticker, snap.refreshed_at.isoformat())
            return

        # Unique ID linking all telemetry events for this pipeline run
        run_id = f"{ticker}:{uuid4().hex[:8]}"
        run_tel = self.tel.with_run_id(run_id) if self.tel else None

        market_tide = self.cache.market_tide

        state = TradingAgentState(
            tickers=[ticker],
            market_tide=market_tide,
            flow_alerts=all_alerts,
            spot_gex={ticker: snap.spot_gex},
            darkpool={ticker: snap.darkpool},
            net_prem_ticks={ticker: snap.net_prem_ticks},
            option_contracts={ticker: snap.option_contracts},
            interpolated_iv={ticker: snap.interpolated_iv},
            technicals={ticker: snap.technicals},
            gex_setups={ticker: snap.gex_setup},
        )

        # Run scoring phases — node functions emit telemetry tagged with run_id
        state = _apply(state, score_candidates(state, self._scorer, run_tel))
        state = _apply(state, check_flow(state, self._trigger, run_tel))
        state = _apply(state, select_contracts(state, self._selector, run_tel))
        state = _apply(state, risk_gate(state, self._engine, run_tel))

        proposed = [
            c for c in state.candidates
            if c.execution_status == "proposed" and c.selected_contract is not None
        ]

        if not proposed:
            logger.debug("%s: no proposed candidates after pipeline", ticker)
            return

        # One live signal per ticker: skip if a proposal for this ticker is
        # already within its TTL window (every new whale print re-runs the
        # pipeline — without this, a hot ticker mints a duplicate proposal
        # and Telegram ping per print) or if a position is already open.
        if await self.proposal_store.has_recent(ticker):
            logger.debug("%s: proposal already exists within TTL — skipping duplicate", ticker)
            return
        if self._position_store is not None and any(
            p.ticker == ticker for p in await self._position_store.all()
        ):
            logger.debug("%s: open position exists — skipping new entry", ticker)
            return

        for candidate in proposed:
            if self.mode == ExecutionMode.PROPOSE_ONLY:
                proposal = await self.proposal_store.add(candidate, run_id=run_id)
                logger.info(
                    "PROPOSE_ONLY %s proposal_id=%s composite=%.3f",
                    ticker, proposal.proposal_id, candidate.blend_scores.composite,
                )
                if self._notifier:
                    await self._notifier.notify_proposal(proposal)

            elif self.mode == ExecutionMode.RH_APPROVAL:
                proposal = await self.proposal_store.add(candidate, run_id=run_id)
                logger.info(
                    "RH_APPROVAL %s proposal_id=%s — awaiting human approval",
                    ticker, proposal.proposal_id,
                )
                if self._notifier:
                    await self._notifier.notify_proposal(proposal)

            elif self.mode == ExecutionMode.AUTONOMOUS:
                try:
                    t0 = _time.monotonic()
                    result = await self.executor.execute(candidate)
                    ms = round((_time.monotonic() - t0) * 1000, 1)
                    if self.tel:
                        lp = result.request.limit_price
                        self.tel.order_attempt(
                            ticker=ticker,
                            mode=self.mode.value,
                            action=result.request.action,
                            quantity=result.request.quantity,
                            limit_price=float(lp) if lp is not None else None,
                            placed=result.placed,
                            order_id=result.order_id,
                            account_number=self.executor.account_number or None,
                            rejection_reason=result.rejection_reason,
                            review_summary=result.review_summary,
                            duration_ms=ms,
                        )
                    if result.placed:
                        if self._order_manager is not None:
                            # Lifecycle manager promotes to a tracked position on fill
                            await self._order_manager.track(candidate, result)
                        elif self._position_store is not None:
                            pos = make_position(candidate, result, result.request.quantity)
                            if pos:
                                await self._position_store.add(pos)
                                logger.info("Position tracked %s position_id=%s", ticker, pos.position_id)
                    logger.info(
                        "AUTONOMOUS %s placed=%s order_id=%s",
                        ticker, result.placed, result.order_id,
                    )
                    if self._notifier:
                        sc = candidate.selected_contract
                        detail = (f"{sc.type} ${sc.strike} exp {sc.expiry}"
                                  if sc else "")
                        if result.placed:
                            await self._notifier.notify_text(
                                f"<b>Autonomous order placed</b>\n"
                                f"{ticker} {detail} x{result.request.quantity} "
                                f"@ <code>{result.request.limit_price}</code>\n"
                                f"Order <code>{result.order_id}</code> — "
                                f"fill/reprice managed automatically."
                            )
                        else:
                            await self._notifier.notify_text(
                                f"<b>Autonomous order rejected</b>\n"
                                f"{ticker} {detail} — {result.rejection_reason}"
                            )
                except Exception as exc:
                    logger.error("%s autonomous execute failed: %s", ticker, exc)
