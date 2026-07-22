"""Runtime-tunable settings, editable from the dashboard Settings tab.

Values are seeded from environment variables at startup, then overridden by
the JSON file at `path` if it exists (dashboard edits are persisted there, so
they survive container restarts). The scanner/watcher/exit loops read these
values each cycle — updates apply from the next cycle without a restart.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# field → (parser, validator, human description of the constraint)
_FIELDS: dict[str, tuple] = {
    "discovery_min_premium": (
        lambda v: Decimal(str(v)),
        lambda v: v > 0,
        "must be a positive dollar amount",
    ),
    "max_discovered_tickers": (
        int,
        lambda v: 1 <= v <= 100,
        "must be between 1 and 100",
    ),
    "flow_min_premium": (
        lambda v: Decimal(str(v)),
        lambda v: v > 0,
        "must be a positive dollar amount",
    ),
    "stop_loss_pct": (
        float,
        lambda v: 0.01 <= v <= 0.95,
        "must be between 0.01 and 0.95",
    ),
    "dte_floor": (
        int,
        lambda v: 0 <= v <= 30,
        "must be between 0 and 30",
    ),
    "wall_proximity_pct": (
        float,
        lambda v: 0.005 <= v <= 0.10,
        "must be between 0.005 (0.5%) and 0.10 (10%)",
    ),
    "seed_tickers": (
        lambda v: [t.strip().upper() for t in (v.split(",") if isinstance(v, str) else v) if t.strip()],
        lambda v: len(v) <= 20 and all(_TICKER_RE.match(t) for t in v),
        "must be at most 20 valid ticker symbols",
    ),
    "selector_dte_min": (
        int,
        lambda v: 1 <= v <= 365,
        "must be between 1 and 365",
    ),
    "selector_dte_max": (
        int,
        lambda v: 1 <= v <= 365,
        "must be between 1 and 365",
    ),
    "selector_delta_min": (
        float,
        lambda v: 0.01 <= v <= 0.99,
        "must be between 0.01 and 0.99",
    ),
    "selector_delta_max": (
        float,
        lambda v: 0.01 <= v <= 0.99,
        "must be between 0.01 and 0.99",
    ),
}


@dataclass
class LiveConfig:
    discovery_min_premium: Decimal = Decimal("250000")
    max_discovered_tickers: int = 20
    flow_min_premium: Decimal = Decimal("100000")
    stop_loss_pct: float = 0.35
    dte_floor: int = 7
    wall_proximity_pct: float = 0.015
    seed_tickers: list[str] = field(default_factory=list)
    # Contract selector window — kept in sync with SelectorParams defaults
    selector_dte_min: int = 21
    selector_dte_max: int = 30
    selector_delta_min: float = 0.30
    selector_delta_max: float = 0.45
    path: Path | None = None

    @classmethod
    def from_env(cls, path: Path | str | None = None) -> "LiveConfig":
        cfg = cls(
            discovery_min_premium=Decimal(os.environ.get("DISCOVERY_MIN_PREMIUM", "250000")),
            max_discovered_tickers=int(os.environ.get("MAX_DISCOVERED_TICKERS", "20")),
            flow_min_premium=Decimal(os.environ.get("FLOW_MIN_PREMIUM", "100000")),
            stop_loss_pct=float(os.environ.get("STOP_LOSS_PCT", "0.35")),
            dte_floor=int(os.environ.get("DTE_FLOOR", "7")),
            wall_proximity_pct=float(os.environ.get("WALL_PROXIMITY_PCT", "0.015")),
            seed_tickers=[t.strip().upper() for t in os.environ.get("TICKERS", "").split(",") if t.strip()],
            path=Path(path) if path else None,
        )
        cfg._load_overrides()
        return cfg

    def _load_overrides(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text())
        except Exception as exc:
            logger.warning("Could not read %s (%s) — using env defaults", self.path, exc)
            return
        errors = self.update(saved, persist=False)
        for err in errors:
            logger.warning("Ignoring saved config value: %s", err)
        logger.info("Loaded config overrides from %s", self.path)

    def update(self, values: dict, persist: bool = True) -> list[str]:
        """Apply validated updates. Returns a list of per-field error strings;
        valid fields are applied even when other fields fail."""
        errors: list[str] = []
        applied = False
        for key, raw in values.items():
            spec = _FIELDS.get(key)
            if spec is None:
                errors.append(f"{key}: unknown setting")
                continue
            parser, valid, constraint = spec
            try:
                parsed = parser(raw)
            except (ValueError, TypeError, InvalidOperation, AttributeError):
                errors.append(f"{key}: could not parse {raw!r}")
                continue
            if not valid(parsed):
                errors.append(f"{key}: {constraint}")
                continue
            setattr(self, key, parsed)
            applied = True
        if applied and persist:
            self.save()
        return errors

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        except Exception as exc:
            logger.error("Could not persist config to %s: %s", self.path, exc)

    def to_dict(self) -> dict:
        return {
            "discovery_min_premium": str(self.discovery_min_premium),
            "max_discovered_tickers": self.max_discovered_tickers,
            "flow_min_premium": str(self.flow_min_premium),
            "stop_loss_pct": self.stop_loss_pct,
            "dte_floor": self.dte_floor,
            "wall_proximity_pct": self.wall_proximity_pct,
            "seed_tickers": self.seed_tickers,
            "selector_dte_min": self.selector_dte_min,
            "selector_dte_max": self.selector_dte_max,
            "selector_delta_min": self.selector_delta_min,
            "selector_delta_max": self.selector_delta_max,
        }
