"""Phase 9 — Structured telemetry for the GEX trading pipeline.

Every pipeline stage emits a JSON event. Events are written via Python's
standard logging (so they respect the root log level and any configured
handlers) and optionally to a dedicated log file.

Configuration via environment variables:
    LOG_LEVEL           Python log level name (default: INFO)
    TELEMETRY_LOG_FILE  Absolute or relative path to write events (default: none)

Sensitive fields are masked automatically:
    account_number      → first 4 chars + ***
    order_id            → first 6 chars + ***

Usage (inside agent nodes):
    tel = TelemetryLogger()

    with tel.stage("gex_setup", ticker="AAPL") as span:
        setup = detector.detect(...)
        span.ok(regime=setup.regime.value, confidence=setup.structure_confidence)

    # Or fire-and-forget:
    tel.emit("flow_check", ticker="AAPL", result="skipped", reason="no whale print")
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Generator

# ---------------------------------------------------------------------------
# Module-level log setup
# ---------------------------------------------------------------------------

_TELEMETRY_LOG_NAME = "trader.telemetry"
_tlog = logging.getLogger(_TELEMETRY_LOG_NAME)

_SENSITIVE_KEYS = frozenset({"account_number", "account_id", "order_id"})

# Mask lengths: show only first N chars, pad with ***
_MASK_SHOW = {"account_number": 4, "account_id": 4, "order_id": 6}


def _mask(value: str, key: str) -> str:
    n = _MASK_SHOW.get(key, 4)
    if len(value) <= n:
        return "***"
    return value[:n] + "***"


def _sanitize(payload: dict[str, Any]) -> dict[str, Any]:
    """Recursively mask sensitive keys in an event payload."""
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k in _SENSITIVE_KEYS and isinstance(v, str) and v:
            out[k] = _mask(v, k)
        elif isinstance(v, dict):
            out[k] = _sanitize(v)
        else:
            out[k] = v
    return out


def _to_json(obj: Any) -> Any:
    """JSON-serialise types the default encoder cannot handle."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value"):       # Enum
        return obj.value
    if hasattr(obj, "__dict__"):    # dataclass / arbitrary obj
        return str(obj)
    return str(obj)


# ---------------------------------------------------------------------------
# Span — tracks duration within a `with tel.stage(...)` block
# ---------------------------------------------------------------------------


@dataclass
class _Span:
    stage: str
    ticker: str
    _logger: "TelemetryLogger"
    _started: float = field(default_factory=time.monotonic, init=False)
    _closed: bool = field(default=False, init=False)

    def ok(self, **extra: Any) -> None:
        """Emit a success event from inside the span."""
        self._emit("ok", reason=None, **extra)

    def skip(self, reason: str, **extra: Any) -> None:
        """Emit a skip/rejection event from inside the span."""
        self._emit("skipped", reason=reason, **extra)

    def error(self, exc: Exception, **extra: Any) -> None:
        """Emit an error event from inside the span."""
        self._emit("error", reason=str(exc), **extra)

    def _emit(self, result: str, reason: str | None, **extra: Any) -> None:
        if self._closed:
            return
        self._closed = True
        ms = (time.monotonic() - self._started) * 1000
        self._logger.emit(
            self.stage,
            ticker=self.ticker,
            result=result,
            reason=reason,
            duration_ms=round(ms, 1),
            **extra,
        )

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        # If span was never closed by the caller, auto-emit on exception or completion
        if not self._closed:
            if exc_type is not None:
                self.error(exc_val)
            else:
                self.ok()
        return False  # never suppress exceptions


# ---------------------------------------------------------------------------
# TelemetryLogger
# ---------------------------------------------------------------------------


