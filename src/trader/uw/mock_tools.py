"""
Fixture-based mock that satisfies the same tool-call interface as the live UW MCP tools.
Used in backtesting and unit tests — zero network I/O.

Each method takes the same kwargs the real MCP tool would receive and returns
the raw dict/list that the real MCP tool would return, sourced from pre-fetched
JSON fixtures.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any


class MockUWTools:
    """
    Loads pre-fetched UW API responses from a fixture directory.

    Fixture layout:
        fixtures/
            history/
                YYYY-MM-DD/
                    {ticker}_spot_gex.json
                    {ticker}_flow_alerts.json
                    {ticker}_darkpool.json
                    {ticker}_net_prem_ticks.json
                    {ticker}_option_contracts.json
                    market_tide.json
            # standalone fixtures used by unit tests
            gex_positive.json
            gex_negative.json
            gex_mixed.json
            flow_alerts.json
            market_tide.json
            darkpool.json
    """

    def __init__(self, fixture_dir: Path) -> None:
        self._dir = fixture_dir

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self, *parts: str) -> Any:
        path = self._dir.joinpath(*parts)
        if not path.exists():
            raise FileNotFoundError(f"Fixture not found: {path}")
        with path.open() as f:
            return json.load(f)

    def _dated(self, as_of: date | None, ticker: str, name: str) -> Any:
        if as_of is not None:
            return self._load("history", as_of.isoformat(), f"{ticker}_{name}.json")
        return self._load(f"{ticker}_{name}.json")

    # ------------------------------------------------------------------
    # Tool methods — mirror the MCP tool signatures
    # ------------------------------------------------------------------

    def get_flow_alerts(
        self,
        ticker_symbol: str | None = None,
        min_premium: float | None = None,
        is_call: bool | None = None,
        is_put: bool | None = None,
        limit: int = 50,
        **_: Any,
    ) -> Any:
        return self._load("flow_alerts.json")

    def get_stock_flow_alerts(self, ticker: str, **_: Any) -> Any:
        return self._load(f"{ticker}_flow_alerts.json")

    def get_spot_exposures_by_strike(
        self,
        ticker: str,
        date: str | None = None,
        **_: Any,
    ) -> Any:
        as_of = _parse_date(date)
        return self._dated(as_of, ticker, "spot_gex")

    def get_greek_exposure_by_strike(self, ticker: str, **_: Any) -> Any:
        return self._dated(None, ticker, "spot_gex")

    def get_market_tide(self, **_: Any) -> Any:
        return self._load("market_tide.json")

    def get_darkpool_ticker(self, ticker: str, **_: Any) -> Any:
        return self._dated(None, ticker, "darkpool")

    def get_net_prem_ticks(self, ticker: str, **_: Any) -> Any:
        return self._dated(None, ticker, "net_prem_ticks")

    def get_option_contracts(self, ticker: str, **_: Any) -> Any:
        return self._dated(None, ticker, "option_contracts")

    def get_greeks(self, ticker: str, **_: Any) -> Any:
        return self._dated(None, ticker, "option_contracts")

    def get_interpolated_iv(self, ticker: str, **_: Any) -> Any:
        return self._dated(None, ticker, "interpolated_iv")

    def get_technical_indicator(self, ticker: str, function: str, **_: Any) -> Any:
        return self._dated(None, ticker, f"technical_{function.lower()}")

    def get_option_contracts_screener(self, **_: Any) -> Any:
        return self._load("screener_option_contracts.json")


def _parse_date(s: str | None) -> date | None:
    if s is None:
        return None
    return date.fromisoformat(s)
