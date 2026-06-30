"""
Coerce raw MCP tool output (list[dict] or dict) into typed Pydantic models.

MCP tools return JSON-decoded Python objects. The UW API wraps most list responses
in {"data": [...]}. These functions unwrap that envelope and validate each item.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .schemas import (
    DarkpoolPrint,
    FlowAlert,
    InterpolatedIVEntry,
    MarketTide,
    NetPremTick,
    OptionContract,
    SpotGEXByStrike,
    TechnicalPoint,
)


class UWValidationError(Exception):
    pass


def _unwrap(raw: Any) -> list[dict]:
    """Extract the list payload from {data: [...]} or bare list."""
    if isinstance(raw, dict):
        if "data" in raw:
            payload = raw["data"]
        elif "alert" in raw:
            payload = [raw["alert"]]
        else:
            payload = [raw]
    elif isinstance(raw, list):
        payload = raw
    else:
        raise UWValidationError(f"Unexpected MCP response type: {type(raw)}")
    return payload


def parse_flow_alerts(raw: Any) -> list[FlowAlert]:
    items = _unwrap(raw)
    result = []
    for i, item in enumerate(items):
        try:
            result.append(FlowAlert.model_validate(item))
        except ValidationError as e:
            raise UWValidationError(f"FlowAlert[{i}] validation failed: {e}") from e
    return result


def parse_spot_gex_by_strike(raw: Any) -> list[SpotGEXByStrike]:
    items = _unwrap(raw)
    result = []
    for i, item in enumerate(items):
        try:
            result.append(SpotGEXByStrike.model_validate(item))
        except ValidationError as e:
            raise UWValidationError(f"SpotGEXByStrike[{i}] validation failed: {e}") from e
    return result


def parse_market_tide(raw: Any) -> list[MarketTide]:
    items = _unwrap(raw)
    result = []
    for i, item in enumerate(items):
        try:
            result.append(MarketTide.model_validate(item))
        except ValidationError as e:
            raise UWValidationError(f"MarketTide[{i}] validation failed: {e}") from e
    return result


def parse_darkpool_prints(raw: Any) -> list[DarkpoolPrint]:
    items = _unwrap(raw)
    result = []
    for i, item in enumerate(items):
        try:
            result.append(DarkpoolPrint.model_validate(item))
        except ValidationError as e:
            raise UWValidationError(f"DarkpoolPrint[{i}] validation failed: {e}") from e
    return result


def parse_net_prem_ticks(raw: Any) -> list[NetPremTick]:
    items = _unwrap(raw)
    result = []
    for i, item in enumerate(items):
        try:
            result.append(NetPremTick.model_validate(item))
        except ValidationError as e:
            raise UWValidationError(f"NetPremTick[{i}] validation failed: {e}") from e
    return result


def parse_option_contracts(raw: Any) -> list[OptionContract]:
    items = _unwrap(raw)
    result = []
    for i, item in enumerate(items):
        try:
            result.append(OptionContract.model_validate(item))
        except ValidationError as e:
            raise UWValidationError(f"OptionContract[{i}] validation failed: {e}") from e
    return result


def parse_interpolated_iv(raw: Any) -> list[InterpolatedIVEntry]:
    items = _unwrap(raw)
    result = []
    for i, item in enumerate(items):
        try:
            result.append(InterpolatedIVEntry.model_validate(item))
        except ValidationError as e:
            raise UWValidationError(f"InterpolatedIVEntry[{i}] validation failed: {e}") from e
    return result


def parse_technical_indicator(raw: Any, function: str) -> list[TechnicalPoint]:
    """
    Parse technical indicator response.
    The UW API returns Alpha Vantage-style data with function-specific field names.
    We normalise MACD fields and map single-value indicators to `.value`.
    """
    items = _unwrap(raw)
    result = []
    for i, item in enumerate(items):
        try:
            normalised = _normalise_technical(item, function)
            result.append(TechnicalPoint.model_validate(normalised))
        except (ValidationError, KeyError) as e:
            raise UWValidationError(
                f"TechnicalPoint[{i}] ({function}) validation failed: {e}"
            ) from e
    return result


def _normalise_technical(item: dict, function: str) -> dict:
    """Map function-specific field names onto the unified TechnicalPoint fields."""
    out: dict = {"timestamp": item.get("timestamp", item.get("date", ""))}
    fn = function.upper()

    if fn == "MACD":
        out["macd"] = item.get("MACD") or item.get("macd")
        out["signal"] = item.get("MACD_Signal") or item.get("signal")
        out["histogram"] = item.get("MACD_Hist") or item.get("histogram")
    elif fn == "BBANDS":
        out["upper_band"] = item.get("Real Upper Band") or item.get("upper_band")
        out["middle_band"] = item.get("Real Middle Band") or item.get("middle_band")
        out["lower_band"] = item.get("Real Lower Band") or item.get("lower_band")
    else:
        # Single-value indicators: RSI, SMA, EMA, etc.
        out["value"] = (
            item.get(fn)
            or item.get(fn.lower())
            or item.get("value")
        )
    return out
