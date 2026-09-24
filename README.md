# VN30 Index Futures: Order Flow and the Turnover-Tax Floor

VN30 index futures are the only liquid instrument in Vietnam that can express a
short-horizon view in both directions, and personal income tax on them is levied on
turnover, not profit. This study asks two questions of the front-month contract:
does exchange-labelled trade-flow imbalance predict the mid-price over seconds to
minutes, and does any such edge survive the tax?

Signed flow is strongly contemporaneous with price (correlation +0.82 over 30-second
windows) and weakly predictive of the next 30 to 60 seconds (+0.12 to +0.13,
positive in all four usable sessions, 95% intervals just including zero), decaying
to zero by five minutes. An out-of-sample rule trading the strongest 20% of signals
earns +1.4 to +2.4 ticks per trade gross at 30 to 60 second holds, against a taker
round-trip cost of about 6 ticks: 2.2 of spread and 3.8 of fees and tax. Net
expectancy at the best horizon is -3.7 ticks per trade (t = -3.1). The fees and tax
alone exceed the best gross edge, so no improvement in execution changes the sign;
the signal would have to be about 2.6 times stronger.

The predictive evidence rests on 6.3 hours of order book across four sessions and is
suggestive, not conclusive. The economic conclusion does not depend on it: cost
exceeds gross edge by a margin the sample size cannot close.

---

## 1. Why futures

An earlier study on about 400 HOSE equities (not part of this repository) found
5-day returns indistinguishable from noise -- no model beat a zero forecast, rank
IC 0.007 -- while volatility was forecastable (rank IC 0.63, most of it
persistence). Neither result is tradeable on the cash market: short selling is
prohibited and settlement is T+2, so a forecast can at best tilt a long-only book.
VN30 futures are the one venue where a Vietnamese signal becomes a position in
either direction and a P&L that can be simulated honestly.

## 2. Market structure and costs

| | |
|---|---|
| Contract | VN30 index future, HNX; front month carries nearly all volume |
| Multiplier | 100,000 VND per index point |
| Tick | 0.1 point = 10,000 VND |
| Price band | +/-7% of reference |
| Sessions (UTC+7) | opening auction 08:45-09:00; continuous 09:00-11:30 and 13:00-14:30; closing auction 14:30-14:45 |
| Expiry | third Thursday; final settlement is a trimmed 30-minute index average since 2022-06-16 |

Round-trip cost per contract has two fixed parts:

- Exchange fee: 2,700 VND per side.
- Personal income tax: 0.1% of a "transfer value" defined as
  `price x 100,000 x contracts x initial_margin / 2`, paid on every side whether
  the trade made money or not.

The initial margin rate, a policy variable set by VSDC, sits inside the tax base, so
a margin announcement changes the cost function itself. The implementation
(`vn30f/execution/cost_model.py`) reproduces the regulator's worked example exactly
(1 contract at 1,000 points, 17% margin: 8,500 VND per side).

Break-even move for one round trip at 1,884 points, before any spread:

| Margin regime | Exchange fees | Tax | Total | Break-even |
|---|---|---|---|---|
| 10% (to 2018-07) | 5,400 VND | 18,840 VND | 24,240 VND | 2.42 ticks |
| 13% (2018-07 to 2022-12) | 5,400 | 24,492 | 29,892 | 2.99 ticks |
| 17% (since 2022-12-15) | 5,400 | 32,028 | 37,428 | 3.74 ticks |

Because the tax scales with the index level while the tick value is fixed, the
break-even in ticks drifts with the market: 2.89 to 4.14 ticks over the 1-minute
sample (2025-05 to 2026-09), median 3.81. The median quoted front-month spread in the recorded book is
2 ticks, so a taker pays roughly 6 ticks per round trip. The fiscal part is the
larger one.

## 3. Data

### 3.1 Sources

There is no public tick dataset for VN30 futures and no purchasable one reachable
from outside Vietnam. The data here was recorded for this study.

