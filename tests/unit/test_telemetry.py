"""Unit tests for Phase 9 — TelemetryLogger."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from trader.telemetry.logger import TelemetryLogger, _mask, _sanitize


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_events(tel: TelemetryLogger, caplog, *, level=logging.INFO) -> list[dict]:
    """Return all JSON-parseable log lines emitted by tel within a caplog block."""
    lines = [r.getMessage() for r in caplog.records if r.name == "trader.telemetry"]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


class TestMasking:
    def test_mask_account_number_shows_first_4(self):
        assert _mask("ABCD1234", "account_number") == "ABCD***"

    def test_mask_short_value_is_fully_redacted(self):
        assert _mask("AB", "account_number") == "***"

    def test_mask_order_id_shows_first_6(self):
        assert _mask("ORD-123456789", "order_id") == "ORD-12***"

    def test_sanitize_masks_account_number_in_dict(self):
        payload = {"account_number": "ABCD1234", "ticker": "AAPL"}
        result = _sanitize(payload)
        assert result["account_number"] == "ABCD***"
        assert result["ticker"] == "AAPL"

    def test_sanitize_masks_nested_account_id(self):
        payload = {"order": {"account_id": "X123456789"}}
        result = _sanitize(payload)
        assert result["order"]["account_id"] == "X123***"

    def test_sanitize_leaves_non_sensitive_fields_alone(self):
        payload = {"ticker": "SPY", "stage": "gex_setup", "composite": 0.75}
        result = _sanitize(payload)
        assert result == payload

    def test_sanitize_ignores_empty_string(self):
        payload = {"account_number": ""}
        result = _sanitize(payload)
        assert result["account_number"] == ""

    def test_sanitize_ignores_non_string_value(self):
        payload = {"account_number": 12345}
        result = _sanitize(payload)
        assert result["account_number"] == 12345


# ---------------------------------------------------------------------------
# emit() — core serialisation
# ---------------------------------------------------------------------------


class TestEmit:
    def test_emit_produces_valid_json(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.emit("gex_setup", ticker="AAPL", result="ok", duration_ms=5.2)
        events = _capture_events(tel, caplog)
        assert len(events) == 1

    def test_emit_has_required_fields(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.emit("flow_check", ticker="AAPL", result="skipped", reason="no whale", duration_ms=1.1)
        events = _capture_events(tel, caplog)
        ev = events[0]
        assert ev["stage"] == "flow_check"
        assert ev["ticker"] == "AAPL"
        assert ev["result"] == "skipped"
        assert ev["reason"] == "no whale"
        assert ev["duration_ms"] == pytest.approx(1.1)
        assert "timestamp" in ev

    def test_emit_extra_kwargs_included(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.emit("blend_score", ticker="SPY", result="ok", duration_ms=2.0,
                     composite=0.72, rank=1)
        events = _capture_events(tel, caplog)
        ev = events[0]
        assert ev["composite"] == pytest.approx(0.72)
        assert ev["rank"] == 1

    def test_emit_masks_account_number(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.emit("order_attempt", ticker="AAPL", result="ok", duration_ms=10.0,
                     account_number="ABCD1234")
        events = _capture_events(tel, caplog)
        assert events[0]["account_number"] == "ABCD***"

    def test_emit_null_reason_is_serialised(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.emit("risk_check", ticker="AAPL", result="ok", duration_ms=0.5)
        events = _capture_events(tel, caplog)
        assert events[0]["reason"] is None

    def test_emit_decimal_converted_to_float(self, caplog):
        from decimal import Decimal
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.emit("contract_select", ticker="AAPL", result="ok", duration_ms=1.0,
                     limit_price=Decimal("3.15"))
        events = _capture_events(tel, caplog)
        assert isinstance(events[0]["limit_price"], float)
        assert events[0]["limit_price"] == pytest.approx(3.15)


# ---------------------------------------------------------------------------
# Log level configuration
# ---------------------------------------------------------------------------


class TestLogLevel:
    def test_default_level_is_info(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.emit("gex_setup", ticker="AAPL", result="ok", duration_ms=1.0)
        # Events at INFO should appear when caplog captures DEBUG+
        events = _capture_events(tel, caplog)
        assert len(events) == 1

    def test_log_level_env_var_debug(self, caplog):
        with patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}):
            with caplog.at_level(logging.DEBUG, logger="trader.telemetry"):
                tel = TelemetryLogger()
                tel.emit("uw_fetch", ticker="AAPL", result="ok", duration_ms=2.0,
                         endpoint="get_market_tide", record_count=10)
        events = _capture_events(tel, caplog)
        assert len(events) == 1

    def test_log_level_constructor_overrides_env(self, caplog):
        with patch.dict(os.environ, {"LOG_LEVEL": "WARNING"}):
            with caplog.at_level(logging.WARNING, logger="trader.telemetry"):
                tel = TelemetryLogger(log_level="WARNING")
                tel.emit("gex_setup", ticker="AAPL", result="ok", duration_ms=1.0)
        events = _capture_events(tel, caplog)
        assert len(events) == 1

    def test_high_log_level_suppresses_events(self, caplog):
        with caplog.at_level(logging.CRITICAL, logger="trader.telemetry"):
            tel = TelemetryLogger(log_level="INFO")
            tel.emit("gex_setup", ticker="AAPL", result="ok", duration_ms=1.0)
        # caplog is set to CRITICAL so INFO events should not appear
        events = _capture_events(tel, caplog)
        assert len(events) == 0


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


class TestFileOutput:
    def test_writes_to_file(self, tmp_path: Path):
        log_file = tmp_path / "telemetry.jsonl"
        tel = TelemetryLogger(log_file=str(log_file))
        tel.emit("gex_setup", ticker="AAPL", result="ok", duration_ms=1.5)
        tel.close()

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        ev = json.loads(lines[0])
        assert ev["stage"] == "gex_setup"

    def test_appends_multiple_events(self, tmp_path: Path):
        log_file = tmp_path / "events.jsonl"
        tel = TelemetryLogger(log_file=str(log_file))
        for i in range(3):
            tel.emit("uw_fetch", ticker="SPY", result="ok", duration_ms=float(i),
                     endpoint="get_market_tide", record_count=i)
        tel.close()

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_creates_parent_directory(self, tmp_path: Path):
        log_file = tmp_path / "nested" / "dir" / "tel.jsonl"
        tel = TelemetryLogger(log_file=str(log_file))
        tel.emit("flow_check", ticker="AAPL", result="ok", duration_ms=0.5, confirmed=True,
                 direction="call", alert_premium=None)
        tel.close()
        assert log_file.exists()

    def test_env_var_telemetry_log_file(self, tmp_path: Path):
        log_file = tmp_path / "env_tel.jsonl"
        with patch.dict(os.environ, {"TELEMETRY_LOG_FILE": str(log_file)}):
            tel = TelemetryLogger()
            tel.emit("blend_score", ticker="AAPL", result="ok", duration_ms=1.0,
                     composite=0.6, market_tide=0.5, darkpool=0.6,
                     flow_pressure=0.7, iv_cost=0.6, technicals=0.5, rank=1)
            tel.close()
        assert log_file.exists()
        ev = json.loads(log_file.read_text().strip())
        assert ev["stage"] == "blend_score"


# ---------------------------------------------------------------------------
# Stage context manager
# ---------------------------------------------------------------------------


class TestStageContextManager:
    def test_span_ok_emits_event(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            with tel.stage("gex_setup", ticker="AAPL") as span:
                span.ok(regime="positive", confidence=0.85)
        events = _capture_events(tel, caplog)
        assert len(events) == 1
        ev = events[0]
        assert ev["result"] == "ok"
        assert ev["regime"] == "positive"

    def test_span_skip_emits_skipped(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            with tel.stage("flow_check", ticker="SPY") as span:
                span.skip("no matching alert")
        events = _capture_events(tel, caplog)
        ev = events[0]
        assert ev["result"] == "skipped"
        assert ev["reason"] == "no matching alert"

    def test_span_error_on_exception(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            with pytest.raises(ValueError):
                with tel.stage("risk_check", ticker="AAPL") as span:
                    raise ValueError("boom")
        events = _capture_events(tel, caplog)
        ev = events[0]
        assert ev["result"] == "error"
        assert "boom" in ev["reason"]

    def test_span_auto_ok_if_no_explicit_close(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            with tel.stage("contract_select", ticker="AAPL"):
                pass  # no span.ok() call
        events = _capture_events(tel, caplog)
        assert events[0]["result"] == "ok"

    def test_span_duration_ms_is_positive(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            with tel.stage("gex_setup", ticker="AAPL") as span:
                time.sleep(0.01)
                span.ok()
        events = _capture_events(tel, caplog)
        assert events[0]["duration_ms"] > 0

    def test_span_not_double_emitted(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            with tel.stage("blend_score", ticker="AAPL") as span:
                span.ok(composite=0.7)
                span.ok(composite=0.8)   # second call ignored
        events = _capture_events(tel, caplog)
        assert len(events) == 1
        assert events[0]["composite"] == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Stage-specific helpers
# ---------------------------------------------------------------------------


class TestStageHelpers:
    def test_uw_fetch_ok(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.uw_fetch(ticker="AAPL", endpoint="get_flow_alerts",
                         record_count=42, duration_ms=12.3)
        ev = _capture_events(tel, caplog)[0]
        assert ev["stage"] == "uw_fetch"
        assert ev["result"] == "ok"
        assert ev["record_count"] == 42
        assert ev["endpoint"] == "get_flow_alerts"

    def test_uw_fetch_error(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.uw_fetch(endpoint="get_market_tide", record_count=0,
                         duration_ms=5.0, error="timeout")
        ev = _capture_events(tel, caplog)[0]
        assert ev["result"] == "error"
        assert ev["reason"] == "timeout"

    def test_gex_setup_ok(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.gex_setup(ticker="AAPL", regime="positive", direction="call",
                          setup_type="pin", confidence=0.82,
                          flip_point=192.5, target_level=200.0, duration_ms=3.1)
        ev = _capture_events(tel, caplog)[0]
        assert ev["stage"] == "gex_setup"
        assert ev["regime"] == "positive"
        assert ev["confidence"] == pytest.approx(0.82)
        assert ev["result"] == "ok"

    def test_gex_setup_skipped(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.gex_setup(ticker="AAPL", regime="mixed", direction="none",
                          setup_type="none", confidence=0.1,
                          flip_point=None, target_level=None, duration_ms=2.0,
                          skipped=True, reason="mixed regime")
        ev = _capture_events(tel, caplog)[0]
        assert ev["result"] == "skipped"
        assert ev["reason"] == "mixed regime"

    def test_blend_score_includes_all_components(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.blend_score(ticker="SPY", composite=0.65, market_tide=0.6,
                            darkpool=0.7, flow_pressure=0.5, iv_cost=0.8,
                            technicals=0.55, rank=2, duration_ms=1.5)
        ev = _capture_events(tel, caplog)[0]
        assert ev["scores"]["market_tide"] == pytest.approx(0.6)
        assert ev["scores"]["darkpool"] == pytest.approx(0.7)
        assert ev["scores"]["iv_cost"] == pytest.approx(0.8)
        assert ev["rank"] == 2

    def test_flow_check_confirmed(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.flow_check(ticker="AAPL", confirmed=True, direction="call",
                           alert_premium=250000.0, duration_ms=0.8)
        ev = _capture_events(tel, caplog)[0]
        assert ev["result"] == "ok"
        assert ev["confirmed"] is True
        assert ev["alert_premium"] == pytest.approx(250000.0)

    def test_flow_check_not_confirmed(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.flow_check(ticker="AAPL", confirmed=False, direction="call",
                           alert_premium=None, duration_ms=0.3)
        ev = _capture_events(tel, caplog)[0]
        assert ev["result"] == "skipped"
        assert ev["reason"] == "no matching whale print"

    def test_contract_select_selected(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.contract_select(ticker="AAPL", selected=True, strike=200.0,
                                expiry="2026-01-30", delta=0.38, dte=28,
                                spread_pct=0.067, duration_ms=2.2)
        ev = _capture_events(tel, caplog)[0]
        assert ev["result"] == "ok"
        assert ev["strike"] == pytest.approx(200.0)
        assert ev["delta"] == pytest.approx(0.38)

    def test_contract_select_none(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.contract_select(ticker="AAPL", selected=False, strike=None,
                                expiry=None, delta=None, dte=None,
                                spread_pct=None, duration_ms=1.0,
                                reason="no contract in delta band")
        ev = _capture_events(tel, caplog)[0]
        assert ev["result"] == "skipped"
        assert ev["reason"] == "no contract in delta band"

    def test_risk_check_approved(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.risk_check(ticker="AAPL", approved=True, reasons=[], duration_ms=0.1)
        ev = _capture_events(tel, caplog)[0]
        assert ev["result"] == "ok"
        assert ev["approved"] is True
        assert ev["rejection_reasons"] == []

    def test_risk_check_rejected(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.risk_check(ticker="AAPL", approved=False,
                           reasons=["position cap reached", "premium too high"],
                           duration_ms=0.2)
        ev = _capture_events(tel, caplog)[0]
        assert ev["result"] == "skipped"
        assert "position cap reached" in ev["reason"]
        assert len(ev["rejection_reasons"]) == 2

    def test_order_attempt_placed(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.order_attempt(ticker="AAPL", mode="autonomous",
                              action="buy_to_open", quantity=1, limit_price=3.10,
                              placed=True, order_id="ORD-ABCDEF1234",
                              account_number="ACCT9999",
                              rejection_reason=None, review_summary=None,
                              duration_ms=250.0)
        ev = _capture_events(tel, caplog)[0]
        assert ev["result"] == "ok"
        assert ev["placed"] is True
        assert ev["order_id"] == "ORD-AB***"   # masked
        assert ev["account_number"] == "ACCT***"  # masked

    def test_order_attempt_rejected(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.order_attempt(ticker="AAPL", mode="rh_approval",
                              action="buy_to_open", quantity=1, limit_price=3.10,
                              placed=False, order_id=None,
                              account_number=None,
                              rejection_reason="user declined",
                              review_summary="review ok",
                              duration_ms=5000.0)
        ev = _capture_events(tel, caplog)[0]
        assert ev["result"] == "skipped"
        assert ev["reason"] == "user declined"
        assert ev["review_summary"] == "review ok"

    def test_exit_signal(self, caplog):
        with caplog.at_level(logging.INFO, logger="trader.telemetry"):
            tel = TelemetryLogger()
            tel.exit_signal(ticker="AAPL", position_id="pos-001",
                            reason="profit_target", pnl_pct=0.583,
                            dte_remaining=25, entry_premium=3.0,
                            current_premium=4.75, duration_ms=0.5)
        ev = _capture_events(tel, caplog)[0]
        assert ev["stage"] == "exit_signal"
        assert ev["reason"] == "profit_target"
        assert ev["pnl_pct"] == pytest.approx(0.583)
        assert ev["entry_premium"] == pytest.approx(3.0)
        assert ev["current_premium"] == pytest.approx(4.75)
