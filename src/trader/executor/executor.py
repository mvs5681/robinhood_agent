"""Phase 7 — Order Executor.

Three execution modes, in increasing autonomy:
  propose_only  — logs intent, places nothing; safe default for dry runs
  rh_approval   — reviews order with RH, then interrupts for human confirmation before placing
  autonomous    — reviews order with RH, checks for fatal alerts, places immediately

Long-only constraint is enforced in _check_order_type() and is called before
any mode dispatch. ExecutionMode can only be promoted via build_graph() config.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.types import interrupt

from trader.rh.mcp_config import rh_call
from trader.scoring.schemas import CandidateSignal
from trader.uw.schemas import OptionContract

from .schemas import ExecutionMode, OrderRequest, OrderResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize_review(review: Any) -> str:
    """Extract a human-readable one-liner from review_option_order response."""
    if not isinstance(review, dict):
        return str(review)[:200]
    parts: list[str] = []
    for check in review.get("order_checks", []):
        if isinstance(check, dict) and check.get("detail"):
            parts.append(check["detail"])
    quote = review.get("quote") or {}
    if isinstance(quote, dict) and quote.get("mid_price"):
        parts.append(f"mid=${quote['mid_price']}")
    return " | ".join(parts) if parts else "(no review summary)"


def _get_blocking_alerts(review: Any) -> list[str]:
    """Return fatal/error alert strings that should block autonomous placement."""
    if not isinstance(review, dict):
        return []
    blocking: list[str] = []
    for check in review.get("order_checks", []):
        if isinstance(check, dict):
            if check.get("severity", "").lower() in ("fatal", "error"):
                blocking.append(check.get("detail") or check.get("message") or "unknown alert")
    return blocking


def _extract_order_id(result: Any) -> str | None:
    if isinstance(result, dict):
        return result.get("id") or result.get("order_id")
    return None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class Executor:
    """
    Dispatches risk-approved candidates to Robinhood via injected MCP tools.

    rh_tools must contain these keys for non-propose modes:
      - get_option_instruments
      - review_option_order
      - place_option_order
    """

    def __init__(
        self,
        mode: ExecutionMode,
        account_number: str,
        rh_tools: dict[str, BaseTool] | None = None,
        quantity: int = 1,
        max_trade_spend: Decimal | None = None,
        max_contracts: int = 20,
    ) -> None:
        self.mode = mode
        self.account_number = account_number
        self.rh_tools: dict[str, BaseTool] = rh_tools or {}
        self.quantity = quantity
        self._max_trade_spend = max_trade_spend
        self._max_contracts = max_contracts

    def calc_quantity(self, mid: Decimal) -> int:
        """Return contracts to buy given the option mid price.

        If MAX_TRADE_SPEND is set: floor(spend / (mid * 100)), capped at
        max_contracts and floored at 1. Falls back to fixed quantity when
        max_trade_spend is unset or mid is zero.
        """
        if self._max_trade_spend is None or mid <= 0:
            return self.quantity
        cost_per_contract = mid * 100
        qty = int(self._max_trade_spend / cost_per_contract)
        return max(1, min(qty, self._max_contracts))

    async def execute_approved(self, candidate: CandidateSignal) -> OrderResult:
        """Place a buy that has already been approved by the human (Telegram/dashboard).

        Identical to execute() but skips the LangGraph interrupt — used by the
        Telegram notifier after the user taps Approve, so the order is placed
        immediately without re-requesting confirmation inside the graph.
        """
        contract = candidate.selected_contract
        if contract is None:
            raise ValueError(f"{candidate.ticker}: selected_contract is None")
        qty = self.calc_quantity(contract.mid)
        request = OrderRequest(
            candidate=candidate,
            action="buy_to_open",
            quantity=qty,
            limit_price=contract.mid,
            mode=self.mode,
        )
        self._check_order_type(request.action)
        if self.mode == ExecutionMode.PROPOSE_ONLY:
            return self._propose(request)
        option_id = await self._resolve_option_id(contract)
        return await self._autonomous(request, option_id)

    async def execute(self, candidate: CandidateSignal) -> OrderResult:
        """Entry point — build a buy_to_open request and dispatch to the configured mode."""
        contract = candidate.selected_contract
        if contract is None:
            raise ValueError(f"{candidate.ticker}: selected_contract is None")

        qty = self.calc_quantity(contract.mid)
        request = OrderRequest(
            candidate=candidate,
            action="buy_to_open",
            quantity=qty,
            limit_price=contract.mid,
            mode=self.mode,
        )
        self._check_order_type(request.action)

        if self.mode == ExecutionMode.PROPOSE_ONLY:
            return self._propose(request)

        option_id = await self._resolve_option_id(contract)

        if self.mode == ExecutionMode.RH_APPROVAL:
            return await self._rh_approval(request, option_id)

        return await self._autonomous(request, option_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_order_type(self, action: str) -> None:
        """Enforce long-only constraint — sell_to_open is never allowed."""
        if action == "sell_to_open":
            raise ValueError(
                f"sell_to_open is prohibited (long-only constraint); action={action!r}"
            )

    def _propose(self, request: OrderRequest) -> OrderResult:
        logger.info(
            "PROPOSE_ONLY %s %s qty=%d limit=%s",
            request.candidate.ticker,
            request.action,
            request.quantity,
            request.limit_price,
        )
        return OrderResult(
            request=request,
            placed=False,
            timestamp=datetime.now(timezone.utc),
        )

    async def _resolve_option_id(self, contract: OptionContract) -> str:
        """Resolve the RH option instrument UUID from contract fields."""
        result = await rh_call(self.rh_tools, "get_option_instruments", {
            "chain_symbol": contract.ticker,
            "expiration_dates": contract.expiry.isoformat(),
            "strike_price": f"{float(contract.strike):.4f}",
            "type": contract.type,
            "state": "active",
        })
        instruments: list = []
        if isinstance(result, dict):
            instruments = result.get("results", result.get("data", []))
        elif isinstance(result, list):
            instruments = result
        if not instruments:
            raise ValueError(
                f"No active option instrument: "
                f"{contract.ticker} {contract.expiry} {contract.strike} {contract.type}"
            )
        return instruments[0]["id"]

    def _build_order_params(self, request: OrderRequest, option_id: str) -> dict[str, Any]:
        """Build the parameter dict shared by review_option_order and place_option_order."""
        contract = request.candidate.selected_contract
        side = "buy" if request.action == "buy_to_open" else "sell"
        position_effect = "open" if request.action.endswith("_to_open") else "close"
        params: dict[str, Any] = {
            "account_number": self.account_number,
            "quantity": str(request.quantity),
            "legs": [{"option_id": option_id, "side": side, "position_effect": position_effect}],
            "type": "limit",
            "time_in_force": "gfd",
        }
        if request.limit_price is not None:
            params["price"] = f"{float(request.limit_price):.2f}"
        if contract is not None:
            params["chain_symbol"] = contract.ticker
            params["underlying_type"] = "equity"
        params["ref_id"] = request.ref_id
        return params

    async def _rh_approval(self, request: OrderRequest, option_id: str) -> OrderResult:
        """
        Review the order with RH, interrupt for explicit human approval, then place.

        The interrupt() call suspends the LangGraph graph at this node.
        The graph must be resumed with the human's response ('approve' to proceed,
        anything else to reject). Requires a LangGraph checkpointer to be configured.
        """
        params = self._build_order_params(request, option_id)

        review_result = await rh_call(self.rh_tools, "review_option_order", params)
        review_summary = _summarize_review(review_result)
        logger.info("%s rh_approval review: %s", request.candidate.ticker, review_summary)

        # Suspend the graph; human responds 'approve' or provides a rejection reason
        decision: str = interrupt({
            "type": "rh_order_review",
            "ticker": request.candidate.ticker,
            "action": request.action,
            "quantity": request.quantity,
            "limit_price": str(request.limit_price),
            "review": review_result,
            "review_summary": review_summary,
            "prompt": "Respond 'approve' to place this order, or anything else to reject.",
        })

        if str(decision).strip().lower() != "approve":
            logger.info("%s rejected by human: %r", request.candidate.ticker, decision)
            return OrderResult(
                request=request,
                placed=False,
                rejection_reason=f"user_rejected: {decision}",
                review_summary=review_summary,
                timestamp=datetime.now(timezone.utc),
            )

        place_result = await rh_call(self.rh_tools, "place_option_order", params)
        order_id = _extract_order_id(place_result)
        logger.info("%s placed order_id=%s", request.candidate.ticker, order_id)
        return OrderResult(
            request=request,
            placed=True,
            order_id=order_id,
            review_summary=review_summary,
            timestamp=datetime.now(timezone.utc),
        )

    async def _autonomous(self, request: OrderRequest, option_id: str) -> OrderResult:
        """
        Review the order with RH and place immediately if no fatal alerts are found.
        No human confirmation step — runs fully within the pipeline.
        """
        params = self._build_order_params(request, option_id)

        review_result = await rh_call(self.rh_tools, "review_option_order", params)
        review_summary = _summarize_review(review_result)

        blocking = _get_blocking_alerts(review_result)
        if blocking:
            logger.warning("%s autonomous blocked: %s", request.candidate.ticker, blocking)
            return OrderResult(
                request=request,
                placed=False,
                rejection_reason=f"blocked_by_alerts: {'; '.join(blocking)}",
                review_summary=review_summary,
                timestamp=datetime.now(timezone.utc),
            )

        place_result = await rh_call(self.rh_tools, "place_option_order", params)
        order_id = _extract_order_id(place_result)
        logger.info("%s autonomous placed order_id=%s", request.candidate.ticker, order_id)
        return OrderResult(
            request=request,
            placed=True,
            order_id=order_id,
            review_summary=review_summary,
            timestamp=datetime.now(timezone.utc),
        )
