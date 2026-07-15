"""Tests for option price tick rounding."""

from decimal import Decimal

from trader.rh.ticks import round_price_to_tick

NICKEL_DIME = {"above_tick": "0.10", "below_tick": "0.05", "cutoff_price": "3.00"}
PENNY = {"above_tick": "0.01", "below_tick": "0.01", "cutoff_price": "0.00"}


class TestRoundPriceToTick:
    def test_penny_grid_passthrough(self):
        assert round_price_to_tick(Decimal("4.17"), PENNY) == Decimal("4.17")

    def test_none_min_ticks_falls_back_to_penny(self):
        assert round_price_to_tick(Decimal("1.125"), None) == Decimal("1.12")

    def test_above_cutoff_uses_above_tick(self):
        # TQQQ-style rejection: mid 4.17 is off the $0.10 grid above $3
        assert round_price_to_tick(Decimal("4.17"), NICKEL_DIME) == Decimal("4.10")

    def test_below_cutoff_uses_below_tick(self):
        assert round_price_to_tick(Decimal("1.13"), NICKEL_DIME) == Decimal("1.10")

    def test_on_grid_price_unchanged(self):
        assert round_price_to_tick(Decimal("4.20"), NICKEL_DIME) == Decimal("4.20")

    def test_never_rounds_to_zero(self):
        assert round_price_to_tick(Decimal("0.03"), NICKEL_DIME) == Decimal("0.05")

    def test_malformed_min_ticks_falls_back_to_penny(self):
        assert round_price_to_tick(Decimal("2.22"), {"above_tick": "bogus"}) == Decimal("2.22")
