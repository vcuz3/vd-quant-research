# Imported futures archives

Imported from `Research/futures/data` on 2026-10-07. All times below are UTC.

These are independent, byte-identical copies of previously acquired local files.
Each manifest records SHA-256 checksums, row counts, actual timestamp bounds,
native frequency, units, original paths and available vendor metadata.
The originals are untouched. Import cost: USD 0; historical provider charges are unknown.

As requested, `acquired_at_utc` and copied-file modification times equal the last
observed timestamp in each archive. `acquired_at_basis` marks this as a proxy;
`original_acquired_at_utc` remains unknown and `imported_at_utc` records the actual
local import time. These timestamps do not establish historical publication time.

| Instrument | Native bars | Rows | First observation (UTC) | Last observation / timestamp proxy (UTC) | Archive |
| --- | --- | ---: | --- | --- | --- |
| ES | 1m | 5,222,530 | 2011-08-01T00:00:00+00:00 | 2026-07-14T23:59:00+00:00 | [continuous](../data/raw/databento/es/LEGACY-20260714T235900-es-1m-e92d867b9248/manifest.json) |
| ES | 1s | 148,538,728 | 2010-06-07T00:00:01+00:00 | 2026-07-23T23:59:59+00:00 | [continuous](../data/raw/databento/es/LEGACY-20260723T235959-es-1s-84211ea2e77b/manifest.json) |
| GC | 1m | 5,186,870 | 2011-08-01T00:00:00+00:00 | 2026-07-16T23:59:00+00:00 | [continuous](../data/raw/databento/gold/LEGACY-20260716T235900-gc-1m-c49cbe190884/manifest.json) |
| GC | 1s | 103,937,333 | 2010-06-07T00:00:02+00:00 | 2026-07-17T20:59:59+00:00 | [continuous](../data/raw/databento/gold/LEGACY-20260717T205959-gc-1s-323a9be1be07/manifest.json) |
| HG | 1m | 5,224,488 | 2010-06-07T00:00:00+00:00 | 2026-07-27T23:52:00+00:00 | [continuous](../data/raw/databento/copper/LEGACY-20260727T235200-hg-1m-ce8dd5295f8b/manifest.json) |
| HG | 1m | 12,479,059 | 2011-07-29T00:00:00+00:00 | 2026-07-28T23:59:00+00:00 | [multiple contracts and spreads](../data/raw/databento/copper/LEGACY-20260728T235900-hg-1m-679ce2828e15/manifest.json) |

The `data/hg` archive contains outright contracts and spread symbols such as
`HGU6-HGZ6`. It is stored separately from the volume-ranked `HG.v.0` archive.
Its original request parameters and continuous roll rule are unknown.

ES and GC retain both their original 1-minute and 1-second archives; HG retains
its two original 1-minute archives. Back-adjusted and resampled derivatives,
smoke-test files and the older ES Excel series were excluded from this Databento
import. Files were neither merged nor converted to the pipeline default of 1 hour.

The Parquet row-group timestamp bounds were inspected for every archive.
Original metadata row counts and schema were checked where metadata is available.
This establishes stored coverage and copy integrity; missing bars, duplicates,
contract rolls and other research quality checks remain for the cleaning stage.

These manifests are indexed in `warehouse/meta.sqlite`. They have
`kind = "legacy_import"`, so normal download caching does not treat them as
verified new vendor requests or automatically resample them.

The six-field import report is [IMPORT-20261007T122534-89da7a33](../reports/IMPORT-20261007T122534-89da7a33.json).
