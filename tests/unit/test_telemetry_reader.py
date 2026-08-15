"""Unit tests for TelemetryReader.pnl_series() — the real-trade data source
backing both the P&L tab and the backtest-vs-reality comparison view."""

from __future__ import annotations

import json

from trader.live.telemetry_reader import TelemetryReader


def _write_events(path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _exit_event(**overrides) -> dict:
    ev = {
        "timestamp": "2026-08-15T20:00:00+00:00",
        "stage": "exit_signal",
        "ticker": "AAPL",
        "result": "ok",
        "reason": "profit_target",
        "duration_ms": 1.2,
        "pnl_pct": 0.42,
        "dte_remaining": 12,
        "entry_premium": 3.0,
        "current_premium": 4.26,
        "quantity": 2,
        "entry_regime": "positive",
        "entry_setup_type": "pin",
    }
    ev.update(overrides)
    return ev


class TestPnlSeries:
    def test_includes_quantity_and_regime_context(self, tmp_path):
        log = tmp_path / "telemetry.jsonl"
        _write_events(log, [_exit_event()])
        reader = TelemetryReader(log)

        series = reader.pnl_series()

        assert len(series) == 1
        row = series[0]
        assert row["ticker"] == "AAPL"
        assert row["pnl_pct"] == 0.42
        assert row["quantity"] == 2
        assert row["entry_regime"] == "positive"
        assert row["entry_setup_type"] == "pin"
        assert row["entry_premium"] == 3.0
        assert row["current_premium"] == 4.26

    def test_older_events_without_new_fields_degrade_to_none(self, tmp_path):
        # exit_signal events logged before this field existed have no
        # quantity/entry_regime/entry_setup_type keys at all.
        log = tmp_path / "telemetry.jsonl"
        old_event = _exit_event()
        for key in ("quantity", "entry_regime", "entry_setup_type"):
            del old_event[key]
        _write_events(log, [old_event])
        reader = TelemetryReader(log)

        row = reader.pnl_series()[0]
        assert row["quantity"] is None
        assert row["entry_regime"] is None
        assert row["entry_setup_type"] is None

    def test_ignores_non_exit_signal_events(self, tmp_path):
        log = tmp_path / "telemetry.jsonl"
        _write_events(log, [
            {"timestamp": "2026-08-15T20:00:00+00:00", "stage": "risk_check",
             "ticker": "AAPL", "result": "ok"},
            _exit_event(),
        ])
        reader = TelemetryReader(log)
        assert len(reader.pnl_series()) == 1

    def test_empty_log_returns_empty_list(self, tmp_path):
        log = tmp_path / "telemetry.jsonl"
        log.write_text("")
        reader = TelemetryReader(log)
        assert reader.pnl_series() == []

    def test_no_log_file_returns_empty_list(self, tmp_path):
        reader = TelemetryReader(tmp_path / "does_not_exist.jsonl")
        assert reader.pnl_series() == []
