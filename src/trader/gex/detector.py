"""
GEX Setup Detector — Phase 2.

Takes a list of SpotGEXByStrike rows (already fetched by Phase 1) plus the
current spot price, and returns a GEXSetup describing the gamma regime,
key walls, flip point, and a candidate trade direction.

No I/O here — fully synchronous and deterministic, so it is easily unit-testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from trader.uw.schemas import SpotGEXByStrike

from .schemas import GEXDetectorParams, GEXRegime, GEXSetup, GEXWall


class GEXDetector:
    def __init__(self, params: GEXDetectorParams | None = None) -> None:
        self.params = params or GEXDetectorParams()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def detect(
        self,
        ticker: str,
        gex_data: Sequence[SpotGEXByStrike],
        spot_price: Decimal,
        as_of: datetime | None = None,
    ) -> GEXSetup:
        if as_of is None:
            as_of = datetime.now(timezone.utc)

        strikes = sorted(gex_data, key=lambda s: s.price)

        if not strikes:
            return self._empty(ticker, spot_price, [], as_of)

        total_abs = sum(abs(s.net_gex) for s in strikes)
        if total_abs == 0:
            return self._empty(ticker, spot_price, list(strikes), as_of)

        # --- Regime classification ---
        total_net = sum(s.net_gex for s in strikes)
        regime_ratio = float(total_net / total_abs)

        top3_abs = sum(
            abs(s.net_gex)
            for s in sorted(strikes, key=lambda s: abs(s.net_gex), reverse=True)[:3]
        )
        top3_pct = float(top3_abs / total_abs)

        # Confidence captures both concentration and directional clarity.
        structure_confidence = min(top3_pct, abs(regime_ratio))

        if structure_confidence < self.params.min_confidence_threshold:
            regime = GEXRegime.MIXED
        elif regime_ratio >= self.params.positive_regime_threshold:
            regime = GEXRegime.POSITIVE
        elif regime_ratio <= self.params.negative_regime_threshold:
            regime = GEXRegime.NEGATIVE
        else:
            regime = GEXRegime.MIXED

        # --- Structural features ---
        flip_point = self._find_flip_point(strikes)
        call_wall = self._find_call_wall(strikes, spot_price)
        put_wall = self._find_put_wall(strikes, spot_price)

        # --- Direction & setup type ---
        candidate_direction, setup_type, target_level = self._resolve_direction(
            regime, spot_price, flip_point, call_wall, put_wall
        )

        return GEXSetup(
            ticker=ticker,
            as_of=as_of,
            spot_price=spot_price,
            regime=regime,
            flip_point=flip_point,
            nearest_call_wall=call_wall,
            nearest_put_wall=put_wall,
            target_level=target_level,
            candidate_direction=candidate_direction,
            setup_type=setup_type,
            structure_confidence=structure_confidence,
            raw_gex_by_strike=list(strikes),
        )

    # ------------------------------------------------------------------
    # Feature helpers (package-private, tested individually)
    # ------------------------------------------------------------------

    @staticmethod
    def _find_flip_point(
        strikes: list[SpotGEXByStrike],
    ) -> Decimal | None:
        """
        Linear interpolation of the strike where net GEX crosses zero.
        Returns None if net GEX never changes sign.
        """
        for i in range(len(strikes) - 1):
            curr, nxt = strikes[i], strikes[i + 1]
            if (curr.net_gex > 0 and nxt.net_gex <= 0) or (
                curr.net_gex < 0 and nxt.net_gex >= 0
            ):
                denom = curr.net_gex - nxt.net_gex
                if denom == 0:
                    continue
                t = float(curr.net_gex / denom)
                flip = float(curr.price) + t * float(nxt.price - curr.price)
                return Decimal(str(round(flip, 4)))
        return None

    @staticmethod
    def _find_call_wall(
        strikes: list[SpotGEXByStrike],
        spot: Decimal,
    ) -> GEXWall | None:
        """Highest net-GEX strike above spot (dealers most long gamma there)."""
        candidates = [s for s in strikes if s.price > spot and s.net_gex > 0]
        if not candidates:
            return None
        wall = max(candidates, key=lambda s: s.net_gex)
        return GEXWall(
            strike=wall.price,
            net_gex=wall.net_gex,
            distance_pct=(abs(wall.price - spot) / spot).quantize(Decimal("0.0001")),
            side="call_wall",
        )

    @staticmethod
    def _find_put_wall(
        strikes: list[SpotGEXByStrike],
        spot: Decimal,
    ) -> GEXWall | None:
        """Most-negative net-GEX strike below spot (dealers most short gamma there)."""
        candidates = [s for s in strikes if s.price < spot and s.net_gex < 0]
        if not candidates:
            return None
        wall = min(candidates, key=lambda s: s.net_gex)  # most negative
        return GEXWall(
            strike=wall.price,
            net_gex=wall.net_gex,
            distance_pct=(abs(wall.price - spot) / spot).quantize(Decimal("0.0001")),
            side="put_wall",
        )

    def _resolve_direction(
        self,
        regime: GEXRegime,
        spot: Decimal,
        flip_point: Decimal | None,
        call_wall: GEXWall | None,
        put_wall: GEXWall | None,
    ) -> tuple[str, str, Decimal | None]:
        """
        Returns (candidate_direction, setup_type, target_level).

        POSITIVE regime:
          Dealers suppress moves → price pins between walls.
          Direction = call (buy calls to play reversion toward call wall).
          setup_type = "pin"

        NEGATIVE regime:
          Dealers amplify moves → momentum trade.
          spot below flip_point → bearish momentum → direction = "put"
          spot above flip_point (or no flip) → bullish squeeze → direction = "call"
          setup_type = "momentum"

        MIXED: no trade.
        """
        if regime == GEXRegime.MIXED:
            return "none", "none", None

        if regime == GEXRegime.POSITIVE:
            target = call_wall.strike if call_wall else None
            return "call", "pin", target

        # NEGATIVE
        if flip_point is not None and spot < flip_point:
            # Spot is in negative gamma territory — bearish momentum
            target = put_wall.strike if put_wall else None
            return "put", "momentum", target
        else:
            # Spot is above flip or no flip → bullish squeeze
            target = call_wall.strike if call_wall else None
            return "call", "momentum", target

    # ------------------------------------------------------------------

    def _empty(
        self,
        ticker: str,
        spot: Decimal,
        raw: list[SpotGEXByStrike],
        as_of: datetime,
    ) -> GEXSetup:
        return GEXSetup(
            ticker=ticker,
            as_of=as_of,
            spot_price=spot,
            regime=GEXRegime.MIXED,
            flip_point=None,
            nearest_call_wall=None,
            nearest_put_wall=None,
            target_level=None,
            candidate_direction="none",
            setup_type="none",
            structure_confidence=0.0,
            raw_gex_by_strike=raw,
        )
