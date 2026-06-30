"""Unit tests for the UW response validation layer."""

from decimal import Decimal

import pytest

from trader.uw.validators import (
    UWValidationError,
    parse_darkpool_prints,
    parse_flow_alerts,
    parse_market_tide,
    parse_spot_gex_by_strike,
)


class TestParseFlowAlerts:
    def test_parses_wrapped_list(self, flow_alerts_raw):
        alerts = parse_flow_alerts(flow_alerts_raw)
        assert len(alerts) == 2
        assert alerts[0].ticker == "AAPL"
        assert alerts[0].is_call is True
        assert alerts[0].total_premium == Decimal("250000")
        assert alerts[1].ticker == "SPY"
        assert alerts[1].is_call is False

    def test_parses_bare_list(self, flow_alerts_raw):
        alerts = parse_flow_alerts(flow_alerts_raw["data"])
        assert len(alerts) == 2

    def test_invalid_item_raises(self):
        with pytest.raises(UWValidationError, match="FlowAlert"):
            parse_flow_alerts({"data": [{"ticker": "AAPL"}]})  # missing required fields

    def test_empty_data_returns_empty(self):
        assert parse_flow_alerts({"data": []}) == []


class TestParseSpotGEXByStrike:
    def test_parses_positive_gex(self, gex_positive_raw):
        strikes = parse_spot_gex_by_strike(gex_positive_raw)
        assert len(strikes) == 7
        # net_gex = call_gamma_oi + put_gamma_oi; all are positive here
        assert all(s.net_gex > 0 for s in strikes)

    def test_parses_negative_gex(self, gex_negative_raw):
        strikes = parse_spot_gex_by_strike(gex_negative_raw)
        # Most strikes should be net negative
        net_gex_values = [s.net_gex for s in strikes]
        assert sum(1 for v in net_gex_values if v < 0) >= 4

    def test_strike_property(self, gex_positive_raw):
        strikes = parse_spot_gex_by_strike(gex_positive_raw)
        assert strikes[0].strike == strikes[0].price

    def test_net_gex_calculation(self, gex_positive_raw):
        strikes = parse_spot_gex_by_strike(gex_positive_raw)
        s = strikes[0]
        assert s.net_gex == s.call_gamma_oi + s.put_gamma_oi


class TestParseMarketTide:
    def test_parses_three_ticks(self, market_tide_raw):
        ticks = parse_market_tide(market_tide_raw)
        assert len(ticks) == 3
        assert ticks[0].net_call_premium == Decimal("1200000.00")
        assert ticks[0].net_put_premium == Decimal("-450000.00")
        assert ticks[0].net_volume == 18500

    def test_bullish_tide(self, market_tide_raw):
        ticks = parse_market_tide(market_tide_raw)
        # All ticks have positive net call vs put
        for tick in ticks:
            assert tick.net_call_premium > abs(tick.net_put_premium)


class TestParseDarkpoolPrints:
    def test_parses_two_prints(self, darkpool_raw):
        prints = parse_darkpool_prints(darkpool_raw)
        assert len(prints) == 2
        assert prints[0].ticker == "AAPL"
        assert prints[0].premium == Decimal("2931300.00")
        assert prints[0].canceled is False

    def test_price_is_decimal(self, darkpool_raw):
        prints = parse_darkpool_prints(darkpool_raw)
        assert isinstance(prints[0].price, Decimal)