- **DNSE LightSpeed** (the planned source) advertises authentication-free
  market-data topics. On contact from abroad the WebSocket connects in 2.5 s, but
  every anonymous MQTT CONNECT is refused ("Not authorized") on MQTT 3.1.1 and 5,
  with guest and public credentials, and on the pre-KRX host.
  `vn30f/ingest/probe_dnse.py` reproduces this.
- **KBS public HTTP endpoints**, called directly: the current session's trades with
  the exchange's aggressor flag, and board snapshots with three depth levels, open
  interest and foreign flow. The aggressor flag removes the need for a Lee-Ready or
  tick-rule classifier.
- **VCI and KBS 1-minute and daily bars**, stored side by side under a `source`
  column so that disagreement stays visible.

### 3.2 What exists, and what can be recovered

| Dataset | Coverage | Recoverable later |
|---|---|---|
| Trades with aggressor side | 8 recorded sessions | No: the endpoint serves only the current session |
| Board snapshots (~2 s cadence) | 8 sessions, partial days | No: a snapshot not taken is gone |
| 1-minute bars, front month and index | 327 trading days, 2025-05-30 to 2026-09-21 | Rolling provider window; shrinks daily |
| Daily bars | 2018-08-20 to 2026-09-21, 97 contract rolls | Yes |
| Bars for an individual contract | from listing | No: history disappears at expiry |

### 3.3 Integrity checks

- **Volume identity.** Each trade carries the exchange's running volume total, so
  the sum of recorded trade sizes must equal the largest running total seen. Any
  shortfall is exactly the volume missing; any excess is double counting. This check
  is part of every recording sweep and is the admission test for the order-flow
  study.
- **Cross-source agreement.** KBS and VCI agree exactly on 99.3% of 32,917
  overlapping index bars; median difference 0.0 points, maximum 4.55.
- **Coverage.** On the median day 0.00% of the 240 expected continuous-session
  minutes are missing (mean 0.34%); 2 of 327 days exceed 2%.

### 3.4 Capture record

Recording started on 2026-08-17. Of 26 trading days to 2026-09-24, trades were
recorded on 8, and 4 are complete full days that reconcile to the exchange counter
(one of them to within a single contract).
The recorder runs on a workstation; when the machine sleeps or shuts down during
the session the process is killed and the day is lost, because the trade endpoint
cannot be queried for a past date. This, not the code, is the binding constraint on
sample size.

| Session | Front month | Trades | Gap vs exchange counter | Continuous-session book | In study |
|---|---|---|---|---|---|
| 2026-08-17 | VN30F2608 | 50,534 | 0 | 1,305 states | yes |
| 2026-08-19 | VN30F2608 | 28,876 | 0 | 3,793 | yes |
| 2026-08-21 | VN30F2609 | 60,522 | 0 | none | no: no book |
| 2026-08-24 | VN30F2609 | 36,947 | +162 | 87 | no: identity fails |
| 2026-08-28 | VN30F2609 | 46,614 | 0 | 2,096 | yes |
| 2026-09-03 | VN30F2609 | 17,786 | -37,251 | 1,128 | no: identity fails |
| 2026-09-09 | VN30F2609 | 44,342 | +1 | 2,050 | yes |
| 2026-09-22 | VN30F2610 | 36,652 | +139 | 300 | no: identity fails |

The study sample is 9,244 distinct book states, 6.3 hours of continuous-session
order book.

## 4. Results

### 4.1 Move size against the cost floor

Absolute change in the front-month 1-minute close over h minutes, measured inside a
single continuous block so that no move spans lunch, an auction or a night.
2025-05-30 to 2026-09-21. Closes carry bid-ask bounce, which inflates move size, so
this comparison flatters intraday trading.

| Horizon | Median move | Mean move | P(move >= fees + tax) | P(move >= fees + tax + spread) | n |
|---|---|---|---|---|---|
| 1 min | 6.0 ticks | 8.5 | 68.3% | 52.8% | 77,561 |
| 5 min | 13.0 | 19.0 | 84.6% | 76.2% | 74,949 |
| 15 min | 23.0 | 32.7 | 90.9% | 86.0% | 68,419 |
| 30 min | 33.0 | 46.9 | 93.6% | 89.9% | 58,624 |

