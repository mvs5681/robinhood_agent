"""Tests for the runtime-tunable LiveConfig."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from trader.live.config import LiveConfig


class TestDefaults:
    def test_env_defaults(self, monkeypatch):
        for var in ("DISCOVERY_MIN_PREMIUM", "MAX_DISCOVERED_TICKERS", "FLOW_MIN_PREMIUM",
                    "STOP_LOSS_PCT", "DTE_FLOOR", "TICKERS"):
            monkeypatch.delenv(var, raising=False)
        cfg = LiveConfig.from_env()
        assert cfg.discovery_min_premium == Decimal("250000")
        assert cfg.max_discovered_tickers == 20
        assert cfg.seed_tickers == []

    def test_env_values_used(self, monkeypatch):
        monkeypatch.setenv("DISCOVERY_MIN_PREMIUM", "100000")
        monkeypatch.setenv("TICKERS", "spy, qqq")
        cfg = LiveConfig.from_env()
        assert cfg.discovery_min_premium == Decimal("100000")
        assert cfg.seed_tickers == ["SPY", "QQQ"]


class TestUpdate:
    def test_valid_update_applies(self):
        cfg = LiveConfig()
        errors = cfg.update({"discovery_min_premium": "150000", "dte_floor": 5})
        assert errors == []
        assert cfg.discovery_min_premium == Decimal("150000")
        assert cfg.dte_floor == 5

    def test_seed_tickers_from_csv_string(self):
        cfg = LiveConfig()
        assert cfg.update({"seed_tickers": "spy, qqq ,BRK.B"}) == []
        assert cfg.seed_tickers == ["SPY", "QQQ", "BRK.B"]

    def test_invalid_values_rejected_with_errors(self):
        cfg = LiveConfig()
        errors = cfg.update({
            "discovery_min_premium": "-5",
            "max_discovered_tickers": 500,
            "stop_loss_pct": "not a number",
            "unknown_key": 1,
        })
        assert len(errors) == 4
        # nothing applied
        assert cfg.discovery_min_premium == Decimal("250000")
        assert cfg.max_discovered_tickers == 20

    def test_partial_update_applies_valid_fields(self):
        cfg = LiveConfig()
        errors = cfg.update({"dte_floor": 3, "stop_loss_pct": 5.0})
        assert len(errors) == 1
        assert cfg.dte_floor == 3
        assert cfg.stop_loss_pct == 0.35

    def test_bad_ticker_symbols_rejected(self):
        cfg = LiveConfig()
        errors = cfg.update({"seed_tickers": "SPY,<script>"})
        assert len(errors) == 1
        assert cfg.seed_tickers == []


class TestPersistence:
    def test_update_persists_and_reloads(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DISCOVERY_MIN_PREMIUM", raising=False)
        path = tmp_path / "live_config.json"
        cfg = LiveConfig.from_env(path)
        cfg.update({"discovery_min_premium": "175000", "seed_tickers": "SPY"})

        saved = json.loads(path.read_text())
        assert saved["discovery_min_premium"] == "175000"

        reloaded = LiveConfig.from_env(path)
        assert reloaded.discovery_min_premium == Decimal("175000")
        assert reloaded.seed_tickers == ["SPY"]

    def test_corrupt_file_falls_back_to_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DISCOVERY_MIN_PREMIUM", raising=False)
        path = tmp_path / "live_config.json"
        path.write_text("{not json")
        cfg = LiveConfig.from_env(path)
        assert cfg.discovery_min_premium == Decimal("250000")

    def test_no_path_update_does_not_raise(self):
        cfg = LiveConfig()
        assert cfg.update({"dte_floor": 4}) == []
