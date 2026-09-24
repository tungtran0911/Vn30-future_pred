"""Tests for the facts the project cannot afford to get wrong.

Not aiming at coverage. Each test pins something that is either externally
verifiable (a regulator's worked example, an exchange calendar rule) or a bug that
already happened during development.

    python -m pytest tests/test_vn30f.py -q
"""

from __future__ import annotations

from datetime import date, datetime, time

import numpy as np
import pandas as pd
import pytest

from vn30f.calendar_vn import (
    Phase,
    expiry_date,
    front_month_symbol,
    is_trading_phase,
    phase_at,
)
from vn30f.config import MULTIPLIER_VND, TICK_VALUE_VND
from vn30f.contracts import build_continuous
from vn30f.execution.cost_model import CostModel
from vn30f.ingest.kbs_api import to_krx_code
from vn30f.ingest.kbs_recorder import trade_uid


class TestSymbolCoding:
    """KRX instrument codes. Verified against vnstock's converter for 2025-2027."""

    @pytest.mark.parametrize("symbol,expected", [
        ("VN30F2608", "41I1G8000"),
        ("VN30F2609", "41I1G9000"),
        ("VN30F2610", "41I1GA000"),   # month 10 -> A, not "10"
        ("VN30F2612", "41I1GC000"),
        ("VN30F2703", "41I1H3000"),   # year rolls the letter
    ])
    def test_known_codes(self, symbol, expected):
        assert to_krx_code(symbol) == expected

    @pytest.mark.parametrize("bad", ["VN30F1M", "VN30F2M", "VN30", "", "VN30F26134"])
    def test_aliases_are_refused(self, bad):
        # Aliases must be resolved to a real contract first. Encoding "1M" would
        # produce a plausible-looking code for a contract that does not exist.
        with pytest.raises(ValueError):
            to_krx_code(bad)


class TestTradeIdentity:
    """The dedup key. Getting this wrong inflated a recorded session by ~50%."""

    def test_keyed_on_accumulated_volume(self):
        row = {"accumulated_volume": 195983, "price": 1875.4, "volume": 1,
               "ts_raw": "2026-08-17 14:29:40:28"}
        assert trade_uid("VN30F2608", row) == "VN30F2608|195983"

    def test_ignores_the_unstable_timestamp(self):
        """The same trade returns a different sub-second stamp on each fetch.

        Observed directly: one page fetched twice gave :28 then :26 for a trade with
        identical accumulated volume, price and size. A key that includes it lets the
        same trade in twice.
        """
        a = {"accumulated_volume": 195983, "price": 1875.4, "volume": 1,
             "ts_raw": "2026-08-17 14:29:40:28"}
        b = dict(a, ts_raw="2026-08-17 14:29:40:26")
        assert trade_uid("VN30F2608", a) == trade_uid("VN30F2608", b)

    def test_contract_namespacing(self):
        """Two contracts share the accumulated-volume number space."""
        row = {"accumulated_volume": 1000}
        assert trade_uid("VN30F2608", row) != trade_uid("VN30F2609", row)


class TestRecorderSchedule:
    """Loop-lifetime and recovery-guard decisions. Two real bugs live here.

    A VN weekday's phases at fixed clock times, tz-aware. Monday 2026-08-24.
    """

    @staticmethod
    def _vn(hh, mm):
        from vn30f.config import TZ
        return datetime(2026, 8, 24, hh, mm, tzinfo=TZ)   # a Monday

    def test_lunch_is_not_day_finished(self):
        """The bug: the loop exited at 11:30 and never recorded the afternoon.

        Lunch must read as "not finished" so the loop idles through it. If this
        flips, a task that starts before lunch silently loses every afternoon.
        """
        from vn30f.ingest.kbs_recorder import _day_finished
        assert _day_finished(self._vn(11, 45)) is False   # lunch
        assert _day_finished(self._vn(9, 30)) is False     # morning
        assert _day_finished(self._vn(13, 30)) is False    # afternoon
        assert _day_finished(self._vn(14, 50)) is True     # after close

    def test_weekend_is_day_finished(self):
        from vn30f.config import TZ
        from vn30f.ingest.kbs_recorder import _day_finished
        saturday = datetime(2026, 8, 22, 10, 0, tzinfo=TZ)
        assert _day_finished(saturday) is True

    def test_sweep_refuses_before_open_and_on_weekend(self):
        """Recovery must not file the prior session under today's date."""
        from vn30f.config import TZ
        from vn30f.ingest.kbs_recorder import _sweep_is_safe_today
        assert _sweep_is_safe_today(self._vn(8, 30)) is False   # pre-open
        assert _sweep_is_safe_today(self._vn(9, 30)) is True     # morning
        assert _sweep_is_safe_today(self._vn(11, 45)) is True    # lunch: today still
        assert _sweep_is_safe_today(self._vn(15, 0)) is True     # after close, same day
        saturday = datetime(2026, 8, 22, 10, 0, tzinfo=TZ)
        assert _sweep_is_safe_today(saturday) is False


