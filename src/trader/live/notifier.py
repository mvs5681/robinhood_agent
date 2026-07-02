"""Telegram bot for trade proposal notifications and approvals.

Sends an inline-keyboard message when the watcher creates a new proposal.
The user taps Approve or Reject directly in Telegram; the bot updates the
message with the outcome and, on approval, calls Executor to place the order.

Setup:
1. Message @BotFather → /newbot → copy the token
2. Start a chat with your new bot (send any message)
3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates → copy chat.id
4. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
5. Set PUBLIC_URL to your container's public address (e.g. https://myagent.fly.dev)
   — used only as a fallback link; approval works without it
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from trader.executor.executor import Executor
    from trader.telemetry.logger import TelemetryLogger
    from .proposals import Proposal, ProposalStore

logger = logging.getLogger(__name__)

_BASE = "https://api.telegram.org/bot{token}/{method}"
_LONG_POLL_TIMEOUT = 30   # seconds for getUpdates long-poll
_RETRY_SLEEP = 5          # seconds to wait after a network error


def _url(token: str, method: str) -> str:
    return _BASE.format(token=token, method=method)


class TelegramNotifier:
    """
    Two responsibilities:
    - notify_proposal(): send a message with Approve/Reject inline buttons
    - run_poller(): long-poll getUpdates and handle button taps
    """

    def __init__(self, bot_token: str, chat_id: str, public_url: str = "") -> None:
        self._token = bot_token
        self._chat_id = str(chat_id)
        self._public_url = public_url.rstrip("/")
        # proposal_id → Telegram message_id (so we can edit on approval/rejection)
        self._msg_ids: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def notify_proposal(self, proposal: Proposal) -> None:
        """Send notification message with Approve / Reject inline buttons."""
        text = self._proposal_text(proposal, status="pending")
        keyboard = {
            "inline_keyboard": [[
                {"text": "Approve", "callback_data": f"approve:{proposal.proposal_id}"},
                {"text": "Reject",  "callback_data": f"reject:{proposal.proposal_id}"},
            ]]
        }
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": keyboard,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(self._token, "sendMessage"),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    body = await resp.json()
                    if body.get("ok"):
                        msg_id = body["result"]["message_id"]
                        self._msg_ids[proposal.proposal_id] = msg_id
                        logger.debug("Telegram notification sent: proposal=%s msg=%d",
                                     proposal.proposal_id, msg_id)
                    else:
                        logger.warning("Telegram sendMessage error: %s", body)
        except Exception as exc:
            logger.warning("Failed to send Telegram notification: %s", exc)

    async def _edit_message(self, proposal_id: str, text: str) -> None:
        """Edit the original notification message in-place."""
        msg_id = self._msg_ids.get(proposal_id)
        if not msg_id:
            return
        payload = {
            "chat_id": self._chat_id,
            "message_id": msg_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []},   # remove buttons
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(self._token, "editMessageText"),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    body = await resp.json()
                    if not body.get("ok"):
                        logger.warning("Telegram editMessageText error: %s", body)
        except Exception as exc:
            logger.warning("Failed to edit Telegram message: %s", exc)

    async def _answer_callback(self, callback_query_id: str, text: str = "") -> None:
        """Dismiss the button-loading indicator."""
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    _url(self._token, "answerCallbackQuery"),
                    json={"callback_query_id": callback_query_id, "text": text},
                    timeout=aiohttp.ClientTimeout(total=5),
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Incoming poller
    # ------------------------------------------------------------------

    async def run_poller(
        self,
        proposal_store: ProposalStore,
        executor: Executor,
        tel: TelemetryLogger | None = None,
    ) -> None:
        """Long-poll Telegram getUpdates and handle Approve/Reject callbacks."""
        logger.info("Telegram poller started")
        offset = 0
        while True:
            try:
                updates = await self._get_updates(offset)
            except Exception as exc:
                logger.warning("Telegram getUpdates error: %s", exc)
                await asyncio.sleep(_RETRY_SLEEP)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await self._handle_update(update, proposal_store, executor, tel)
                except Exception as exc:
                    logger.error("Error handling Telegram update %d: %s",
                                 update.get("update_id"), exc)

    async def _get_updates(self, offset: int) -> list[dict]:
        params = {
            "offset": offset,
            "timeout": _LONG_POLL_TIMEOUT,
            "allowed_updates": ["callback_query"],
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _url(self._token, "getUpdates"),
                params=params,
                timeout=aiohttp.ClientTimeout(total=_LONG_POLL_TIMEOUT + 10),
            ) as resp:
                body = await resp.json()
                if not body.get("ok"):
                    raise RuntimeError(f"getUpdates error: {body}")
                return body.get("result", [])

    async def _handle_update(
        self,
        update: dict,
        proposal_store: ProposalStore,
        executor: Executor,
        tel: TelemetryLogger | None,
    ) -> None:
        cb = update.get("callback_query")
        if not cb:
            return

        data: str = cb.get("data", "")
        cb_id: str = cb["id"]

        if ":" not in data:
            await self._answer_callback(cb_id, "Unknown action")
            return

        action, proposal_id = data.split(":", 1)

        if action == "reject":
            proposal = await proposal_store.reject(proposal_id, note="Rejected via Telegram")
            if proposal is None:
                await self._answer_callback(cb_id, "Proposal not found or already expired")
                return
            await self._answer_callback(cb_id, "Rejected")
            await self._edit_message(
                proposal_id,
                self._proposal_text(proposal, status="rejected"),
            )
            logger.info("Proposal %s rejected via Telegram", proposal_id)

        elif action == "approve":
            proposal = await proposal_store.approve(proposal_id)
            if proposal is None:
                await self._answer_callback(cb_id, "Proposal not found or already expired")
                return

            await self._answer_callback(cb_id, "Approved — placing order...")
            await self._edit_message(
                proposal_id,
                self._proposal_text(proposal, status="executing"),
            )
            logger.info("Proposal %s approved via Telegram — executing", proposal_id)

            t0 = _time.monotonic()
            try:
                result = await executor.execute(proposal.candidate)
                ms = round((_time.monotonic() - t0) * 1000, 1)
                if tel:
                    c = proposal.candidate
                    sc = c.selected_contract
                    lp = sc.mid_price if sc else None
                    tel.order_attempt(
                        ticker=c.ticker,
                        mode="rh_approval",
                        action="buy",
                        quantity=1,
                        limit_price=float(lp) if lp is not None else None,
                        placed=result.placed,
                        order_id=result.order_id,
                        duration_ms=ms,
                    )
                await proposal_store.mark_executed(proposal_id, result)
                status_text = "executed" if result.placed else f"rejected — {result.rejection_reason}"
                await self._edit_message(
                    proposal_id,
                    self._proposal_text(proposal, status=status_text),
                )
                logger.info("Proposal %s executed placed=%s order_id=%s",
                            proposal_id, result.placed, result.order_id)
            except Exception as exc:
                logger.error("Execute failed for proposal %s: %s", proposal_id, exc)
                await self._edit_message(
                    proposal_id,
                    self._proposal_text(proposal, status=f"error — {exc}"),
                )

    # ------------------------------------------------------------------
    # Message text helper
    # ------------------------------------------------------------------

    def _proposal_text(self, proposal: Proposal, *, status: str) -> str:
        c = proposal.candidate
        sc = c.selected_contract
        score = c.blend_scores.composite if c.blend_scores else 0.0
        regime = c.gex_setup.regime if c.gex_setup else "—"
        direction = c.gex_setup.direction if c.gex_setup else "—"
        strike = sc.strike if sc else "?"
        expiry = sc.expiry if sc else "?"
        delta = f"{sc.delta:.2f}" if sc and sc.delta is not None else "—"
        limit = f"${sc.mid_price:.2f}" if sc and sc.mid_price is not None else "—"

        status_icons = {
            "pending":   "Awaiting approval",
            "approved":  "Approved",
            "executing": "Placing order...",
            "rejected":  "Rejected",
            "executed":  "Order placed",
        }
        status_line = status_icons.get(status, status)

        dashboard = f'\n<a href="{self._public_url}/">Open dashboard</a>' if self._public_url else ""

        return (
            f"<b>Trade proposal</b>  [{status_line}]\n"
            f"\n"
            f"<b>{c.ticker}</b>  {direction}  {regime}\n"
            f"Strike <code>{strike}</code>  Exp <code>{expiry}</code>  Δ {delta}\n"
            f"Limit {limit}  ·  Score <b>{score:.2f}</b>\n"
            f"ID: <code>{proposal.proposal_id}</code>"
            f"{dashboard}"
        )
