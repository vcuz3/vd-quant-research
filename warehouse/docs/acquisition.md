# Raw-data acquisition

## Implementation and commands

The acquisition implementation lives in `src/acquisition/acquire.py`, with
sections for settings, shared records, storage, inspections, catalogue, legacy
archives, HTTP, FRED, RBA, IBKR, Databento, costs, request selection, execution and
CLI. `__main__.py` is a small launcher. Commands run from the repository root:

```powershell
python -m src.acquisition estimate --instruments us_2y vix au_2y
python -m src.acquisition download --instruments us_2y vix au_2y
python -m src.acquisition estimate --instruments audusd gc --frequency 1h --acknowledge-ibkr-entitlements
python -m src.acquisition estimate --all --acknowledge-ibkr-entitlements
python -m src.acquisition resume --report warehouse/reports/<run_id>.json
python -m src.acquisition status --report warehouse/reports/<run_id>.json
python -m src.acquisition rebuild-catalogue
```

Direct execution also works: `python src/acquisition/acquire.py ...`.
There is no separate plan command or plan artifact. A download writes a six-field summary; effective settings for resume live in
`warehouse/meta.sqlite`. `--all` and the default
selection mean the datasets configured in `configs/acquisition/hyp_1.toml`.
Aliases include `gc` for gold, `hg` for copper, `sp500` for ES and `dgs2` for US
2-year yields. Select daily/monthly iron ore or coal explicitly only when suitable;
daily iron ore and coal remain unresolved and block execution.

## Dates and frequency

Edit `start`, exclusive `end` and `frequency` in `configs/acquisition/hyp_1.toml`,
or pass `--start`, `--end`, `--frequency`. Defaults use hourly AUDUSD and futures.
`1m`, `1h`, `1d` map to IBKR `1 min`, `1 hour`, `1 day` and Databento
`ohlcv-1m`, `ohlcv-1h`, `ohlcv-1d`. FRED/RBA keep their native daily/monthly
frequencies. No resampling, filling, deduplication, roll adjustment or cleaning
is performed. Provider fields are retained in CSV, XLSX, DBN or IBKR JSON.
The IBKR JSON is native bar-field serialization, not a wire capture.

AUDUSD hourly/daily windows use calendar-month chunks (`chunk_months = 1`),
with partial first/last months when dates are not month boundaries. Minute-bar
requests use daily chunks. The current full window has 201 monthly requests.
IBKR fetches an additional provider duration day (`boundary_padding_days = 1`)
to cover UTC starts across its session boundaries. Native overlaps remain in raw
files, and manifests record request duration and in/out-of-window row counts.
New downloads use these settings; resuming an older report uses its frozen requests.

Live checks on 2026-10-07 returned 482 hourly bars inside January 2010 and 528
inside September 2026. Both final requests completed on the first attempt at
estimated USD 0 under existing entitlements, with no duplicate/out-of-order
timestamps. First January data was 2010-01-03 22:15 UTC; the September data covered
2026-09-01 00:00 UTC through 2026-09-30 23:00 UTC. This tests two windows, not every
month in the historical range. The SDK event loop is owned/drained per attempt to
close disconnected Windows sockets before retrying or exiting.

