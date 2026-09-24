"""What a VN30 futures round trip actually costs.

This module exists because the central question of the project is not "is there a
signal" but "does the signal clear the cost floor", and in Vietnam that floor is
unusually high and unusually shaped.

Personal income tax on derivatives is levied on TURNOVER, not on profit:

    transfer_value = settlement_price * MULTIPLIER * contracts * margin_rate / 2
    tax            = transfer_value * 0.1%

Three consequences that drive the whole study:

1. The tax is paid on every side whether the trade won or lost. It is a per-trade
   toll, so total cost scales with turnover and a strategy cannot trade its way out
   of it. Cost per unit of holding time falls as holding time rises, which sets a
   minimum viable holding period -- and any signal that decays faster than that
   period is unmonetisable no matter how predictive it is.

2. The margin rate sits INSIDE the tax base. A VSDC margin announcement is therefore
   a change to the cost function, not merely to capital efficiency. The 2022-12-15
   move from 13% to 17% raised the per-side tax by roughly a third overnight, and
   any backtest spanning that date that uses one flat cost is wrong on one side of
   it.

3. Because the toll is proportional to price level and the tick value is fixed, the
   break-even move measured in TICKS drifts with the index. Costs quoted as "x ticks"
   from an old paper do not transfer to today's level.

`breakeven_ticks` is the number worth carrying around: how far the market must move
in your favour before a round trip is worth doing at all.

All figures are VND per contract unless named otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

from vn30f.config import (
    EXCHANGE_FEE_VND_PER_SIDE,
    INITIAL_MARGIN_RATE,
    MULTIPLIER_VND,
    PIT_TURNOVER_RATE,
    TICK_SIZE,
    TICK_VALUE_VND,
    VSDC_COLLATERAL_FEE_MAX_VND,
    VSDC_COLLATERAL_FEE_MIN_VND,
    VSDC_COLLATERAL_FEE_MONTHLY,
    VSDC_POSITION_FEE_VND_PER_DAY,
)


@dataclass(frozen=True)
class CostBreakdown:
    """Every component kept separate, because which one dominates is the finding."""

    exchange_fee: float = 0.0
    pit_tax: float = 0.0
    position_fee: float = 0.0
    collateral_fee: float = 0.0
    slippage: float = 0.0

    @property
    def total(self) -> float:
        return (self.exchange_fee + self.pit_tax + self.position_fee
                + self.collateral_fee + self.slippage)

    @property
    def total_ticks(self) -> float:
        return self.total / TICK_VALUE_VND

    @property
    def total_points(self) -> float:
        return self.total / MULTIPLIER_VND

    def __str__(self) -> str:
        return (f"{self.total:,.0f} VND ({self.total_ticks:.2f} ticks / "
                f"{self.total_points:.3f} pts)")


@dataclass
class CostModel:
    """Cost of trading one contract, at a price, under a margin regime.

    `margin_rate` defaults to the current VSDC rate but should be passed per-date
    from the recorded policy series when backtesting across an announcement.
    """

    margin_rate: float = INITIAL_MARGIN_RATE
    slippage_ticks_per_side: float = 0.0

    # -- single components -------------------------------------------------
    def pit_tax_per_side(self, price: float, contracts: int = 1) -> float:
        """Turnover tax. Note the margin rate in the base -- that is not a typo."""
        transfer_value = price * MULTIPLIER_VND * contracts * self.margin_rate / 2
        return transfer_value * PIT_TURNOVER_RATE

    def exchange_fee_per_side(self, contracts: int = 1) -> float:
        return EXCHANGE_FEE_VND_PER_SIDE * contracts

    def slippage_per_side(self, contracts: int = 1) -> float:
        return self.slippage_ticks_per_side * TICK_VALUE_VND * contracts

    def overnight_fee(self, contracts: int = 1, nights: int = 0) -> float:
        """VSDC position-management fee, charged only on positions held overnight."""
        return VSDC_POSITION_FEE_VND_PER_DAY * contracts * max(0, nights)

    def collateral_fee(self, price: float, contracts: int = 1,
                       nights: int = 0) -> float:
        """Fee on margin posted, prorated from the monthly rate.

        Floored and capped per account per month; applied here per position, which
        overstates it for an account running many positions. Intraday strategies
        hold no margin overnight, so this is usually zero and the approximation does
        not matter -- it is here so that overnight variants are not free by omission.
        """
        if nights <= 0:
            return 0.0
        margin = price * MULTIPLIER_VND * contracts * self.margin_rate
        monthly = margin * VSDC_COLLATERAL_FEE_MONTHLY
        monthly = min(max(monthly, VSDC_COLLATERAL_FEE_MIN_VND),
                      VSDC_COLLATERAL_FEE_MAX_VND)
        return monthly * nights / 30.0

    # -- aggregates --------------------------------------------------------
    def round_trip(self, entry_price: float, exit_price: float | None = None,
                   contracts: int = 1, nights: int = 0) -> CostBreakdown:
        """Full cost of opening and closing one position.

        Both legs are taxed at their own price, which matters over a large move; the
        common shortcut of doubling the entry-side cost understates a winning trade
        and overstates a losing one.
        """
        exit_price = entry_price if exit_price is None else exit_price
        return CostBreakdown(
            exchange_fee=2 * self.exchange_fee_per_side(contracts),
            pit_tax=(self.pit_tax_per_side(entry_price, contracts)
                     + self.pit_tax_per_side(exit_price, contracts)),
            position_fee=self.overnight_fee(contracts, nights),
            collateral_fee=self.collateral_fee(entry_price, contracts, nights),
            slippage=2 * self.slippage_per_side(contracts),
        )

    def breakeven_ticks(self, price: float, contracts: int = 1,
                        nights: int = 0) -> float:
        """Ticks the market must move your way for a round trip to break even.

        The single number to compare any signal against. A predictor whose average
        forecast move is below this is not tradeable regardless of its t-statistic.
        """
        rt = self.round_trip(price, price, contracts, nights)
        return rt.total / (TICK_VALUE_VND * contracts)

    def breakeven_bps(self, price: float, contracts: int = 1,
                      nights: int = 0) -> float:
        """The same threshold as a fraction of notional, in basis points."""
        rt = self.round_trip(price, price, contracts, nights)
        notional = price * MULTIPLIER_VND * contracts
        return rt.total / notional * 10_000



def summarise(price: float, margin_rates: tuple[float, ...] = (0.10, 0.13, 0.17),
              slippage_ticks: float = 0.0) -> str:
    """Break-even across the margin regimes the market has actually traded under."""
    lines = [f"Round-trip cost, 1 contract at {price:,.1f} index points",
             f"(tick = {TICK_SIZE} pt = {TICK_VALUE_VND:,.0f} VND, "
             f"slippage assumed {slippage_ticks} ticks/side)",
             "",
             f"{'margin':>7} {'fee':>10} {'tax':>10} {'total':>12} "
             f"{'ticks':>7} {'bps':>7}"]
    for mr in margin_rates:
        m = CostModel(margin_rate=mr, slippage_ticks_per_side=slippage_ticks)
        rt = m.round_trip(price)
        lines.append(f"{mr:>6.0%} {rt.exchange_fee:>10,.0f} {rt.pit_tax:>10,.0f} "
                     f"{rt.total:>12,.0f} {m.breakeven_ticks(price):>7.2f} "
                     f"{m.breakeven_bps(price):>7.2f}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summarise(1884.0))
    print()
    print(summarise(1884.0, slippage_ticks=0.5))