class TelemetryLogger:
    """
    Emits structured JSON telemetry events for every pipeline stage.

    Each event has at minimum:
        timestamp   ISO-8601 UTC string
        stage       pipeline stage name
        ticker      equity symbol (empty string for market-wide events)
        result      "ok" | "skipped" | "error"
        reason      why a candidate was skipped/rejected (null on ok)
        duration_ms wall time for this stage in milliseconds (null if unknown)

    Stage-specific fields are passed as **kwargs to emit().
    """

    STAGES = frozenset({
        "uw_fetch",
        "gex_setup",
        "blend_score",
        "flow_check",
        "contract_select",
        "risk_check",
        "order_attempt",
        "exit_signal",
    })

    def __init__(
        self,
        log_level: str | None = None,
        log_file: str | Path | None = None,
    ) -> None:
        level_name = (log_level or os.environ.get("LOG_LEVEL", "INFO")).upper()
        self._level = getattr(logging, level_name, logging.INFO)

        self._file: TextIOWrapper | None = None
        file_path = log_file or os.environ.get("TELEMETRY_LOG_FILE")
        if file_path:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8")  # noqa: SIM115

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None

    def __del__(self) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Core emit
    # ------------------------------------------------------------------

    def emit(
        self,
        stage: str,
        *,
        ticker: str = "",
        result: str = "ok",
        reason: str | None = None,
        duration_ms: float | None = None,
        **extra: Any,
    ) -> None:
        """Write one structured JSON event."""
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "ticker": ticker,
            "result": result,
            "reason": reason,
            "duration_ms": duration_ms,
        }
        payload.update(extra)
        payload = _sanitize(payload)

        line = json.dumps(payload, default=_to_json)
        _tlog.log(self._level, line)

        if self._file:
            self._file.write(line + "\n")
            self._file.flush()

    # ------------------------------------------------------------------
    # Span context manager
    # ------------------------------------------------------------------

    @contextmanager
    def stage(self, stage_name: str, ticker: str = "") -> Generator[_Span, None, None]:
        """Context manager that auto-times and emits on exit if not already closed."""
        span = _Span(stage=stage_name, ticker=ticker, _logger=self)
        try:
            yield span
        except Exception as exc:
            if not span._closed:
                span.error(exc)
            raise
        else:
            if not span._closed:
                span.ok()

    # ------------------------------------------------------------------
    # Stage-specific helpers (typed payloads for each pipeline stage)
    # ------------------------------------------------------------------

    def uw_fetch(
        self,
        *,
        ticker: str = "",
        endpoint: str,
        record_count: int,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        self.emit(
            "uw_fetch",
            ticker=ticker,
            result="error" if error else "ok",
            reason=error,
            duration_ms=duration_ms,
            endpoint=endpoint,
            record_count=record_count,
        )

    def gex_setup(
        self,
        *,
        ticker: str,
        regime: str,
        direction: str,
        setup_type: str,
        confidence: float,
        flip_point: float | None,
        target_level: float | None,
        duration_ms: float,
        skipped: bool = False,
        reason: str | None = None,
    ) -> None:
        self.emit(
            "gex_setup",
            ticker=ticker,
            result="skipped" if skipped else "ok",
            reason=reason,
            duration_ms=duration_ms,
            regime=regime,
            direction=direction,
            setup_type=setup_type,
            confidence=confidence,
            flip_point=flip_point,
            target_level=target_level,
        )

    def blend_score(
        self,
        *,
        ticker: str,
        composite: float,
        market_tide: float,
        darkpool: float,
        flow_pressure: float,
        iv_cost: float,
        technicals: float,
        rank: int,
        duration_ms: float,
    ) -> None:
        self.emit(
            "blend_score",
            ticker=ticker,
            result="ok",
            duration_ms=duration_ms,
            composite=composite,
            scores={
                "market_tide": market_tide,
                "darkpool": darkpool,
                "flow_pressure": flow_pressure,
                "iv_cost": iv_cost,
                "technicals": technicals,
            },
            rank=rank,
        )

    def flow_check(
        self,
        *,
        ticker: str,
        confirmed: bool,
        direction: str,
        alert_premium: float | None,
        duration_ms: float,
    ) -> None:
        self.emit(
            "flow_check",
            ticker=ticker,
            result="ok" if confirmed else "skipped",
            reason=None if confirmed else "no matching whale print",
            duration_ms=duration_ms,
            confirmed=confirmed,
            direction=direction,
            alert_premium=alert_premium,
        )

    def contract_select(
        self,
        *,
        ticker: str,
        selected: bool,
        strike: float | None,
        expiry: str | None,
        delta: float | None,
        dte: int | None,
        spread_pct: float | None,
        duration_ms: float,
        reason: str | None = None,
    ) -> None:
        self.emit(
            "contract_select",
            ticker=ticker,
            result="ok" if selected else "skipped",
            reason=reason,
            duration_ms=duration_ms,
            selected=selected,
            strike=strike,
            expiry=expiry,
            delta=delta,
            dte=dte,
            spread_pct=spread_pct,
        )

    def risk_check(
        self,
        *,
        ticker: str,
        approved: bool,
        reasons: list[str],
        duration_ms: float,
    ) -> None:
        self.emit(
            "risk_check",
            ticker=ticker,
            result="ok" if approved else "skipped",
            reason="; ".join(reasons) if reasons else None,
            duration_ms=duration_ms,
            approved=approved,
            rejection_reasons=reasons,
        )

    def order_attempt(
        self,
        *,
        ticker: str,
        mode: str,
        action: str,
        quantity: int,
        limit_price: float | None,
        placed: bool,
        order_id: str | None,
        account_number: str | None,
        rejection_reason: str | None,
        review_summary: str | None,
        duration_ms: float,
    ) -> None:
        self.emit(
            "order_attempt",
            ticker=ticker,
            result="ok" if placed else "skipped",
            reason=rejection_reason,
            duration_ms=duration_ms,
            mode=mode,
            action=action,
            quantity=quantity,
            limit_price=limit_price,
            placed=placed,
            order_id=order_id,
            account_number=account_number,  # masked by _sanitize
            review_summary=review_summary,
        )

    def exit_signal(
        self,
        *,
        ticker: str,
        position_id: str,
        reason: str,
        pnl_pct: float,
        dte_remaining: int,
        entry_premium: float,
        current_premium: float,
        duration_ms: float,
    ) -> None:
        self.emit(
            "exit_signal",
            ticker=ticker,
            result="ok",
            reason=reason,
            duration_ms=duration_ms,
            position_id=position_id,
            pnl_pct=pnl_pct,
            dte_remaining=dte_remaining,
            entry_premium=entry_premium,
            current_premium=current_premium,
        )
