"""TelemetryReader — tail-reads the JSONL telemetry log.

Each call to any public method re-reads any new lines appended since the last
read, so the data is always fresh without re-parsing the entire file.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# Pipeline stage order for the funnel view
FUNNEL_STAGES = [
    "uw_fetch",
    "gex_setup",
    "blend_score",
    "flow_check",
    "contract_select",
    "risk_check",
    "order_attempt",
]

# Stages that form one decision (excludes uw_fetch which is market-wide)
DECISION_STAGES = [
    "gex_setup",
    "blend_score",
    "flow_check",
    "contract_select",
    "risk_check",
    "order_attempt",
]

_INTERNAL_KEYS = frozenset({"_ts"})


def _build_decision(run_id: str, events: list[dict]) -> dict:
    ticker = next((e.get("ticker") for e in events if e.get("ticker")), "")
    first_ts = events[0].get("_ts")
    timestamp = first_ts.isoformat() if first_ts else events[0].get("timestamp", "")

    stages: dict[str, dict] = {}
    for ev in events:
        stage = ev.get("stage", "")
        if stage in DECISION_STAGES:
            stages[stage] = {k: v for k, v in ev.items() if k not in _INTERNAL_KEYS}

    # Determine outcome from first failed stage
    outcome = "proposed"
    for stage_name in DECISION_STAGES:
        s = stages.get(stage_name, {})
        result = s.get("result", "")
        if result == "error":
            outcome = f"error_{stage_name}"
            break
        if result == "skipped":
            outcome = f"skipped_{stage_name}"
            break

    # Override if order was actually placed
    order = stages.get("order_attempt", {})
    if order.get("placed"):
        outcome = "executed"

    return {
        "run_id": run_id,
        "ticker": ticker,
        "timestamp": timestamp,
        "outcome": outcome,
        "stages": stages,
    }


class TelemetryReader:
    """Reads and caches telemetry events from a JSONL log file.

    Thread-safe: a reentrant lock guards the in-memory cache and file offset.
    """

    def __init__(self, log_file: str | Path | None = None) -> None:
        self._path: Path | None = Path(log_file) if log_file else None
        self._events: list[dict[str, Any]] = []
        self._file_offset: int = 0  # byte position for tail behaviour
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Read any new lines appended to the log file since last call."""
        if self._path is None or not self._path.exists():
            return

        with open(self._path, "r", encoding="utf-8") as fh:
            fh.seek(self._file_offset)
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                    # Normalise timestamp to aware datetime object
                    ts_raw = event.get("timestamp", "")
                    if ts_raw:
                        try:
                            dt = datetime.fromisoformat(ts_raw)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            event["_ts"] = dt
                        except ValueError:
                            event["_ts"] = None
                    else:
                        event["_ts"] = None
                    self._events.append(event)
                except json.JSONDecodeError:
                    pass  # skip malformed lines
            self._file_offset = fh.tell()

    def _get_events(self) -> list[dict[str, Any]]:
        """Return the up-to-date list of all events (caller holds lock)."""
        self._refresh()
        return self._events

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def summary_last_hour(self) -> dict[str, Any]:
        """Counts of ok/skipped/error events in the last 60 minutes.

        Returns:
            {
                "by_result": {"ok": int, "skipped": int, "error": int},
                "by_stage": {stage: {"ok": int, "skipped": int, "error": int}, ...},
                "total": int,
            }
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        by_result: dict[str, int] = defaultdict(int)
        by_stage: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        with self._lock:
            events = self._get_events()
            for ev in events:
                ts = ev.get("_ts")
                if ts is None or ts < cutoff:
                    continue
                result = ev.get("result", "")
                stage = ev.get("stage", "")
                by_result[result] += 1
                by_stage[stage][result] += 1

        return {
            "by_result": {k: by_result[k] for k in ("ok", "skipped", "error")},
            "by_stage": {s: dict(v) for s, v in by_stage.items()},
            "total": sum(by_result.values()),
        }

    def funnel_today(self) -> list[dict[str, Any]]:
        """Pipeline funnel counts for the current calendar day (UTC).

        For each stage, counts unique tickers that reached that stage with an
        "ok" result (i.e. passed the gate).  The fallthrough uses the ordered
        FUNNEL_STAGES list so gaps are always visible.

        Returns:
            [{"stage": str, "ok": int, "skipped": int, "error": int, "tickers": [str]}, ...]
        """
        today = datetime.now(timezone.utc).date()
        # stage -> result -> set of tickers
        stage_tickers: dict[str, dict[str, set[str]]] = {
            s: {"ok": set(), "skipped": set(), "error": set()}
            for s in FUNNEL_STAGES
        }

        with self._lock:
            events = self._get_events()
            for ev in events:
                ts = ev.get("_ts")
                if ts is None or ts.date() != today:
                    continue
                stage = ev.get("stage", "")
                if stage not in stage_tickers:
                    continue
                result = ev.get("result", "")
                ticker = ev.get("ticker", "") or "__market__"
                bucket = stage_tickers[stage]
                if result in bucket:
                    bucket[result].add(ticker)

        out = []
        for s in FUNNEL_STAGES:
            bucket = stage_tickers[s]
            out.append({
                "stage": s,
                "ok": len(bucket["ok"]),
                "skipped": len(bucket["skipped"]),
                "error": len(bucket["error"]),
                "tickers": sorted(bucket["ok"]),
            })
        return out

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        """Return the last *n* telemetry events, most-recent first.

        The ``_ts`` internal key is serialised to an ISO string for JSON safety.
        """
        with self._lock:
            events = self._get_events()
            tail = events[-n:][::-1]

        out = []
        for ev in tail:
            item = {k: v for k, v in ev.items() if k != "_ts"}
            ts = ev.get("_ts")
            if ts is not None:
                item["timestamp"] = ts.isoformat()
            out.append(item)
        return out

    def decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        """Group pipeline events by run_id into structured decision objects.

        Returns most-recent decisions first. Events without a run_id are skipped
        (they predate the run_id feature or come from non-pipeline stages).
        """
        with self._lock:
            events = self._get_events()

        runs: dict[str, list[dict]] = defaultdict(list)
        for ev in events:
            rid = ev.get("run_id")
            if rid:
                runs[rid].append(ev)

        out = []
        for rid, evts in runs.items():
            evts_sorted = sorted(
                evts,
                key=lambda e: e.get("_ts") or datetime.min.replace(tzinfo=timezone.utc),
            )
            out.append(_build_decision(rid, evts_sorted))

        out.sort(key=lambda d: d["timestamp"], reverse=True)
        return out[:limit]

    def decision_detail(self, run_id: str) -> dict[str, Any] | None:
        """Return the full decision object for a single run_id."""
        with self._lock:
            events = self._get_events()

        evts = [e for e in events if e.get("run_id") == run_id]
        if not evts:
            return None
        evts_sorted = sorted(
            evts,
            key=lambda e: e.get("_ts") or datetime.min.replace(tzinfo=timezone.utc),
        )
        return _build_decision(run_id, evts_sorted)

    def pnl_series(self) -> list[dict[str, Any]]:
        """Return all exit_signal events as a pnl_pct time series.

        Returns:
            [{"timestamp": str, "ticker": str, "pnl_pct": float, "reason": str}, ...]
        """
        out = []
        with self._lock:
            events = self._get_events()
            for ev in events:
                if ev.get("stage") != "exit_signal":
                    continue
                ts = ev.get("_ts")
                out.append({
                    "timestamp": ts.isoformat() if ts else ev.get("timestamp", ""),
                    "ticker": ev.get("ticker", ""),
                    "pnl_pct": ev.get("pnl_pct"),
                    "reason": ev.get("reason", ""),
                })
        return out