class TestCalendar:
    def test_expiry_is_third_thursday(self):
        assert expiry_date(2026, 8) == date(2026, 8, 20)
        assert expiry_date(2026, 9) == date(2026, 9, 17)
        assert expiry_date(2022, 6) == date(2022, 6, 16)   # first trimmed-FSP expiry

    def test_front_month_rolls_after_expiry(self):
        assert front_month_symbol(date(2026, 8, 19)) == "VN30F2608"
        assert front_month_symbol(date(2026, 8, 20)) == "VN30F2608"  # expiry day
        assert front_month_symbol(date(2026, 8, 21)) == "VN30F2609"

    def test_front_month_rolls_across_year_end(self):
        assert front_month_symbol(date(2026, 12, 18)) == "VN30F2701"

    @pytest.mark.parametrize("t,expected", [
        (time(8, 44), Phase.PRE_OPEN),
        (time(8, 45), Phase.ATO),        # ATO opens before the cash market exists
        (time(9, 0), Phase.MORNING),
        (time(11, 30), Phase.LUNCH),
        (time(13, 0), Phase.AFTERNOON),
        (time(14, 30), Phase.ATC),
        (time(14, 45), Phase.CLOSED),
    ])
    def test_phase_boundaries(self, t, expected):
        assert phase_at(t) is expected

    def test_lunch_is_not_tradeable(self):
        assert not is_trading_phase(Phase.LUNCH)
        assert is_trading_phase(Phase.ATO)


class TestRollEngine:
    """Back-adjustment. An off-by-one bar here corrupts every cross-roll return."""

    @staticmethod
    def _bars():
        """Two contracts around the 2026-07-16 expiry, with a known -1.0 roll gap.

        F1M jumps +17.2 across the expiry bar, but the incoming contract only moved
        -5.3. The adjusted series must show -5.3.
        """
        rows = []
        for t, f1, f2 in [
            ("2026-07-15", 1919.1, 1919.9),
            ("2026-07-16", 1936.3, 1935.3),   # expiry: gap = 1935.3 - 1936.3 = -1.0
            ("2026-07-17", 1930.0, 1931.5),   # F1M is now the incoming contract
        ]:
            rows.append({"time": pd.Timestamp(t), "symbol": "VN30F1M", "close": f1})
            rows.append({"time": pd.Timestamp(t), "symbol": "VN30F2M", "close": f2})
        return pd.DataFrame(rows)

    def test_gap_is_the_expiry_day_spread(self):
        _, gaps = build_continuous(self._bars())
        roll = gaps[gaps["observed"]].iloc[0]
        assert roll["gap"] == pytest.approx(-1.0)

    def test_expiry_bar_belongs_to_the_outgoing_contract(self):
        """The expiry bar must be shifted with the OLD segment.

        On expiry day the front month is still the outgoing contract -- it trades to
        that day's close. Shifting only the bars strictly before it does not remove
        the roll jump, it just relocates it one bar earlier.
        """
        cont, _ = build_continuous(self._bars())
        c = cont.set_index("time")
        assert c.loc["2026-07-16", "cum_adjustment"] == pytest.approx(-1.0)
        assert c.loc["2026-07-17", "cum_adjustment"] == pytest.approx(0.0)

    def test_roll_crossing_return_is_the_incoming_contracts_return(self):
        cont, _ = build_continuous(self._bars())
        c = cont.set_index("time")
        crossing = c.loc["2026-07-17", "adjusted"] - c.loc["2026-07-16", "adjusted"]
        assert crossing == pytest.approx(1930.0 - 1935.3)   # -5.3, not the raw +17.2...

    def test_current_segment_is_unadjusted(self):
        """Raw prices must survive after the last roll, for execution and price levels."""
        cont, _ = build_continuous(self._bars())
        tail = cont[cont["time"] > pd.Timestamp("2026-07-16")]
        assert (tail["adjusted"] == tail["raw"]).all()

    def test_missing_second_month_is_flagged_not_guessed(self):
        bars = self._bars()
        bars = bars[~((bars["symbol"] == "VN30F2M")
                      & (bars["time"] == pd.Timestamp("2026-07-16")))]
        _, gaps = build_continuous(bars)
        assert not gaps.iloc[0]["observed"]
        assert gaps.iloc[0]["gap"] == 0.0

    def test_requires_both_aliases(self):
        one = self._bars().query("symbol == 'VN30F1M'")
        with pytest.raises(ValueError, match="VN30F2M"):
            build_continuous(one)


