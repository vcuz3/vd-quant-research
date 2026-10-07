# Dataset definitions and methodology review

Reviewed 5 October 2026. Change-review window: 5 October 2011–5 October 2026.
The configured acquisition window starts in 2010 to allow model warm-up. This
document describes measurement; it does not select a signal clock or clean data.
Confirmed changes below have primary-source references. An incomplete change
history means unknown, not evidence that a methodology never changed.

## AUDUSD — IBKR

AUDUSD is USD per AUD. The selected instrument is IBKR CASH on IDEALPRO, using
MIDPOINT bars: open is the starting quoted midpoint, high/low are extrema of
midpoints, close is the final midpoint. The midpoint is `(bid + ask) / 2`;
midpoint OHLC cannot be reconstructed by averaging independently aggregated
bid and ask OHLC. The bar is a vendor quote observation, not a consolidated
FX transaction price. Volume is not applicable to MIDPOINT, BID or ASK.
[IBKR historical-bar definitions](https://interactivebrokers.github.io/tws-api/historical_bars.html).

Store returned intraday times in their native UTC representation and retain all
provider fields. BID and ASK would be separate acquisitions. They do not make
every historical bar an attainable execution price.

No complete, dated 15-year history of IBKR FX liquidity-provider composition,
historical quote filtering or archive corrections was established from the
public documentation examined. Record SDK/TWS versions where available and
keep new snapshots when refreshing. Do not inherit the old repo's assertion
that all market-price snapshots are automatically point-in-time safe.
IBKR documents history limitations and an earliest-data query; neither a
successful request nor a checkpoint proves complete coverage.
[Limitations](https://interactivebrokers.github.io/tws-api/historical_limitations.html),
[earliest-data query](https://interactivebrokers.github.io/tws-api/head_timestamp.html).

## Australian 2-year yield — RBA F2

`FCMYGBAG2D` is an interpolated Australian government bond yield at two years,
in percent per annum. The current F2 workbook describes RBA assessments of
closing bond yields, informed by market participants and RBA judgment. It does
not establish a replicable historical curve-fitting algorithm. Retain the
original Data and Notes sheets; do not assume it equals an executable quote or
the US Treasury par-curve methodology.
[Original daily workbook](https://www.rba.gov.au/statistics/tables/xls/f02d.xlsx).

On **31 March 2023**, RBA announced deletion of pre-2021 history in several yield
series and calculation changes for commercial reasons. On **29 December 2023**,
F2 series were replaced by RBA closing-yield assessments; publication became
weekly with observations two business days behind. Observed daily frequency
must be distinguished from that release schedule.
[Dated RBA notices](https://www.rba.gov.au/statistics/tables/changes-to-tables.html).

The workbook actually retrieved in the public smoke run has table dates from
2013-05-20, but nonmissing 2-year values only from **2013-09-02**, through
2026-09-30 (3,288 nonmissing observations). Thus the earlier deletion notice
must not be used to infer today's exact file coverage. Current coverage is
still insufficient for 15 years plus warm-up. An earlier source and its
measurement comparability need separate verification. Historical release
timestamps/vintages remain unresolved.

## US 2-year yield — FRED DGS2

FRED distributes the Federal Reserve H.15 two-year constant-maturity Treasury
yield, daily, in percent and not seasonally adjusted. It is a fitted maturity
point, not a continuously two-year-to-maturity single bond, policy rate or
two-year total return. [FRED DGS2](https://fred.stlouisfed.org/series/DGS2).

Treasury's par curve uses indicative bid-side quotations near 15:30 US Eastern,
not executed trades. On **6 December 2021**, monotone-convex construction
replaced the quasi-cubic Hermite spline. Earlier published rates remain
official; this change does not justify retrospectively rewriting them.
The current method bootstraps forward rates and interpolates them to construct
par yields. Treasury usually publishes by 18:00 Eastern, with possible delays;
FRED can distribute later. The 15:30 input clock is not publication time.
[Treasury methodology](https://home.treasury.gov/policy-issues/financing-the-government/interest-rate-statistics/treasury-yield-curve-methodology),
[change information](https://home.treasury.gov/policy-issues/financing-the-government/yield-curve-methodology-change-information-sheet).

Mark 2021-12-06 for later sensitivity checks. Keep native percentage units and
missing markers; a later yield difference must use compatible units. Latest
FRED CSV is a download-time snapshot, not a historical release-vintage archive.

## Databento GLBX.MDP3 — common bar and roll conventions

OHLCV is aggregated from trades: first, highest, lowest, last price, and total
volume over the interval. `ts_event` labels its inclusive start; the schema
describes aggregation using trade-message `ts_recv`. Empty intervals produce no
bar. `ohlcv-1d` follows UTC dates rather than an exchange trading session. Vendor
choices on trade conditions and retroactive breaks can differ; a full historical
implementation changelog was not established here.
[OHLCV documentation](https://databento.com/docs/schemas-and-data-formats/ohlcv).

`ES.v.0`, `GC.v.0` and `HG.v.0` select the highest-volume contract based on the
previous day's trading volume. They provide actual, **unadjusted** prices:
contract changes can produce gaps. Preserve dated mappings and definitions,
including instrument IDs, rather than collapsing identifiers across years.
The chosen roll rule is a measurement convention, distinct from an exchange's
settlement active month. [Symbology](https://databento.com/docs/standards-and-conventions/symbology),
[continuous contracts](https://databento.com/docs/examples/symbology/continuous).

These acquisitions request one-minute bars and definitions. No back-adjustment,
session resampling or roll-return calculation is performed. Exchange settlements
are not included in OHLCV: acquiring settlement statistics would be an additional
quoted request. Degraded/missing dataset days need verification before research.

## Equity factor — ES futures

ES is an exchange-traded price of exposure to the S&P 500; it is not the cash
index, an ETF adjusted close or an equity total-return index. The underlying
index is float-adjusted market-capitalization weighted, with constituents and
share counts maintained by S&P DJI. Futures also reflect financing, dividends,
expiry and basis. [S&P DJI index definition](https://www.spglobal.com/spdji/en/research-insights/index-literacy/the-sp-500-and-the-dow/),
[CME final settlement](https://www.cmegroup.com/trading/equity-index/settlement.html).

On **26 October 2020**, daily settlement determination moved from 15:15 to
15:00 Central. The amended lead-month calculation uses the 14:59:30–15:00
settlement window. This matters if settlement series or settlement-based cutoffs
are later used; it does not turn Databento's bar close into a settlement.
[CME SER-8591](https://www.cmegroup.com/notices/ser/2020/09/SER-8591.pdf).

Normal constituent/corporate-action maintenance is embedded in the underlying
index, not an instruction to adjust ES prices. An exhaustive S&P eligibility,
corporate-action and CME session-rule chronology remains unverified.

## Gold and copper — GC and HG futures

GC is quoted in USD per troy ounce, with a standard 100-troy-ounce contract;
HG is USD per pound, with a standard 25,000-pound contract. Both are physically
deliverable futures, not London gold fixes or LME copper assessments. Futures
basis and rolls are real distinctions from spot prices.
[GC specifications](https://www.cmegroup.com/markets/metals/precious/gold.contractSpecs.html),
[HG specifications](https://www.cmegroup.com/markets/metals/base/copper.contractSpecs.html).

On **23 October 2017**, CME standardized settlement methodology for these metals:
active-month VWAP and tiered calendar-spread criteria for other months, with
fallbacks where market evidence is insufficient. The filing specifies active
GC's 13:29–13:30 Eastern window and HG's 12:59–13:00 window. The amendment is
relevant to settlement observations, not a retroactive OHLCV adjustment.
[CME filing 17-358, before/after procedures](https://www.cmegroup.com/market-regulation/rule-filings/2017/09/17-358_1.pdf).

No exhaustive record of every deliverable-brand, delivery-location, session,
feed-normalization or historical correction change was validated. Preserve
definitions at acquisition and never infer constant historical tick sizes or
economics solely from today's specifications. The separate volume-based
continuous roll convention applies to both datasets.

## VIX — FRED VIXCLS / upstream Cboe

VIX estimates annualized 30-day S&P 500 volatility from SPX option bid/ask
midquotes. It uses option-price-weighted variance across strikes and interpolates
eligible expiries to a 30-day horizon, then takes the square root and scales by
100. A value of 20 represents 20% annualized implied volatility, not a 20% daily
move. `VIXCLS` is the daily closing index; it is not VIX futures or the special
opening quotation used to settle VIX derivatives.
[Cboe FAQ](https://www.cboe.com/tradable_products/vix/faqs),
[FRED daily-close series](https://fred.stlouisfed.org/series/VIXCLS).

On **6 October 2014**, Cboe included Friday weekly SPX options alongside monthly
options. This alters the expiry interpolation inputs across the sample.
[Cboe research timeline](https://cdn.cboe.com/resources/education/research_publications/VIXInterpolationWhitepaper.pdf).
On **15 April 2016**, overnight VIX dissemination began; this is a dissemination
change and is not proof that FRED changed its closing-value convention.
[Cboe announcement](https://ir.cboe.com/news/news-details/2016/CBOE-to-Start-Overnight-Dissemination-of-CBOE-Volatility-Index-VIX-April-15-04-13-2016/default.aspx).

Record the 2014 break explicitly. Exact historical FRED ingestion delays,
correction vintages and all subsequent filtering-rule revisions remain unknown.
Original daily closes are also published directly by Cboe, but the configured
adapter uses FRED to keep one acquisition route.
[Cboe historical index files](https://www.cboe.com/tradable-products/vix/vix-historical-data/).

## Iron ore — source decision still open

FRED `PIORECRUSDM` is an **IMF** monthly global-price series, USD per metric ton,
with period-average nominal prices. The old Research code's World Bank comment
is not authoritative. It cannot supply daily repricing without changing the
research variable. [FRED/IMF definition](https://fred.stlouisfed.org/series/PIORECRUSDM).

For a daily physical alternative, Platts IODEX is a normalized delivered-to-China
price assessment; its methodology and grade are part of the variable definition.
On **2 January 2018**, TSI-62% and IODEX methodologies were aligned and the series
became identical. [Platts merger notice](https://www.spglobal.com/energy/en/pricing-benchmarks/our-methodology/subscriber-notes/070617-platts-to-merge-tsi-62-amp-iodex-price-series-from-jan-2018).
On **2 January 2026**, IODEX changed from 62% to 61% Fe with higher impurity
specifications. A calculated transitional spread supports an implied 62% value;
it does not mean the underlying benchmark stayed unchanged.
[Platts specification notice](https://www.spglobal.com/energy/en/pricing-benchmarks/our-methodology/subscriber-notes/010226-platts-updates-iodex-quality-specifications-to-reflect-61-fe-effective-jan-2-2026).

SGX-style iron-ore futures are another variable: traded contract prices versus
underlying assessment and monthly final settlement must be distinguished. Source,
contract, license, API, coverage and quote are unresolved. These Platts changes
must not automatically be attributed to the IMF series or every iron-ore contract.

## Coal and remaining research decisions

Coal appears in HYP_1. Copper and VIX are additional candidates requested by the
user; they are not silent coal substitutes. A thermal versus coking benchmark,
grade, port/delivery basis, spot versus futures convention and daily source have
not been selected. No unsupported coal-methodology claims are registered.

Before modelling, resolve the earlier Australian 2-year history, signal clock,
data availability at that clock, and futures roll-return convention. An
observation date and download timestamp alone cannot establish information
available to the historical signal. A change flag warrants investigation;
it does not demonstrate a statistically measurable discontinuity.
