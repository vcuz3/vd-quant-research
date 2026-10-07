# Quant research

HYP_1 tests daily AUDUSD deviations from cross-asset relationships. The pipeline
acquires raw data and logs its provenance; cleaning is deferred.

## Structure

```text
src/acquisition/
  acquire.py       # All implementation, divided into sections and functions
  __main__.py      # Tiny launcher for python -m src.acquisition
  __init__.py      # Package declaration
configs/acquisition/
  datasets.toml    # Provider, instrument, units and request settings
  hyp_1.toml       # Default instruments, dates and bar frequency
warehouse/
  docs/            # Acquisition guide and methodology review
  reports/         # Six-field run and cost-review summaries
  data/raw/        # Native data and a manifest for each snapshot
  meta.sqlite      # Manifest index and internal resume/cost state
.env.example       # Copy to .env and add your provider settings
```

The implementation is consolidated in [acquire.py](src/acquisition/acquire.py).
Its sections cover environment loading, shared records, atomic storage, raw
inspections, the catalogue, legacy imports, HTTP access, each provider, cost
controls, instrument selection, reporting and the CLI. Each provider has separate
estimate/download functions, so selecting an instrument runs only its provider.
There is no separate plan command or plan file.

## Setup and credentials

Python 3.11+ is required. Public FRED/RBA downloads and offline tests use the
standard library. For IBKR or Databento, install the optional dependencies in
your virtual environment. Run commands from this repository directory:

```powershell
python -m pip install -r requirements-acquisition.txt
Copy-Item -LiteralPath .env.example -Destination .env
```

Copy the template once, then edit `.env`. The CLI automatically loads this
repository's `.env`; existing process environment variables take precedence.
Blank file values are skipped. Literal values, quotes, comments and an optional
`export` prefix are supported, without expansion or shell execution. `.env` is
ignored by Git. Programmatic callers can use `load_env()` from `acquire.py`.

| Setting | Purpose |
| --- | --- |
| `DATABENTO_API_KEY` | Databento estimates and downloads. |
| `IB_HOST` | Already logged-in TWS/Gateway host; default `127.0.0.1`. |
| `IB_PORT` | Your configured API port; default `4001`. |
| `IB_CLIENT_ID` | An unused connection identifier; default `71`. |

IBKR authentication happens in TWS/Gateway, with Read-Only API enabled; no broker
username/password goes in `.env`. Public FRED CSV and RBA XLSX need no API keys.

## IBKR setup after an update

In TWS, open **Edit ? Global Configuration ? API ? Settings**:

1. Enable **ActiveX and Socket Clients**.
2. Keep **Read-Only API** enabled for this data-only pipeline.
3. Set/confirm **Socket Port**, then click **Apply / OK**.
4. If TWS prompts to accept the local connection, accept it. Keep access limited
   to the local machine when the script runs on the same computer.