The fees-and-tax floor is 64% of a median one-minute move and 12% of a median
30-minute move. The tax does not rule out intraday trading; it pushes the
viable holding period out toward tens of minutes. Order-flow predictability is known
to decay within minutes. Whether the two windows overlap is what sections 4.2-4.4
measure.

Move size is not capturable edge: a strategy must forecast the direction of the
move, and the relevant quantity is expected directional gain per trade.

### 4.2 Order flow: contemporaneous impact

Trade-flow imbalance `x` is net aggressor volume (buyer-initiated minus
seller-initiated contracts) over the trailing 30 seconds, evaluated at each board
snapshot. Trades carry whole-second exchange stamps, so only trades stamped at or
before `t - 1s` are counted; the trades stamped in the snapshot's own second may
postdate it and are dropped. All returns are mid-to-mid, so a signal is never
scored against the bid-ask bounce it helped create.

Correlation between `x` and the mid change over the same 30 seconds is +0.815
(95% block-bootstrap interval +0.774 to +0.847, n = 9,002). Price moves 2.25 ticks
per 100 contracts of net aggressor flow. This is the expected impact relation and
confirms that the recorded flow and the recorded book describe the same market.

Real order-flow imbalance in the Cont-Kukanov-Stoikov sense counts every book
event and needs event-level data. At a ~2 second snapshot cadence the book moves
between reads, so the aggressor-signed trade flow, which is exact, is the variable
used here.

### 4.3 Order flow: predictive power

Correlation between `x` and the forward mid change. Intervals are from a block
bootstrap over 5-minute blocks, because forward windows overlap and returns are
autocorrelated.

| Horizon | corr(x, forward move) | 95% interval | Sessions with positive correlation | Median absolute move | n |
|---|---|---|---|---|---|
| 10 s | +0.070 | -0.015 to +0.134 | 3 of 4 | 2.0 ticks | 9,068 |
| 30 s | +0.118 | -0.007 to +0.205 | 4 of 4 | 3.5 | 9,026 |
| 60 s | +0.129 | -0.021 to +0.229 | 4 of 4 | 5.5 | 8,967 |
| 120 s | +0.051 | -0.101 to +0.151 | 2 of 4 | 7.5 | 8,838 |
| 300 s | +0.010 | -0.104 to +0.101 | 1 of 4 | 10.5 | 8,449 |

The profile is the one the microstructure literature predicts: continuation that
peaks between 30 and 60 seconds and is gone by five minutes. The sign is consistent
across sessions at the peak, but every interval includes zero. With four sessions
this is evidence of a small effect, not a demonstration of one.

### 4.4 Order flow: an out-of-sample trading rule

Rule: at a board snapshot, if `|x|` exceeds the 80th percentile of `|x|` computed on
the other sessions only (leave-one-session-out, so the threshold never sees the
session it trades), take one contract in the direction of `x`. Enter and exit as a
taker, paying half the quoted spread on each side, hold h seconds, no overlapping
positions. Costs use the cost model at the actual entry and exit prices.

Ticks per trade:

| Hold | Trades | Hit rate | Gross | t(gross) | Spread paid | Fees + tax | Net | t(net) |
|---|---|---|---|---|---|---|---|---|
| 10 s | 448 | 50% | +0.73 | +2.09 | 2.14 | 3.82 | -5.22 | -14.91 |
| 30 s | 201 | 49% | +1.38 | +1.69 | 2.28 | 3.82 | -4.72 | -5.80 |
| 60 s | 131 | 52% | +2.35 | +1.96 | 2.26 | 3.81 | -3.72 | -3.10 |
| 120 s | 87 | 43% | +1.72 | +0.71 | 2.06 | 3.81 | -4.15 | -1.72 |
| 300 s | 53 | 32% | -4.71 | -1.11 | 2.22 | 3.81 | -10.73 | -2.53 |