See the provider references for [IBKR historical bars](https://interactivebrokers.github.io/tws-api/historical_bars.html)
and [Databento OHLCV schemas](https://databento.com/docs/schemas-and-data-formats).

## Credentials and costs

Copy `.env.example` into the repository-root `.env` and fill your values.
The CLI loads supported settings before dispatch; existing process environment
values take precedence, blanks are skipped, and values are literal. Supported
settings are `DATABENTO_API_KEY`, `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID`. Direct
Python callers can invoke `load_env()`. FRED/RBA need no keys. IBKR uses an
already authenticated TWS/Gateway session, read-only; no account passwords or
order APIs are used. Verify your configured API port and entitlements.

Optional SDK dependencies are in `requirements-acquisition.txt`. Public
FRED/RBA and tests use only Python 3.11+ standard library. Installed provider
versions and code hashes are logged for each run. `.env` is ignored by Git.

All costs must be known before any observations are requested. Every execution
prints and saves its current estimate; unknown sources or entitlements block
the entire selection. Select unaffected instruments to run them separately.
The default estimated cost cap is USD 0. IBKR needs
`--acknowledge-ibkr-entitlements` on estimate/download/resume to confirm existing
access and no incremental charge. The tool never purchases subscriptions.

Databento quotes include bars and instrument definitions. For chargeable data,
run `estimate` separately with the same instruments/dates/frequency, review it,
then pass both `--approve-cost` and `--max-cost-usd <approved_amount>` to
`download`. Reviews expire after 24 hours. Quotes are refreshed immediately
before each Databento chunk, and spending gates run again. Actual billed charges
remain unknown until reconciled; an estimated cap is not a billing guarantee.

Each attempt reserves its quoted cost before the provider data request. Failed
or interrupted reservations remain counted on resume. Paid requests are not
automatically retried; a paid failure stops later requests. Public HTTP requests
use bounded transient retries (two retries, three attempts total). IBKR transport
errors/timeouts also allow at most two retries, with at least 10/20-second delays.
Provider rejections and empty IBKR data are not retried; an IBKR failure stops the
run before further chunks. Error guidance is included in the six-field note. Resume requires the explicit spending and
entitlement flags again when applicable.

## Artifacts and recovery

```text
warehouse/reports/<run_id>.json
warehouse/reports/<run_id>-estimate-<estimate_id>.json
warehouse/data/raw/<source>/<dataset>/<artifact_id>/<provider_file>
warehouse/data/raw/<source>/<dataset>/<artifact_id>/manifest.json
warehouse/meta.sqlite
```

Each report contains only `date_ran`, `amount_usd`, `start_time`, `finish_time`,
`status`, `note`. Times are UTC. Download amounts are reserved estimated USD;
cost-review amounts are quotes, or `null` when costs are unknown. Actual charges
require reconciliation. A running report has a blank finish time. Interrupted
historical runs keep a blank finish time if it cannot be established.

Reports and cost reviews share one folder. The run summary is updated atomically.
Internal resume settings, approved reviews and spending reservations live in
SQLite; no event-log file or verbose execution estimate is generated. Console
output shows the cost before downloading and the final summary. Raw manifests
retain provenance and source inspections.

Snapshot manifests record UTC timestamps, source, series, schema, granularity,
units, time semantics, requested/returned ranges, counts, missing-value
inspections, byte lengths, SHA-256 hashes, quotes and approvals. Methodology
references and document hashes point to `warehouse/docs/`. Acquisition time is
never substituted for an unknown historical publication time.

Request fingerprints identify exact equivalent requests; file checksums verify
bytes. Both are checked for reuse. There is no partial-overlap cache matching.
`--refresh` creates new snapshots; its resume reuses only that run's completed
artifacts. A failed companion download leaves already received native files
available for audit, but without a complete manifest they are not reused.
A crash after manifest installation can recover by scanning snapshots even if
catalogue registration or the completion event failed.

SQLite stores a rebuildable artifact index plus internal resume/cost state.
`rebuild-catalogue` preserves the latter; back up the database for recovery. A warehouse `.lock` prevents concurrent runs;
check for a running process before removing a stale lock. It is never deleted
automatically by another process. Temporary writes end in `.partial`. Generated
raw data/reports are ignored by Git and need separate backup. Clearing reports
does not remove raw snapshots. A fresh download can reuse them; use `--refresh`
to acquire another snapshot.

Legacy Research files remain untouched. `import-legacy --dataset <id> --files
<paths>` registers hashes and references, without copying files or inventing
original timestamps/costs. Legacy imports never satisfy new provider requests.

## Validation and limits

Run `python -m unittest discover -s tests -v` for offline invariants and CLI
selection, hourly settings, spending gates, report recovery and environment
loading. Public FRED/RBA have been smoke-tested. IBKR/Databento need live
verification with account access and the optional SDKs.

Acquisition success does not certify complete calendar coverage or historical
publication availability. The acquired RBA 2-year series has nonmissing values
only from 2013-09-02. Native FRED missing markers are retained. Source selection
for daily iron ore and coal remains open. Definitions and the methodology change
review are in [METHODOLOGY.md](METHODOLOGY.md), with references in
[methodology.json](methodology.json).