The script's `.env` must match that port. Standard defaults are TWS live `7496`,
TWS paper `7497`, Gateway live `4001`, Gateway paper `4002`; custom values are
valid if both sides match. `IB_CLIENT_ID=71` must be unused by other scripts.
Restart TWS after applying settings if the listener remains closed.
See [IBKR API setup](https://www.interactivebrokers.com/docs/tws-api/doc/tws-settings/introduction)
and [connection errors](https://www.interactivebrokers.com/docs/tws-api/doc/error-handling/error-codes).

Check the configured port once, without downloading data or requiring the SDK:

```powershell
python -m src.acquisition check-ibkr
```

This checks the TCP listener only; a successful result does not establish API
login, client-ID availability or market-data entitlements. For downloads, use
an environment with the optional provider dependencies installed.

IBKR transport errors/timeouts get **at most two retries (three attempts total)**,
with at least 10 and 20 seconds between attempts. Each attempt disconnects before
retrying. Provider rejections and empty responses are not retried. Any unresolved
IBKR failure stops the run, so later chunks do not repeatedly hit the provider.
The six-field report includes a short error in `note`. Fix the connection and use
`resume --report ...` to continue; completed snapshots are verified and reused.

## Choose instruments

Use `--instruments` with one or more names. `--all` selects all instruments in
`hyp_1.toml`, currently AUDUSD, Australian/US yields, VIX, ES, gold and copper.
Omitting the selection also uses that configured list. Daily iron ore and coal
remain unresolved and are excluded. The optional monthly iron series is separate.

| Instrument | Accepted name | Source | Frequency |
| --- | --- | --- | --- |
| AUDUSD | `audusd` | IBKR MIDPOINT bars | Configurable; default 1h |
| S&P 500 futures | `es`, `sp500` | Databento ES continuous | Configurable; default 1h |
| Gold futures | `gold`, `gc` | Databento GC continuous | Configurable; default 1h |
| Copper futures | `copper`, `hg` | Databento HG continuous | Configurable; default 1h |
| Australian 2-year yield | `au_2y` | RBA F2 | Native daily |
| US 2-year yield | `us_2y`, `dgs2` | FRED DGS2 | Native daily |
| VIX | `vix` | FRED VIXCLS | Native daily close |
| Optional monthly iron ore | `iron_ore_monthly` | FRED IMF series | Native monthly |

Examples of cost reviews:

```powershell
python -m src.acquisition estimate --instruments audusd --acknowledge-ibkr-entitlements
python -m src.acquisition estimate --instruments audusd gc --acknowledge-ibkr-entitlements
python -m src.acquisition estimate --all --acknowledge-ibkr-entitlements
```

The IBKR flag confirms existing entitlements and no incremental download charge.
Use it only when that is true. An estimate obtains cost metadata without
requesting observation data. Unknown costs or entitlements block downloads.

For public data, or AUDUSD after confirming entitlements:

```powershell
python -m src.acquisition download --instruments us_2y vix au_2y
python -m src.acquisition download --instruments audusd --acknowledge-ibkr-entitlements
```

For paid data, first review `estimate` with the same instruments, dates and
frequency, then use `download` with `--approve-cost` and
`--max-cost-usd <approved_amount>`. The review must be less than 24 hours old.
Downloads print and save fresh quotes before requesting observations. The default
cap is USD 0. Failed attempts remain reserved on resume because billing can be
uncertain; the cap limits estimates and is not a provider billing guarantee.

## Dates and hourly bars

Set `start`, exclusive `end`, and `frequency = "1h"` in
[configs/acquisition/hyp_1.toml](configs/acquisition/hyp_1.toml). CLI overrides
apply to a single invocation:

```powershell
python -m src.acquisition estimate --instruments audusd gc --start 2025-01-01 --end 2026-01-01 --frequency 1h --acknowledge-ibkr-entitlements
```

Hourly/daily AUDUSD requests now use calendar-month boundaries (`chunk_months = 1`
in the dataset register), giving **201 chunks** for the configured 2010-01-01 to
2026-10-01 window. Partial edge months use the requested start/end. Minute-bar
AUDUSD overrides use daily chunks to keep requests modest.

`boundary_padding_days = 1` adds a provider duration day when fetching IBKR bars.
The duration is expressed in days, since the live `1 M` test omitted the earlier
UTC hours of the first date. Native response overlaps can extend beyond the
requested UTC window; they are retained in raw data, with in-window/outside-window
counts recorded in the manifest. Cleaning is still deferred.

**Start a new `download` to use monthly chunks.** Resuming an old report preserves
its original request boundaries, including seven-day chunks. Existing snapshots
remain available; the two final monthly test snapshots can be reused by new runs.

Supported bar frequencies are `1m`, `1h` and `1d`. The code maps them to IBKR's
bar-size setting and Databento's matching native OHLCV schema. Hourly is already
the default. RBA yields, DGS2 and VIXCLS keep their native daily frequency; the
monthly iron series stays monthly. No hourly values are fabricated or resampled.
See [IBKR bar sizes](https://interactivebrokers.github.io/tws-api/historical_bars.html)
and [Databento schemas](https://databento.com/docs/schemas-and-data-formats).

## Reports, fingerprints and resume

`warehouse/reports/` contains one short JSON summary per download or cost review,
with exactly `date_ran`, `amount_usd`, `start_time`, `finish_time`, `status`, and
`note`. Times are UTC; finish time is blank while running or when an old
interrupted run has no known finish time. Download amounts are reserved estimated
USD, including uncertain failed attempts; estimate summaries show quoted USD.
Unknown quotes are `null`. Actual provider billing requires reconciliation.

The console prints the cost before data requests and the final summary, without
per-chunk progress. Resume settings, cost reviews and spending reservations are
stored internally in `warehouse/meta.sqlite`. No `.events.jsonl` file or verbose
execution estimate is generated. Native source metadata stays with raw manifests.

A **request fingerprint** is a deterministic SHA-256 hash of request settings.
It identifies matching source/instrument/date/frequency/price requests for cache
reuse and distinguishes hourly from minute data. A **file checksum** verifies
that downloaded bytes have not changed. Both must match before data is reused.
The request-set fingerprint connects a reviewed quote with the same selection
in a later download command. Hashes are consistency checks, not proof of data
quality or complete historical coverage.

Use the report path printed by a download:

```powershell
python -m src.acquisition status --report warehouse/reports/<run_id>.json
python -m src.acquisition resume --report warehouse/reports/<run_id>.json
```

Pass the same spending/entitlement flags on resume when needed. Use
`download --refresh` for new snapshots; review its costs using `estimate --refresh`
first. Refresh resumes reuse only artifacts from that run. Ordinary downloads
reuse exact verified requests. Snapshots are never overwritten. Request chunk
boundaries must match for automatic reuse; changing dates may overlap old chunks.

Each raw snapshot manifest logs UTC acquisition times, source, symbol,
granularity, units, date ranges, inspections, byte lengths, hashes and cost
approval. `warehouse/meta.sqlite` stores the artifact index and internal resume/cost state.
`python -m src.acquisition rebuild-catalogue` rebuilds only its artifact index;
back up this database to preserve resume history and cost reviews. Generated data/reports are ignored
by Git and need separate backup. Removing a summary does not erase the database history or raw snapshots.
Use `--refresh` to acquire new snapshots.

Previously acquired ES, GC and HG Databento archives have also been copied into
`warehouse/data/raw/databento/`, retaining their native 1-second/1-minute bars.
Their acquisition timestamps use the last available observation as requested,
with the actual import time recorded separately. See the
[imported futures inventory](warehouse/docs/imported_futures.md) for coverage,
provenance and the separate HG archive containing contracts and spreads.
These legacy imports are indexed but do not automatically satisfy new download
requests or convert to hourly data.

## Research and verification

- [Acquisition and recovery details](warehouse/docs/acquisition.md)
- [Definitions and historical methodology changes](warehouse/docs/METHODOLOGY.md)
- [Machine-readable methodology references](warehouse/docs/methodology.json)
- [Imported ES, GC and HG archives](warehouse/docs/imported_futures.md)

The acquired RBA 2-year series has nonmissing values only from 2013-09-02;
complete historical coverage and publication clocks still require research.
IBKR/Databento require live verification with your account and dependencies.

```powershell
python -m unittest discover -s tests -v
```

Offline tests cover cost approval, aliases, frequency/date overrides, raw
inspections, immutable storage, cache corruption, report-based resume and `.env`
loading. No paid provider requests are made by the tests.
