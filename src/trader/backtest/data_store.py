"""Phase 8 — Historical data store for backtest replay.

Fixture layout (one subdirectory per trading day):

    <root>/
      YYYY-MM-DD/
        market_tide.json           market-wide
        flow_alerts.json           market-wide
        {TICKER}_spot_gex.json     per-ticker
        {TICKER}_darkpool.json     per-ticker
        {TICKER}_net_prem_ticks.json
        {TICKER}_option_contracts.json
        {TICKER}_interpolated_iv.json
        {TICKER}_technicals_RSI.json
        {TICKER}_technicals_MACD.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.uw.schemas import OptionContract


# ---------------------------------------------------------------------------
# Duck-typed tool shim — avoids unittest.mock in production code
# ---------------------------------------------------------------------------


class _SliceTool:
    """Minimal async-callable shim that duck-types as a LangChain BaseTool."""

    def __init__(self, name: str, resolver) -> None:
        self.name = name
        self._resolve = resolver

    async def ainvoke(self, kwargs: dict) -> Any:
        return self._resolve(kwargs)


# ---------------------------------------------------------------------------
# BacktestDataSlice
# ---------------------------------------------------------------------------


@dataclass
class BacktestDataSlice:
    """One day's worth of pre-fetched UW API responses."""

    date: date
    tickers: list[str]

    # Raw JSON dicts — same shape the real MCP tools return
    market_tide_raw: Any = field(default_factory=lambda: {"data": []})
    flow_alerts_raw: Any = field(default_factory=lambda: {"data": []})
    spot_gex_raw: dict[str, Any] = field(default_factory=dict)
    darkpool_raw: dict[str, Any] = field(default_factory=dict)
    net_prem_ticks_raw: dict[str, Any] = field(default_factory=dict)
    option_contracts_raw: dict[str, Any] = field(default_factory=dict)
    interpolated_iv_raw: dict[str, Any] = field(default_factory=dict)
    technicals_raw: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Tool creation
    # ------------------------------------------------------------------

    def as_tools(self) -> list[_SliceTool]:
        """Return duck-typed tool shims for use with build_graph(tools=...)."""

        def _market_tide(_: dict) -> Any:
            return self.market_tide_raw

        def _flow_alerts(_: dict) -> Any:
            return self.flow_alerts_raw

        def _by_ticker(store: dict[str, Any]) -> Any:
            def _fn(kwargs: dict) -> Any:
                # get_dark_pool_trades passes ticker_symbol; the rest pass ticker
                ticker = kwargs.get("ticker") or kwargs.get("ticker_symbol", "")
                return store.get(ticker, {"data": []})
            return _fn

        def _technicals(kwargs: dict) -> Any:
            ticker = kwargs.get("ticker", "")
            fn = kwargs.get("function", "RSI")
            return self.technicals_raw.get(ticker, {}).get(fn, {"data": []})

        return [
            _SliceTool("get_market_tide", _market_tide),
            _SliceTool("get_flow_alerts", _flow_alerts),
            _SliceTool("get_greek_exposure_by_strike", _by_ticker(self.spot_gex_raw)),
            _SliceTool("get_dark_pool_trades", _by_ticker(self.darkpool_raw)),
            _SliceTool("get_flow_per_strike", _by_ticker(self.net_prem_ticks_raw)),
            _SliceTool("get_options_chain", _by_ticker(self.option_contracts_raw)),
            _SliceTool("get_interpolated_iv", _by_ticker(self.interpolated_iv_raw)),
            _SliceTool("get_extended_technical_indicator", _technicals),
        ]

    # ------------------------------------------------------------------
    # Price / premium lookups for exit evaluation
    # ------------------------------------------------------------------

    def get_spot_price(self, ticker: str) -> Decimal | None:
        """Extract the most recent underlying price for a ticker."""
        from trader.uw.validators import parse_darkpool_prints, parse_flow_alerts

        try:
            for alert in parse_flow_alerts(self.flow_alerts_raw):
                if alert.ticker == ticker and alert.underlying_price is not None:
                    return alert.underlying_price
        except Exception:
            pass

        try:
            raw = self.darkpool_raw.get(ticker)
            if raw is not None:
                prints = parse_darkpool_prints(raw)
                if prints:
                    return max(prints, key=lambda p: p.executed_at).price
        except Exception:
            pass

        return None

    def get_option_premium(self, contract: OptionContract) -> Decimal | None:
        """Return the mid-price for a contract on this day, or None if unavailable."""
        from trader.uw.validators import parse_option_contracts

        try:
            raw = self.option_contracts_raw.get(contract.ticker, {"data": []})
            for c in parse_option_contracts(raw):
                if (
                    c.strike == contract.strike
                    and c.expiry == contract.expiry
                    and c.type == contract.type
                ):
                    return c.mid
        except Exception:
            pass

        return None


# ---------------------------------------------------------------------------
# DataStore
# ---------------------------------------------------------------------------


class DataStore:
    """Loads BacktestDataSlice objects from a fixture root directory.

    Detects which tickers are available on a given date by scanning for
    files matching ``{TICKER}_spot_gex.json`` in the date subdirectory.
    All other per-ticker files are optional and default to ``{"data": []}``.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def available_dates(self) -> list[date]:
        """Return sorted list of dates that have fixture data."""
        dates: list[date] = []
        if not self._root.exists():
            return dates
        for d in sorted(self._root.iterdir()):
            if d.is_dir():
                try:
                    dates.append(date.fromisoformat(d.name))
                except ValueError:
                    pass
        return dates

    def load(self, trade_date: date) -> BacktestDataSlice:
        """Load all fixture data for a single trading day."""
        day_dir = self._root / trade_date.isoformat()
        if not day_dir.exists():
            raise FileNotFoundError(
                f"No fixture data for {trade_date}: {day_dir}"
            )

        tickers = sorted(
            f.name.replace("_spot_gex.json", "")
            for f in day_dir.glob("*_spot_gex.json")
        )

        def _load(name: str) -> Any:
            path = day_dir / name
            return json.loads(path.read_text()) if path.exists() else {"data": []}

        return BacktestDataSlice(
            date=trade_date,
            tickers=tickers,
            market_tide_raw=_load("market_tide.json"),
            flow_alerts_raw=_load("flow_alerts.json"),
            spot_gex_raw={t: _load(f"{t}_spot_gex.json") for t in tickers},
            darkpool_raw={t: _load(f"{t}_darkpool.json") for t in tickers},
            net_prem_ticks_raw={t: _load(f"{t}_net_prem_ticks.json") for t in tickers},
            option_contracts_raw={t: _load(f"{t}_option_contracts.json") for t in tickers},
            interpolated_iv_raw={t: _load(f"{t}_interpolated_iv.json") for t in tickers},
            technicals_raw={
                t: {
                    fn: _load(f"{t}_technicals_{fn}.json")
                    for fn in ("RSI", "MACD")
                }
                for t in tickers
            },
        )