The gross edge is positive and of the size the correlations imply, with hit rates
near 50%: the gain comes from larger moves in the signalled direction, not from
being right more often. The best gross edge, +2.35 ticks at 60 seconds, is smaller
than fees and tax alone (3.81 ticks). A maker who paid no spread would still lose
about 1.5 ticks per trade; a taker loses 3.7. To break even as a taker the signal
would need to deliver about 6.1 ticks per trade, 2.6 times the best observed.

The tension identified in 4.1 resolves against intraday order-flow trading: the
signal lives in the 30 to 60 second window, and the turnover tax requires a holding
period over which the signal has already decayed.

### 4.5 Contract roll

The provider's continuous front-month series splices contracts without adjusting
for the price gap between them. Differencing it treats each contract switch as a
price move. On expiry day both the outgoing and incoming contracts trade, so the gap
is observable as that day's calendar spread. The adjusted series shifts each
historical segment by the sum of later gaps; the raw series is kept alongside it,
since only raw prices are tradeable levels.

Daily bars, 2018-08-20 to 2026-09-21:

- 97 rolls; 95 with an observed expiry-day spread, 2 without (zero gap, flagged).
- Gap mean -3.28 points, standard deviation 12.58, range -78.0 to +30.3.
- Cumulative adjustment on the oldest bar: -311.2 points.
- Median absolute daily move on roll-crossing days: 8.85 points raw, 5.90 adjusted,
  against 7.70 on ordinary days. The adjusted move across each roll equals the
  incoming contract's own return to within 2.3e-13 points, checked on the 82 rolls
  where both days are present.

The expiry-day close of the outgoing contract is the weakest price in the series: it
converges on a settlement average and trades thinly. On 2020-05-21 it printed at its
+7% band limit (864.0 against 786.0 for the next contract), a -78 point "gap" that is
almost certainly not carry. Measured one trading day before expiry instead, the
gaps have standard deviation 7.45 and sum to -160.4 points rather than -311.2. The
choice of splice day changes the level of an eight-year adjusted series by roughly
150 points, about 8% of the index. No result in section 4 depends on the adjusted
series; rolling one day early is the correction to make before any multi-year
backtest.

### 4.6 Basis

Front month minus the VN30 index on aligned 1-minute closes, 2026-02-23 to
2026-09-21, 32,770 minutes over 145 days: mean +0.39 points, median +0.66, standard
deviation 5.37. As an annualised rate (days to expiry of 5 or more) the median is
+0.1%.

| Days to expiry | Days | Mean basis | Median |
|---|---|---|---|
| 0-3 | 28 | +2.62 | +2.40 |
| 4-10 | 35 | -0.03 | +0.07 |
| 11-20 | 38 | +0.47 | +0.74 |
| 21+ | 44 | -0.78 | -0.77 |

Under any positive net carry -- VND funding rates above the index dividend yield --
fair value sits above spot. A basis near zero at an annual rate is in the direction
the short-sale prohibition predicts: the arbitrage that would lift a cheap future
requires selling the basket. The magnitude of the gap to fair value is not tested
here, since it needs a funding rate and a dividend schedule the study does not
model. The days-to-expiry buckets show no stable term structure over about seven
expiry cycles; the daily standard deviation is larger than the bucket means.

## 5. Data failures and how they were caught

Each of these reached working code or a running system. Most did not raise an
error, which is why they are listed.

1. **The planned source did not exist.** DNSE refuses anonymous connections despite
   its documentation (section 3.1). Replaced by direct HTTP to KBS.
2. **A wrapper library terminated the recorder.** The `vnstock` client caps
   unregistered callers at 20 requests per minute and calls `sys.exit` on breach,
   which killed the recorder mid-session. The recorder now calls the endpoints
   directly; `vnstock` remains only in the bar backfill.
3. **The trade timestamp is not a trade identity.** Fetching the same page twice
   returns the same trade (identical running total, price and size) with a different
   sub-second stamp, and the stamp repeats for about a fifth of rows within one page.
   De-duplicating on timestamp, price and size admitted the same trades repeatedly
   and inflated one session to 1.5 times the exchange's own volume. Trades are now
   keyed on the running volume total. Caught by the volume identity: 310,612 recorded
   against a counter of 200,484.
