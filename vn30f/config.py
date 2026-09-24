"""Instrument, session and cost constants for VN30 index futures.

Every number here is an institutional fact, not a modelling choice, so it lives in
one place and is cited. Modelling knobs belong with the model that uses them.

Sources are named inline. The margin rate is a policy variable VSDC can change, and
it sits inside the tax base, so a backtest spanning an announcement must vary it per
date rather than use the constant below -- see REGIME_BREAKS for the dates that
matter. The constant is the current value and the fallback.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "vn30f"
RAW = DATA / "raw"
CURATED = DATA / "curated"
LOGS = ROOT / "logs"

for _p in (RAW, CURATED, LOGS):
    _p.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------
TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# HNX derivatives session. The 08:45-09:00 ATO window is the one structural feature
# that has no equity analogue: futures price before the cash market opens, so the
# opening auction carries overnight information the index itself has not yet shown.
ATO_START = time(8, 45)
ATO_END = time(9, 0)
MORNING_START = time(9, 0)
MORNING_END = time(11, 30)
AFTERNOON_START = time(13, 0)
AFTERNOON_END = time(14, 30)
ATC_START = time(14, 30)
ATC_END = time(14, 45)

SESSION_OPEN = ATO_START
SESSION_CLOSE = ATC_END

# --------------------------------------------------------------------------
# Contract specification (HNX VN30 futures)
# --------------------------------------------------------------------------
MULTIPLIER_VND = 100_000       # VND per index point
TICK_SIZE = 0.1                # index points
TICK_VALUE_VND = MULTIPLIER_VND * TICK_SIZE   # 10,000 VND per tick
PRICE_BAND = 0.07              # +/-7% against the reference price

# --------------------------------------------------------------------------
# Costs. These make the strategy economics, so they are exact, not rounded.
# --------------------------------------------------------------------------
EXCHANGE_FEE_VND_PER_SIDE = 2_700     # HNX trading fee, per contract per side
VSDC_POSITION_FEE_VND_PER_DAY = 2_550  # per contract per account per day, overnight only
VSDC_COLLATERAL_FEE_MONTHLY = 0.000024  # 0.0024%/month on margin held
VSDC_COLLATERAL_FEE_MIN_VND = 100_000
VSDC_COLLATERAL_FEE_MAX_VND = 1_600_000

# Personal income tax on derivatives is levied on TURNOVER, not profit. This is the
# single most important cost fact for an intraday strategy in Vietnam: it is paid on
# every side regardless of whether the trade made money, so it scales with turnover
# and sets a hard floor on viable holding periods.
#
#   taxable = settlement_price * MULTIPLIER * n_contracts * initial_margin_rate / 2
#   tax     = taxable * 0.001
#
# Note the margin rate enters the tax base, which makes a policy variable part of the
# cost function -- the reason IM is tracked per-day rather than hardcoded downstream.
PIT_TURNOVER_RATE = 0.001

# VSDC initial margin rate. Announced on the 1st/10th/20th and effective at least two
# business days later; 17% has held since 2022-12-15.
INITIAL_MARGIN_RATE = 0.17
INITIAL_MARGIN_EFFECTIVE_FROM = "2022-12-15"

# Position limits (VSDC/SSC). The individual limit is the capacity ceiling any P&L
# claim has to respect, so it belongs in the backtest, not in a footnote.
POSITION_LIMIT_INDIVIDUAL = 5_000
POSITION_LIMIT_INSTITUTION = 10_000
POSITION_LIMIT_PROFESSIONAL = 20_000
ORDER_SIZE_LIMIT = 500        # contracts per single order

# --------------------------------------------------------------------------
# Regime breaks worth marking before any model sees the data
# --------------------------------------------------------------------------
# Decision 61/QD-VSD (signed 2022-05-16) changed the final settlement price from the
# index close to a trimmed 30-minute average (drop the 3 highest and 3 lowest values
# of the continuous session). First applied to VN30F2206, expiring 2022-06-16.
# Expiry-day microstructure before and after this date is not the same process.
REGIME_BREAKS = {
    "2018-07-18": "initial margin 13%",
    "2022-06-16": "final settlement price -> trimmed 30-min average (61/QD-VSD)",
    "2022-12-15": "initial margin 17%",
    "2025-05-05": "KRX trading system go-live",
}



# Recording targets. The front month carries essentially all the volume and the second
# month is needed for the calendar spread and roll gap. The cash index is NOT recorded
# live -- its board endpoint returns no prices -- it is backfilled as 1-minute bars.
RECORD_FUTURES = ["VN30F1M", "VN30F2M"]