class TestFeatures:

    def test_tfi_preserves_total_signed_volume(self):
        """Aggregating trade-flow imbalance must equal raw signed volume."""
        from vn30f.features.microstructure import tfi_at
        ts = pd.date_range("2026-08-17 09:00", periods=6, freq="500ms",
                           tz="Asia/Ho_Chi_Minh")
        ticks = pd.DataFrame({
            "ts": ts,
            "label": ["VN30F2608"] * 6,
            "side": ["B", "B", "S", "B", "S", "S"],
            "side_sign": [1, 1, -1, 1, -1, -1],
            "volume": [3, 2, 5, 1, 4, 2],
            "signed_volume": [3, 2, -5, 1, -4, -2],
        })
        tfi = tfi_at(ticks, "1s")
        assert tfi["signed_vol"].sum() == ticks["signed_volume"].sum() == -5

    def test_flow_excludes_trades_in_the_snapshots_own_second(self):
        """No look-ahead in the headline study.

        Trades carry whole-second stamps. At a snapshot 0.5s into second s, a trade
        stamped s may not have happened yet, so it must not count; by the next
        second it must.
        """
        from vn30f.studies.order_flow import build_panel
        tz = "Asia/Ho_Chi_Minh"
        ticks = pd.DataFrame({
            "ts": pd.to_datetime(["2026-08-28 13:00:10",
                                  "2026-08-28 13:00:12"]).tz_localize(tz),
            "label": "VN30F2609",
            "volume": [5, 7],
            "signed_volume": [5, -7],
        })
        book = pd.DataFrame({
            "ts": pd.to_datetime(["2026-08-28 13:00:12.500",
                                  "2026-08-28 13:00:13.200"]).tz_localize(tz),
            "mid": [1900.0, 1900.1], "spread": [0.2, 0.2], "phase": "afternoon",
        })
        panel = build_panel("2026-08-28", ticks, book)
        assert panel["x"].tolist() == [5.0, -2.0]


class TestCostModel:

    def test_matches_the_regulators_worked_example(self):
        """1 contract at 1,000 points, 17% margin -> 8,500 VND of tax per side.

        Straight from the published worked example of the derivatives PIT rule. If
        this drifts, every net P&L number in the project is wrong.
        """
        m = CostModel(margin_rate=0.17)
        assert m.pit_tax_per_side(price=1000.0) == pytest.approx(8_500.0)

    def test_tax_scales_with_margin_rate(self):
        """The margin rate is inside the tax base, so a policy change moves costs."""
        low = CostModel(margin_rate=0.13).pit_tax_per_side(1884.0)
        high = CostModel(margin_rate=0.17).pit_tax_per_side(1884.0)
        assert high / low == pytest.approx(0.17 / 0.13)

    def test_breakeven_at_current_levels(self):
        """~3.7 ticks round trip at 17% -- several times the front-month spread."""
        m = CostModel(margin_rate=0.17)
        assert m.breakeven_ticks(1884.0) == pytest.approx(3.74, abs=0.01)

    def test_both_legs_taxed_at_their_own_price(self):
        """Doubling the entry-side tax is wrong once the price has moved."""
        m = CostModel(margin_rate=0.17)
        flat = m.round_trip(1884.0, 1884.0).pit_tax
        moved = m.round_trip(1884.0, 1920.0).pit_tax
        assert moved > flat

    def test_intraday_pays_no_overnight_fees(self):
        m = CostModel()
        rt = m.round_trip(1884.0, nights=0)
        assert rt.position_fee == 0.0
        assert rt.collateral_fee == 0.0

    def test_overnight_costs_more_than_intraday(self):
        m = CostModel()
        assert m.round_trip(1884.0, nights=1).total > m.round_trip(1884.0).total

    def test_tick_value_identity(self):
        # A tick is 0.1 points and a point is 100,000 VND. Getting this wrong by a
        # factor of ten silently rescales every cost in the project.
        assert TICK_VALUE_VND == 10_000
        assert MULTIPLIER_VND == 100_000

    def test_slippage_enters_both_sides(self):
        free = CostModel(margin_rate=0.17, slippage_ticks_per_side=0.0)
        slipped = CostModel(margin_rate=0.17, slippage_ticks_per_side=0.5)
        diff = slipped.breakeven_ticks(1884.0) - free.breakeven_ticks(1884.0)
        assert diff == pytest.approx(1.0)   # 0.5 ticks each way
