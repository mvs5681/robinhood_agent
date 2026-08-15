"""Intraday flow-alert capture — the real-timestamp counterpart to the
once-daily flow_alerts.json snapshot in data/history/.

get_flow_alerts is current-data-only (no historical date= filtering), so a
single end-of-day snapshot can be many hours stale by the time it's
captured relative to whatever it's later replayed against — confirmed
live: a 2026-08-14 capture held alerts timestamped 2026-08-12, always
outside FlowTrigger's 4h lookback (see CHANGELOG 2026-08-15's
bypass_flow_gate entry). The only way to ever recover real intraday flow
timing for backtest replay is to log alerts as they're actually seen.

This piggybacks on FlowWatcher's existing 60s poll — zero extra UW API
calls, just persisting more of what's already fetched.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trader.uw.schemas import FlowAlert

logger = logging.getLogger(__name__)


def _alert_key(alert: "FlowAlert") -> str:
    # Same composite key FlowWatcher._alert_key() already uses to dedup
    # against re-fetching the same print across consecutive polls — no
    # stable id field on FlowAlert, but this combination is what the
    # codebase already treats as the alert's identity.
    return f"{alert.ticker}:{alert.expiry}:{alert.strike}:{alert.type}:{alert.created_at}"


class FlowAlertCapture:
    """Appends newly-seen flow alerts to a per-day JSONL log, deduped by
    the same (ticker, expiry, strike, type, created_at) key FlowWatcher
    uses internally. Safe to call every poll — repeats of an alert still
    active in UW's returned window are silently dropped, not re-written.
    """

    def __init__(self, history_dir: str | Path = "data/history") -> None:
        self._root = Path(history_dir)
        self._seen: set[str] = set()
        self._seen_date: date | None = None

    def record(self, alerts: list["FlowAlert"]) -> int:
        """Append any not-yet-seen alerts to today's log. Returns the
        count actually written (0 if everything was already seen).

        Alerts are only marked seen after a successful write — if the write
        fails partway, none of this batch is marked seen, so a retried poll
        can pick them up again rather than silently losing them. A rare
        duplicate line from a partial-then-retried write is a much smaller
        problem than losing data.
        """
        today = date.today()
        if today != self._seen_date:
            self._seen.clear()
            self._seen_date = today

        candidates = [(a, _alert_key(a)) for a in alerts]
        new = [(a, key) for a, key in candidates if key not in self._seen]
        if not new:
            return 0

        try:
            day_dir = self._root / today.isoformat()
            day_dir.mkdir(parents=True, exist_ok=True)
            path = day_dir / "flow_alerts_intraday.jsonl"
            with path.open("a") as f:
                for a, _key in new:
                    f.write(json.dumps(a.model_dump(mode="json")) + "\n")
        except Exception as exc:
            logger.error("FlowAlertCapture: failed to persist %d alert(s): %s", len(new), exc)
            return 0

        for _a, key in new:
            self._seen.add(key)
        return len(new)
