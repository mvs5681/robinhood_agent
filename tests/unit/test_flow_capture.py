"""Unit tests for FlowAlertCapture — the intraday flow-alert log that
recovers real timestamps get_flow_alerts can't be replayed against
historically (see flow_capture.py's module docstring)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from trader.live.flow_capture import FlowAlertCapture
from trader.uw.schemas import FlowAlert


def _alert(ticker: str = "AAPL", created_at=None, strike: str = "200") -> FlowAlert:
    return FlowAlert(
        ticker=ticker, expiry=date(2026, 9, 18), strike=Decimal(strike),
        type="call", total_premium=Decimal("250000"), total_size=500,
        volume=3000, open_interest=10000, alert_rule="RepeatedHits",
        trade_count=12, underlying_price=Decimal("195"),
        created_at=created_at or datetime(2026, 8, 15, 14, 30, tzinfo=timezone.utc),
    )


class TestRecord:
    def test_writes_new_alert_to_todays_dir(self, tmp_path):
        capture = FlowAlertCapture(history_dir=tmp_path)
        with patch("trader.live.flow_capture.date") as mock_date:
            mock_date.today.return_value = date(2026, 8, 15)
            n = capture.record([_alert()])

        assert n == 1
        path = tmp_path / "2026-08-15" / "flow_alerts_intraday.jsonl"
        assert path.exists()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["ticker"] == "AAPL"
        assert row["strike"] == "200"

    def test_dedupes_identical_alert_across_calls(self, tmp_path):
        capture = FlowAlertCapture(history_dir=tmp_path)
        alert = _alert()
        with patch("trader.live.flow_capture.date") as mock_date:
            mock_date.today.return_value = date(2026, 8, 15)
            n1 = capture.record([alert])
            n2 = capture.record([alert])  # same poll returning the same print again

        assert n1 == 1
        assert n2 == 0
        path = tmp_path / "2026-08-15" / "flow_alerts_intraday.jsonl"
        assert len(path.read_text().strip().split("\n")) == 1

    def test_distinct_alerts_both_written(self, tmp_path):
        capture = FlowAlertCapture(history_dir=tmp_path)
        with patch("trader.live.flow_capture.date") as mock_date:
            mock_date.today.return_value = date(2026, 8, 15)
            n = capture.record([_alert(strike="200"), _alert(strike="210")])

        assert n == 2
        path = tmp_path / "2026-08-15" / "flow_alerts_intraday.jsonl"
        assert len(path.read_text().strip().split("\n")) == 2

    def test_same_ticker_different_created_at_both_written(self, tmp_path):
        capture = FlowAlertCapture(history_dir=tmp_path)
        a1 = _alert(created_at=datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc))
        a2 = _alert(created_at=datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc))
        with patch("trader.live.flow_capture.date") as mock_date:
            mock_date.today.return_value = date(2026, 8, 15)
            n = capture.record([a1, a2])

        assert n == 2

    def test_empty_alert_list_writes_nothing(self, tmp_path):
        capture = FlowAlertCapture(history_dir=tmp_path)
        with patch("trader.live.flow_capture.date") as mock_date:
            mock_date.today.return_value = date(2026, 8, 15)
            n = capture.record([])

        assert n == 0
        assert not (tmp_path / "2026-08-15").exists()

    def test_seen_set_resets_on_day_rollover(self, tmp_path):
        capture = FlowAlertCapture(history_dir=tmp_path)
        alert = _alert()
        with patch("trader.live.flow_capture.date") as mock_date:
            mock_date.today.return_value = date(2026, 8, 15)
            n1 = capture.record([alert])

            mock_date.today.return_value = date(2026, 8, 16)
            n2 = capture.record([alert])  # "same" alert, but a new day — not a dup anymore

        assert n1 == 1
        assert n2 == 1
        assert (tmp_path / "2026-08-15" / "flow_alerts_intraday.jsonl").exists()
        assert (tmp_path / "2026-08-16" / "flow_alerts_intraday.jsonl").exists()

    def test_write_failure_does_not_mark_alerts_seen(self, tmp_path):
        # A history_dir that can't be created (e.g. permission denied) must
        # not silently swallow the alert — it should be retryable next poll,
        # not marked seen-but-never-persisted.
        capture = FlowAlertCapture(history_dir=tmp_path / "unwritable")
        alert = _alert()
        with patch("trader.live.flow_capture.date") as mock_date:
            mock_date.today.return_value = date(2026, 8, 15)
            with patch("pathlib.Path.mkdir", side_effect=PermissionError("denied")):
                n1 = capture.record([alert])
            # Retried on the next poll, after whatever blocked mkdir clears
            n2 = capture.record([alert])

        assert n1 == 0
        assert n2 == 1

    def test_appends_across_multiple_calls_same_day(self, tmp_path):
        capture = FlowAlertCapture(history_dir=tmp_path)
        with patch("trader.live.flow_capture.date") as mock_date:
            mock_date.today.return_value = date(2026, 8, 15)
            capture.record([_alert(strike="200")])
            capture.record([_alert(strike="210")])

        path = tmp_path / "2026-08-15" / "flow_alerts_intraday.jsonl"
        assert len(path.read_text().strip().split("\n")) == 2