4. **A network error was read as the end of the data.** On wake from sleep the first
   page request failed, the sweep treated the exception as "no more pages", and the
   log reported zero missing trades for a session missing most of its morning. Only
   an empty page now ends a sweep; a failed page is retried and then raises.
5. **One pass over the endpoint is never complete.** Pagination is by offset over a
   live dataset, so any single pass skips scattered rows. Sweeps repeat until the
   volume identity balances.
6. **The recorder stopped at lunch.** The capture loop ran while the market was
   trading, which is false from 11:30 to 13:00, so a run started in the morning
   recorded the morning and exited. It now runs until the close and idles through
   the break. The recovery sweep, which fired on only one exit code, now also fires
   when the operating system kills the process.
7. **2026-09-03 is internally inconsistent.** Recorded trade sizes sum to more than
   the exchange counter (gap -37,251), and only 42% of consecutive counter
   increments equal the trade size, against 100% on clean days. It was the first
   session after a three-day holiday; the cause was not identified. The session is
   excluded.
8. **Expiry-day prints are unreliable** (section 4.5).

## 6. Limitations

- **Sample size.** Four sessions and 6.3 hours of book. The predictive correlations
  are not significant on their own; the negative net result is.
- **Book cadence.** Two-second snapshots undersample the book, which rules out
  event-level order-flow measures and understates what a co-located participant
  could observe. A faster feed would sharpen the signal; it would not change the
  cost of trading on it.
- **Execution model.** Fills are at the quoted touch with no queue, no market
  impact and no latency. Every one of those omissions favours the strategy.
- **Single live source.** Trades and the book come from one broker's public
  endpoint. Bars are cross-checked against a second provider; ticks cannot be.
- **Capture reliability.** A workstation that sleeps is not a recording host.
  An always-on machine is the precondition for a larger sample.

## 7. Reproduction

Market data is not included: it is large, and redistribution is not permitted. It
must be recorded.

```
pip install -r requirements.txt

python -m vn30f.ingest.probe_dnse --seconds 45      # section 3.1, during trading hours
python -m vn30f.ingest.kbs_recorder --wait-for-open  # one session; run every trading day
python -m vn30f.ingest.backfill                      # 1-minute and daily bars
python -m vn30f.ingest.curate                        # raw parts to typed daily files
python -m vn30f.quality                              # data gate, section 3.3

python -m vn30f.studies.cost_horizon                 # sections 4.1, 4.5, 4.6
python -m vn30f.studies.order_flow                   # sections 3.4, 4.2-4.4
python -m vn30f.execution.cost_model                 # section 2

python -m pytest -q
```

`scripts/vn30f_record.ps1` runs one trading day end to end (record, recover,
backfill, curate) for a Windows scheduled task. It waits for the Hanoi open against
the exchange clock, so it needs no re-timing across daylight-saving changes.

## 8. Repository

```
vn30f/
  config.py              contract, session and cost constants, with sources
  calendar_vn.py         session phases, expiry dates, front-month resolution
  contracts.py           roll dates, contract master, back-adjusted series
  quality.py             data gate: coverage, cross-source drift, reconciliation
  ingest/
    probe_dnse.py        the DNSE authentication test
    kbs_api.py           direct client for the KBS endpoints
    kbs_recorder.py      live capture of trades and board snapshots
    backfill.py          1-minute and daily bars from two providers
    curate.py            typing, de-duplication, mid, spread, imbalance
  execution/
    cost_model.py        fees, turnover tax, break-even
  features/
    microstructure.py    trade-flow imbalance, book features
    basis.py             futures minus index
  studies/
    cost_horizon.py      move size vs cost, roll, basis
    order_flow.py        impact, predictability, out-of-sample trading rule
tests/test_vn30f.py      43 tests: regulator's tax example, calendar rules,
                         roll alignment, trade identity, no look-ahead
scripts/vn30f_record.ps1 daily capture job
```
