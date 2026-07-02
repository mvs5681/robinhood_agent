"""HTTP approval server — exposes pending proposals and accepts approve/reject actions.

Endpoints:
    GET  /proposals          — list all pending proposals (JSON array)
    GET  /proposals/{id}     — get one proposal (JSON)
    POST /proposals/{id}/approve  — approve and immediately execute via Executor
    POST /proposals/{id}/reject   — reject with optional {"note": "..."} body
    GET  /health             — liveness check

The server runs in the same asyncio event loop as the scanner and watcher.
It is intentionally minimal — no auth on these endpoints, so you should
run behind a reverse proxy with auth (nginx + basic auth, or Fly.io private networking).
"""

from __future__ import annotations

import json
import logging
import time as _time
from typing import TYPE_CHECKING

from aiohttp import web

from trader.executor.schemas import ExecutionMode
from trader.telemetry.logger import TelemetryLogger

from .proposals import ProposalStore

if TYPE_CHECKING:
    from trader.executor.executor import Executor

logger = logging.getLogger(__name__)


def _json_response(data, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, default=str),
        content_type="application/json",
        status=status,
    )


def create_app(
    proposal_store: ProposalStore,
    executor: Executor,
    tel: TelemetryLogger | None = None,
) -> web.Application:
    app = web.Application()

    # ------------------------------------------------------------------ #
    # GET /health
    # ------------------------------------------------------------------ #

    async def health(_: web.Request) -> web.Response:
        return _json_response({"status": "ok"})

    # ------------------------------------------------------------------ #
    # GET /proposals
    # ------------------------------------------------------------------ #

    async def list_proposals(_: web.Request) -> web.Response:
        pending = await proposal_store.list_pending()
        return _json_response([p.summary() for p in pending])

    # ------------------------------------------------------------------ #
    # GET /proposals/{id}
    # ------------------------------------------------------------------ #

    async def get_proposal(request: web.Request) -> web.Response:
        pid = request.match_info["id"]
        proposal = await proposal_store.get(pid)
        if proposal is None:
            return _json_response({"error": "not found"}, status=404)
        return _json_response(proposal.summary())

    # ------------------------------------------------------------------ #
    # POST /proposals/{id}/approve
    # ------------------------------------------------------------------ #

    async def approve_proposal(request: web.Request) -> web.Response:
        pid = request.match_info["id"]
        proposal = await proposal_store.approve(pid)
        if proposal is None:
            return _json_response({"error": "not found or not pending"}, status=404)

        ticker = proposal.candidate.ticker
        logger.info("Human approved proposal %s for %s", pid, ticker)

        # Execute immediately via the executor in autonomous mode
        # (bypasses interrupt — human already gave approval via HTTP)
        try:
            t0 = _time.monotonic()
            # Temporarily use autonomous logic for the approved candidate
            saved_mode = executor.mode
            executor.mode = ExecutionMode.AUTONOMOUS
            result = await executor.execute(proposal.candidate)
            executor.mode = saved_mode
            ms = round((_time.monotonic() - t0) * 1000, 1)

            await proposal_store.mark_executed(pid, result)

            if tel:
                lp = result.request.limit_price
                tel.order_attempt(
                    ticker=ticker,
                    mode="rh_approval_via_http",
                    action=result.request.action,
                    quantity=result.request.quantity,
                    limit_price=float(lp) if lp is not None else None,
                    placed=result.placed,
                    order_id=result.order_id,
                    account_number=executor.account_number or None,
                    rejection_reason=result.rejection_reason,
                    review_summary=result.review_summary,
                    duration_ms=ms,
                )

            logger.info("%s order placed=%s order_id=%s", ticker, result.placed, result.order_id)
            return _json_response({
                "approved": True,
                "placed": result.placed,
                "order_id": result.order_id,
                "rejection_reason": result.rejection_reason,
                "review_summary": result.review_summary,
            })

        except Exception as exc:
            logger.error("Execute after approval failed for %s: %s", pid, exc)
            return _json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # POST /proposals/{id}/reject
    # ------------------------------------------------------------------ #

    async def reject_proposal(request: web.Request) -> web.Response:
        pid = request.match_info["id"]
        note = ""
        try:
            body = await request.json()
            note = body.get("note", "")
        except Exception:
            pass

        proposal = await proposal_store.reject(pid, note=note)
        if proposal is None:
            return _json_response({"error": "not found or not pending"}, status=404)

        logger.info("Human rejected proposal %s: %s", pid, note or "(no note)")
        return _json_response({"rejected": True, "proposal_id": pid, "note": note})

    # ------------------------------------------------------------------ #
    # Route wiring
    # ------------------------------------------------------------------ #

    app.router.add_get("/health", health)
    app.router.add_get("/proposals", list_proposals)
    app.router.add_get("/proposals/{id}", get_proposal)
    app.router.add_post("/proposals/{id}/approve", approve_proposal)
    app.router.add_post("/proposals/{id}/reject", reject_proposal)

    return app
