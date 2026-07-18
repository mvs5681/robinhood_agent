"""
Coerce raw MCP tool output (list[dict] or dict) into typed Pydantic models.

MCP tools return JSON-decoded Python objects. The UW API wraps most list responses
in {"data": [...]}. These functions unwrap that envelope and validate each item.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
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
    """Extract the list payload from MCP content format, {data:[...]}, or bare list."""
    # MCP tools return content blocks: [{'type': 'text', 'text': <str|dict|list>}]
    # Some adapters pre-parse 'text' into a Python object; others leave it as a JSON string.
    # When there are multiple content blocks (large responses), merge their payloads.
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "text" in raw[0]:
        merged: list = []
        any_parsed = False
        for block in raw:
            if not isinstance(block, dict):
                continue
            text_val = block.get("text")
            if isinstance(text_val, str):
                try:
                    text_val = json.loads(text_val)
                except Exception:
                    continue  # skip unparseable blocks
            # text_val is now a dict or list (pre-parsed or just decoded)
            if isinstance(text_val, dict):
                any_parsed = True
                if "data" in text_val and isinstance(text_val["data"], list):
                    merged.extend(text_val["data"])
                elif "alert" in text_val:
                    merged.append(text_val["alert"])
                else:
                    merged.append(text_val)
            elif isinstance(text_val, list):
                any_parsed = True
                merged.extend(text_val)
        if any_parsed:
            return merged
        # All blocks returned error text (e.g. "unsupported parameter") — treat as empty
        return []

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


def _parse_occ_symbol(symbol: str) -> tuple[str, date, Decimal, str]:
    """Parse OCC option symbol {ticker}{YYMMDD}{C|P}{8-digit strike*1000}."""
    strike = Decimal(int(symbol[-8:])) / Decimal(1000)
    option_type = "call" if symbol[-9].upper() == "C" else "put"
    date_str = symbol[-15:-9]
    ticker = symbol[:-15].rstrip()
    expiry = date(2000 + int(date_str[:2]), int(date_str[2:4]), int(date_str[4:]))
    return ticker, expiry, strike, option_type


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
    skipped = 0
    for item in items:
        # Try multiple field name variants used across different API versions
        price = (item.get("price") or item.get("strike") or
                 item.get("strike_price") or item.get("expiry_strike"))
        if price is None:
            skipped += 1
            continue
        try:
            normalised = {
                "price":          price,
                "call_gamma_oi":  item.get("call_gamma_oi") or item.get("call_gex") or 0,
                "put_gamma_oi":   item.get("put_gamma_oi") or item.get("put_gex") or 0,
                "call_gamma_vol": item.get("call_gamma_vol"),
                "put_gamma_vol":  item.get("put_gamma_vol"),
                "call_delta_oi":  item.get("call_delta_oi") or item.get("call_delta"),
                "put_delta_oi":   item.get("put_delta_oi") or item.get("put_delta"),
                "time":           item.get("time") or item.get("date"),
            }
            result.append(SpotGEXByStrike.model_validate(normalised))
        except ValidationError:
            skipped += 1
    if skipped:
        import logging
        logging.getLogger(__name__).debug("parse_spot_gex_by_strike: skipped %d unparseable rows", skipped)
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
            # get_flow_per_strike uses call_volume/put_volume; treat as premium proxy
            normalised = {
                "timestamp":       item.get("timestamp") or item.get("date") or "",
                "net_call_premium": item.get("net_call_premium") or item.get("call_volume") or 0,
                "net_put_premium":  item.get("net_put_premium") or item.get("put_volume") or 0,
                "net_volume":       item.get("net_volume"),
            }
            result.append(NetPremTick.model_validate(normalised))
        except ValidationError as e:
            raise UWValidationError(f"NetPremTick[{i}] validation failed: {e}") from e
    return result


def parse_option_contracts(raw: Any) -> list[OptionContract]:
    items = _unwrap(raw)
    result = []
    for i, item in enumerate(items):
        try:
            # get_options_chain encodes ticker/expiry/strike/type in option_symbol (OCC format)
            symbol = item.get("option_symbol", "")
            if symbol and len(symbol) >= 15:
                ticker, expiry, strike, option_type = _parse_occ_symbol(symbol)
            else:
                ticker = item.get("ticker", "")
                expiry = item.get("expiry") or item.get("expiration_date")
                strike = item.get("strike")
                option_type = (item.get("option_type") or item.get("type") or "").lower()

            normalised = {
                "ticker":             ticker,
                "expiry":             expiry,
                "strike":             strike,
                "type":               option_type,
                "bid":                item.get("bid") or item.get("nbbo_bid") or 0,
                "ask":                item.get("ask") or item.get("nbbo_ask") or 0,
                "open_interest":      item.get("open_interest") or 0,
                "volume":             item.get("volume") or 0,
                "implied_volatility": item.get("implied_volatility") or item.get("iv"),
                "delta":              item.get("delta"),
                "gamma":              item.get("gamma"),
                "theta":              item.get("theta"),
                "vega":               item.get("vega"),
            }
            result.append(OptionContract.model_validate(normalised))
        except (ValidationError, Exception) as e:
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
    out: dict = {"timestamp": item.get("date") or item.get("timestamp", "")}
    fn = function.upper()

    # get_extended_technical_indicator returns {"values": {"RSI": "54.82"}, ...}
    values = item.get("values", {})
    if isinstance(values, dict):
        item = {**item, **values}

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
