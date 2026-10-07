"""Raw-data acquisition: settings, sources, cost controls, reports and CLI."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import argparse
import csv
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import platform
import re
import sqlite3
import subprocess
import time
import tomllib
import uuid
import warnings
import xml.etree.ElementTree as ET
import zipfile


# ============================================================================
# Environment and shared records
# ============================================================================

ROOT = Path(__file__).resolve().parents[2]


PROVIDER_KEYS = frozenset({"DATABENTO_API_KEY", "IB_HOST", "IB_PORT", "IB_CLIENT_ID"})


def load_env(path=None, *, environ=None):
    """Load .env defaults and return loaded key names, never their values.

    Missing .env is allowed. Empty values are skipped so provider defaults work.
    Parse before mutating the environment to avoid partial loads on errors.
    """
    path = Path(path) if path is not None else ROOT / ".env"
    target = os.environ if environ is None else environ
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    parsed = {}
    for number, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if key not in PROVIDER_KEYS:
            continue
        if not separator:
            raise ValueError(
                f"Invalid provider setting in .env at line {number}: expected KEY=value"
            )
        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            closing = value.find(quote, 1)
            if closing < 0 or (
                value[closing + 1 :].strip()
                and (not value[closing + 1 :].lstrip().startswith("#"))
            ):
                raise ValueError(
                    f"Invalid quoted provider setting in .env at line {number}"
                )
            value = value[1:closing]
        else:
            value = (
                ""
                if value.startswith("#")
                else re.split("\\s+#", value, maxsplit=1)[0].rstrip()
            )
        if value:
            parsed[key] = value
    loaded = []
    for key, value in parsed.items():
        if key not in target:
            target[key] = value
            loaded.append(key)
    return loaded


# ============================================================================
# Payloads, timestamps and request identity
# ============================================================================


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class Payload:
    filename: str
    content: bytes
    inspection: dict = field(default_factory=dict)


@dataclass
class Estimate:
    amount_usd: str | None
    status: str
    note: str


class AcquisitionError(RuntimeError):
    pass


# ============================================================================
# Atomic storage and locking
# ============================================================================


def write_json(path, value, *, replace=False):
    write_bytes(
        path,
        (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode(),
        replace=replace,
    )


def write_bytes(path, content, *, replace=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_artifact(manifest, root):
    paths = manifest.get("files", [])
    return bool(paths) and all(
        (
            (root / item["path"]).is_file()
            and (root / item["path"]).stat().st_size == item["bytes"]
            and (sha256_file(root / item["path"]) == item["sha256"])
            for item in paths
        )
    )


@contextmanager
def run_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(
            f"Run is locked: {path}. Check for an active process before removing a stale lock."
        ) from exc
    try:
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


# ============================================================================
# Raw response inspections
# ============================================================================


def inspect_dates(rows, start, end, *, values=None):
    in_range = [d for d in rows if start <= d < end]
    warnings = []
    if not in_range:
        warnings.append("No dated observations inside the requested interval.")
    if in_range and in_range[0] > start:
        warnings.append(
            "First returned observation is later than requested start; inspect holidays and source coverage."
        )
    return {
        "row_count": len(rows),
        "returned_start": min(rows) if rows else None,
        "returned_end": max(rows) if rows else None,
        "rows_in_requested_interval": len(in_range),
        "duplicate_dates": len(rows) - len(set(rows)),
        "out_of_order_dates": sum((b < a for a, b in zip(rows, rows[1:]))),
        "missing_value_rows": sum((v in ("", ".", "NA", "NaN") for v in values))
        if values is not None
        else None,
        "warnings": warnings,
        "coverage_note": "Observation count is not proof of complete trading-day coverage. No calendar filling or repair performed.",
    }


def fred_csv(content, series, start, end):
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if not reader.fieldnames or reader.fieldnames != ["observation_date", series]:
        raise ValueError(
            "Unexpected FRED CSV header; response retained only on successful validation."
        )
    rows = list(reader)
    for row in rows:
        datetime.strptime(row["observation_date"], "%Y-%m-%d")
    return inspect_dates(
        [r["observation_date"] for r in rows],
        start,
        end,
        values=[r[series] for r in rows],
    )


def xlsx(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if archive.testzip() or "xl/workbook.xml" not in archive.namelist():
            raise ValueError("Invalid XLSX response")
    return {
        "format_verified": True,
        "note": "Original workbook retained including provider notes; no observations transformed.",
    }


def rba_xlsx(content, series, start, end):
    result = xlsx(content)
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = [
                "".join(item.itertext())
                for item in ET.fromstring(archive.read("xl/sharedStrings.xml"))
            ]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        props = workbook.find("s:workbookPr", ns)
        epoch = (
            datetime(1904, 1, 1)
            if props is not None and props.get("date1904") in ("1", "true")
            else datetime(1899, 12, 30)
        )
        for name in archive.namelist():
            if not name.startswith("xl/worksheets/sheet") or not name.endswith(".xml"):
                continue
            sheet = ET.fromstring(archive.read(name))
            rows = []
            for row in sheet.findall("s:sheetData/s:row", ns):
                cells = {}
                for cell in row.findall("s:c", ns):
                    col = "".join((c for c in cell.get("r", "") if c.isalpha()))
                    value = cell.find("s:v", ns)
                    text = (
                        value.text
                        if value is not None and value.text is not None
                        else ""
                    )
                    if cell.get("t") == "s":
                        text = shared[int(text)]
                    elif cell.get("t") == "inlineStr":
                        text = "".join(cell.find("s:is", ns).itertext())
                    cells[col] = text
                rows.append(cells)
            header_pos = next(
                (i for i, row in enumerate(rows) if series in row.values()), None
            )
            if header_pos is None:
                continue
            column = next((c for c, v in rows[header_pos].items() if v == series))
            dates, values = ([], [])
            for row in rows[header_pos + 1 :]:
                raw = row.get("A", "")
                try:
                    serial = float(raw)
                    if serial < 20000 or serial > 100000:
                        continue
                    day = (epoch + timedelta(days=serial)).date().isoformat()
                except ValueError:
                    continue
                dates.append(day)
                values.append(row.get(column, ""))
            populated = [
                d for d, v in zip(dates, values) if v not in ("", ".", "NA", "NaN")
            ]
            result.update(inspect_dates(dates, start, end, values=values))
            result.update(
                series_id=series,
                series_nonmissing_start=min(populated) if populated else None,
                series_nonmissing_end=max(populated) if populated else None,
                series_nonmissing_rows_in_interval=sum(
                    (start <= d < end for d in populated)
                ),
            )
            result["warnings"].append(
                "Full original workbook and notes retained; historical publication times are not reconstructed."
            )
            return result
    raise ValueError(f"Expected RBA series {series} not found in workbook")


# ============================================================================
# Catalogue and legacy file registration
# ============================================================================


def connect(root):
    path = root / "warehouse/meta.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS acquisition_artifacts (artifact_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL, acquired_at TEXT, manifest_path TEXT NOT NULL UNIQUE, manifest_json TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS acquisition_runs (run_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS acquisition_cost_reviews (review_id TEXT PRIMARY KEY, reviewed_at TEXT NOT NULL, request_set_fingerprint TEXT NOT NULL, all_costs_known INTEGER NOT NULL, total_estimated_usd TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS acquisition_reservations (artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL, estimated_usd TEXT NOT NULL, reserved_at TEXT NOT NULL)"
    )
    return connection


def register(root, path, manifest):
    with connect(root) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO acquisition_artifacts VALUES (?, ?, ?, ?, ?, ?)",
            (
                manifest["artifact_id"],
                manifest["dataset_id"],
                manifest["request_fingerprint"],
                manifest.get("acquired_at_utc"),
                path.relative_to(root).as_posix(),
                json.dumps(manifest),
            ),
        )
    connection.close()


def manifests(root):
    for path in sorted((root / "warehouse/data/raw").glob("*/*/*/manifest.json")):
        yield (path, json.loads(path.read_text(encoding="utf-8")))


def rebuild(root):
    with connect(root) as connection:
        connection.execute("DELETE FROM acquisition_artifacts")
    connection.close()
    count = 0
    for path, manifest in manifests(root):
        register(root, path, manifest)
        count += 1
    return count


# ============================================================================
# Legacy archives
# ============================================================================


def import_legacy(root, dataset_id, paths):
    registry = tomllib.loads(
        (root / "configs/acquisition/datasets.toml").read_text(encoding="utf-8")
    )["datasets"]
    if dataset_id not in registry:
        raise ValueError("Unknown dataset ID")
    files = []
    for value in paths:
        path = Path(value).resolve()
        if not path.is_file():
            raise ValueError(f"Not a readable file: {path}")
        files.append(
            {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "inspection": {
                    "role": "legacy external reference",
                    "coverage": "not independently inspected",
                },
            }
        )
    artifact_id = uuid.uuid4().hex
    manifest = {
        "version": 1,
        "artifact_id": artifact_id,
        "kind": "legacy_import",
        "status": "registered",
        "run_id": "legacy",
        "dataset_id": dataset_id,
        "request_fingerprint": "legacy-" + fingerprint(files),
        "acquired_at_utc": None,
        "imported_at_utc": utc_now(),
        "original_source_claim": registry[dataset_id]["source"],
        "source_claim_verified": False,
        "original_cost_usd": None,
        "files": files,
        "note": "External reference only; does not prove original vendor response, request parameters, or acquisition time. External changes invalidate hashes.",
    }
    path = (
        root
        / "warehouse/data/raw"
        / registry[dataset_id]["source"]
        / dataset_id
        / artifact_id
        / "manifest.json"
    )
    write_json(path, manifest)
    register(root, path, manifest)
    return path


# ============================================================================
# Public HTTP access
# ============================================================================


def public_get(url, attempts=3):
    for attempt in range(attempts):
        started = utc_now()
        try:
            with urlopen(
                Request(
                    url,
                    headers={
                        "User-Agent": "quant-research-acquisition/0.1 (personal research)"
                    },
                ),
                timeout=45,
            ) as response:
                content = response.read()
                metadata = {
                    "endpoint": url,
                    "http_status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "etag": response.headers.get("ETag"),
                    "http_attempts": attempt + 1,
                }
                metadata.update(
                    download_started_at_utc=started, download_completed_at_utc=utc_now()
                )
            if not content:
                raise ValueError("Empty HTTP response")
            return (content, metadata)
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise AcquisitionError(
                    f"Public HTTP request failed with status {exc.code}"
                ) from None
        except (URLError, TimeoutError):
            if attempt == attempts - 1:
                raise AcquisitionError(
                    "Public HTTP request failed after bounded retries (network/TLS/timeout)"
                ) from None
        time.sleep(2**attempt)


# ============================================================================
# FRED
# ============================================================================


def fred_estimate(request):
    return Estimate(
        "0",
        "known",
        "Public FRED CSV; no download charge. Latest snapshot, not reconstructed publication vintages.",
    )


def fred_download(request):
    end_inclusive = (date.fromisoformat(request["end"]) - timedelta(days=1)).isoformat()
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urlencode(
        {"id": request["symbol"], "cosd": request["start"], "coed": end_inclusive}
    )
    content, metadata = public_get(url)
    inspection = fred_csv(content, request["symbol"], request["start"], request["end"])
    inspection.update(metadata)
    return [Payload("observations.csv", content, inspection)]


# ============================================================================
# RBA
# ============================================================================


def rba_estimate(request):
    return Estimate(
        "0",
        "known",
        "Public RBA files; no download charge. Available history and publication latency differ from requested observation dates.",
    )


def rba_download(request):
    url = "https://www.rba.gov.au/statistics/tables/xls/f02d.xlsx"
    content, metadata = public_get(url)
    inspection = rba_xlsx(content, request["symbol"], request["start"], request["end"])
    inspection.update(metadata)
    return [Payload("f02d.xlsx", content, inspection)]


# ============================================================================
# IBKR
# ============================================================================


IBKR_MAX_RETRIES = 2  # Initial attempt plus two retries: at most three attempts.


def ibkr_connection_settings():
    host = os.getenv("IB_HOST", "127.0.0.1").strip()
    try:
        port = int(os.getenv("IB_PORT", "4001"))
        client_id = int(os.getenv("IB_CLIENT_ID", "71"))
    except ValueError:
        raise AcquisitionError(
            "IB_PORT and IB_CLIENT_ID must be integers; check .env."
        ) from None
    if not host or not 1 <= port <= 65535 or client_id < 0:
        raise AcquisitionError(
            "Check IB_HOST, IB_PORT (1–65535) and IB_CLIENT_ID (nonnegative) in .env."
        )
    return host, port, client_id


def check_ibkr_port():
    """One local TCP check; no market data, authentication or subscription changes."""
    import socket

    host, port, _ = ibkr_connection_settings()
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError:
        raise AcquisitionError(
            f"IBKR port {host}:{port} is not reachable. Log into TWS/Gateway, enable "
            "ActiveX and Socket Clients, and Apply/OK the matching Socket Port setting."
        ) from None
    print(
        f"TCP port {host}:{port} is reachable. This checks the listener only; no market data requested."
    )


def ibkr_retry(action, *, host, port, pacing_seconds=10):
    """Retry only transport/timeouts; provider rejections and empty data fail once."""
    delay = max(10, float(pacing_seconds))
    for attempt in range(IBKR_MAX_RETRIES + 1):
        try:
            result = action()
            return result, attempt + 1
        except (OSError, TimeoutError):
            if attempt == IBKR_MAX_RETRIES:
                raise AcquisitionError(
                    f"IBKR connection/request failed at {host}:{port} after {attempt + 1} attempts "
                    f"({IBKR_MAX_RETRIES} retries). Check TWS/Gateway login, Enable ActiveX and Socket Clients, "
                    "Socket Port, and an unused IB_CLIENT_ID. Run stopped; fix settings before resuming."
                ) from None
            time.sleep(delay * 2**attempt)


def ibkr_estimate(request):
    return Estimate(
        None,
        "entitlement_required",
        "IBKR has no request-specific quote here. Confirm existing account/data entitlements; this tool never purchases subscriptions.",
    )


def ibkr_download(request):
    host, port, _ = ibkr_connection_settings()
    payloads, attempts = ibkr_retry(
        lambda: ibkr_download_once(request),
        host=host,
        port=port,
        pacing_seconds=request.get("pacing_seconds", 10),
    )
    for payload in payloads:
        payload.inspection["attempts"] = attempts
    time.sleep(max(10, float(request.get("pacing_seconds", 10))))
    return payloads


def ibkr_download_once(request):
    import asyncio

    # Own and drain each attempt's event loop so disconnected sockets are closed
    # before another attempt starts or the CLI exits (notably on Windows).
    with asyncio.Runner() as runner:
        runner.get_loop()
        try:
            return ibkr_fetch(request)
        finally:
            runner.run(asyncio.sleep(0.1))


def ibkr_fetch(request):
    try:
        from ib_async import IB, Forex
    except ImportError:
        raise RuntimeError(
            "Install acquisition provider dependencies: python -m pip install -r requirements-acquisition.txt"
        ) from None
    client = IB()
    client.RaiseRequestErrors = True
    host, port, client_id = ibkr_connection_settings()
    try:
        client.connect(
            host,
            port,
            clientId=client_id,
            readonly=True,
            timeout=30,
        )
        contract = Forex(request["symbol"])
        client.qualifyContracts(contract)
        start = datetime.fromisoformat(request["start"]).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(request["end"]).replace(tzinfo=timezone.utc)
        padding = int(request.get("boundary_padding_days", 0))
        duration = ibkr_duration(start.date(), end.date(), padding_days=padding)
        bars = client.reqHistoricalData(
            contract,
            endDateTime=end,
            durationStr=duration,
            barSizeSetting=request["bar_size"],
            whatToShow=request["price_type"],
            useRTH=False,
            formatDate=2,
            keepUpToDate=False,
            timeout=90,
        )
        if not bars:
            raise AcquisitionError(
                "IBKR returned no bars: check timeout, entitlement, history coverage or a closed-market interval; empty chunks are not cached as complete."
            )
        rows = [asdict(bar) for bar in bars]

        def native(value):
            if hasattr(value, "isoformat"):
                return value.isoformat()
            raise TypeError(type(value).__name__)

        content = (json.dumps(rows, default=native, ensure_ascii=False) + "\n").encode()
        dates = [bar.date.astimezone(timezone.utc).isoformat() for bar in bars]
        inspection = inspect_dates(
            [v[:10] for v in dates], request["start"], request["end"]
        )
        inspection["returned_start"] = min(dates) if dates else None
        inspection["returned_end"] = max(dates) if dates else None
        inspection["duplicate_dates"] = None
        inspection["duplicate_timestamps"] = len(dates) - len(set(dates))
        inspection["out_of_order_timestamps"] = sum(
            (b < a for a, b in zip(dates, dates[1:]))
        )
        inspection["request_duration"] = duration
        inspection["boundary_padding_days"] = padding
        inspection["rows_in_requested_interval"] = sum(
            start <= bar.date.astimezone(timezone.utc) < end for bar in bars
        )
        inspection["rows_before_requested_start"] = sum(
            bar.date.astimezone(timezone.utc) < start for bar in bars
        )
        inspection["rows_at_or_after_requested_end"] = sum(
            bar.date.astimezone(timezone.utc) >= end for bar in bars
        )
        inspection["contract"] = {
            k: getattr(contract, k)
            for k in ("conId", "symbol", "currency", "secType", "exchange")
        }
        inspection["warnings"].append(
            "MIDPOINT quote bars are not execution prices; provider FX volume fields are not consolidated traded volume. Native overlaps are preserved."
        )
        return [Payload("bars.json", content, inspection)]
    finally:
        client.disconnect()


def ibkr_duration(start, end, *, padding_days=0):
    """Pad UTC windows to account for IBKR's trading-session duration boundaries."""
    if padding_days < 0:
        raise ValueError("boundary_padding_days must be nonnegative")
    if padding_days:
        return f"{(end - start).days + padding_days} D"
    following_month = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    if start.day == 1 and end == following_month:
        return "1 M"
    return f"{(end - start).days} D"


# ============================================================================
# Databento
# ============================================================================


def databento_client():
    key = os.getenv("DATABENTO_API_KEY")
    if not key:
        raise RuntimeError("DATABENTO_API_KEY is not set")
    try:
        import databento as db
    except ImportError:
        raise RuntimeError(
            "Install acquisition provider dependencies: python -m pip install -r requirements-acquisition.txt"
        ) from None
    return db.Historical(key)


def databento_parameters(request, schema):
    return {
        "dataset": request["dataset"],
        "symbols": request["symbol"],
        "schema": schema,
        "stype_in": "continuous",
        "start": request["start"],
        "end": request["end"],
    }


def databento_estimate(request):
    api = databento_client()
    total = sum(
        (
            Decimal(str(api.metadata.get_cost(**databento_parameters(request, schema))))
            for schema in (request["schema"], "definition")
        ),
        Decimal("0"),
    )
    return Estimate(
        str(total),
        "known",
        "USD estimate includes native bars and instrument definitions; billed charges may differ. Symbology metadata retained separately.",
    )


def databento_download(request):
    api = databento_client()
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="quant-acq-") as temporary:
        for schema in (request["schema"], "definition"):
            path = Path(temporary) / f"{schema}.dbn.zst"
            started = utc_now()
            store = api.timeseries.get_range(
                **databento_parameters(request, schema), path=path
            )
            frame = store.to_df(map_symbols=False, pretty_ts=True, tz="UTC")
            timestamps = frame.index
            inspection = {
                "row_count": len(frame),
                "returned_start": str(timestamps.min()) if len(frame) else None,
                "returned_end": str(timestamps.max()) if len(frame) else None,
                "schema": schema,
                "out_of_order_timestamps": int(
                    (timestamps[1:] < timestamps[:-1]).sum()
                ),
                "warnings": []
                if len(frame)
                else ["No records returned inside requested interval."],
                "coverage_note": "No-trade intervals produce no OHLCV bar; no synthetic bars added.",
            }
            inspection.update(
                download_started_at_utc=started, download_completed_at_utc=utc_now()
            )
            yield Payload(path.name, path.read_bytes(), inspection)
        mapping = api.symbology.resolve(
            dataset=request["dataset"],
            symbols=request["symbol"],
            stype_in="continuous",
            stype_out="raw_symbol",
            start_date=request["start"],
            end_date=request["end"],
        )
        yield Payload(
            "symbology.json",
            (json.dumps(mapping, default=str, indent=2) + "\n").encode(),
            {
                "note": "Dated mapping intervals preserved; not collapsed to one contract name."
            },
        )


# ============================================================================
# Cost estimates
# ============================================================================


def amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Cost must be a finite, nonnegative decimal") from None
    if not result.is_finite() or result < 0:
        raise ValueError("Cost must be a finite, nonnegative decimal")
    return result


def estimate_requests(run, requests=None, *, acknowledge_ibkr=False):
    output, total = ([], Decimal("0"))
    for request in requests if requests is not None else run["requests"]:
        if request.get("cached_manifest"):
            estimate = Estimate(
                "0", "cached", "Verified existing artifact; no download planned."
            )
        elif request["source"] == "ibkr" and acknowledge_ibkr:
            estimate = Estimate(
                "0",
                "acknowledged_entitlement",
                "User confirms existing IBKR entitlements and no incremental download charge. Subscription fees are outside this estimate.",
            )
        elif request["source"] not in ADAPTERS:
            estimate = Estimate(
                None, "unresolved", request.get("note", "Source not selected")
            )
        else:
            try:
                estimate = ADAPTERS[request["source"]].estimate(request)
                if estimate.amount_usd is not None:
                    amount(estimate.amount_usd)
            except Exception as exc:
                estimate = Estimate(
                    None,
                    "blocked",
                    f"Estimate unavailable ({type(exc).__name__}); check credentials, dependencies, entitlements and availability.",
                )
        if estimate.amount_usd is not None:
            total += amount(estimate.amount_usd)
        output.append(
            {
                "request_fingerprint": request["request_fingerprint"],
                "dataset_id": request["dataset_id"],
                **asdict(estimate),
            }
        )
    return {
        "created_at_utc": utc_now(),
        "request_set_fingerprint": run["request_set_fingerprint"],
        "total_estimated_usd": str(total),
        "all_costs_known": all((r["amount_usd"] is not None for r in output)),
        "requests": output,
    }


ADAPTERS = {
    "fred": SimpleNamespace(estimate=fred_estimate, download=fred_download),
    "rba": SimpleNamespace(estimate=rba_estimate, download=rba_download),
    "ibkr": SimpleNamespace(estimate=ibkr_estimate, download=ibkr_download),
    "databento": SimpleNamespace(
        estimate=databento_estimate, download=databento_download
    ),
}


# ============================================================================
# Instrument selection, frequency and request construction (no plan files)
# ============================================================================

FREQUENCIES = {
    "1m": {"bar_size": "1 min", "schema": "ohlcv-1m"},
    "1h": {"bar_size": "1 hour", "schema": "ohlcv-1h"},
    "1d": {"bar_size": "1 day", "schema": "ohlcv-1d"},
}
ALIASES = {"gc": "gold", "hg": "copper", "sp500": "es", "dgs2": "us_2y"}


def provenance(root):
    def git(*args):
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root), *args],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    packages = {
        name: importlib.metadata.version(name)
        for name in ("databento", "ib_async", "pandas")
        if importlib.util.find_spec(name)
    }
    return {
        "python": platform.python_version(),
        "packages": packages,
        "git_revision": git("rev-parse", "HEAD"),
        "git_status": git("status", "--porcelain"),
        "source_sha256": {
            path.relative_to(root).as_posix(): sha256_file(path)
            for path in (root / "src").rglob("*.py")
        },
    }


def requests_for(dataset, start, end):
    left, stop = date.fromisoformat(start), date.fromisoformat(end)
    if left >= stop:
        raise ValueError("start must precede exclusive end")
    step = int(dataset.get("chunk_days", 30))
    months = int(dataset.get("chunk_months", 0))
    if step <= 0:
        raise ValueError("chunk_days must be positive")
    if "chunk_months" in dataset and months <= 0:
        raise ValueError("chunk_months must be positive")
    if dataset["source"] in ("fred", "rba", "unresolved"):
        step = (stop - left).days
        months = 0
    while left < stop:
        if months:
            month_index = left.year * 12 + left.month - 1 + months
            boundary = date(month_index // 12, month_index % 12 + 1, 1)
            right = min(stop, boundary)
        else:
            right = min(stop, left + timedelta(days=step))
        request = dict(
            dataset,
            start=left.isoformat(),
            end=right.isoformat(),
            boundary="[start,end)",
        )
        request["request_fingerprint"] = fingerprint(request)
        yield request
        left = right


def selected_instruments(names, registry):
    result = []
    for value in names:
        for name in value.split(","):
            name = name.strip().lower()
            name = ALIASES.get(name, name)
            if not re.fullmatch(r"[a-z0-9_]+", name) or name not in registry:
                raise ValueError(f"Unknown instrument: {name}")
            if name in result:
                raise ValueError(f"Duplicate instrument: {name}")
            result.append(name)
    if not result:
        raise ValueError("Choose at least one instrument")
    return result


def create_run(
    config_path,
    *,
    root=ROOT,
    instruments=None,
    all_instruments=False,
    start=None,
    end=None,
    frequency=None,
    refresh=False,
):
    """Build requests in memory; reports are written by estimate/download only."""
    config = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    settings = dict(config["run"])
    settings.update(
        start=start or settings["start"],
        end=end or settings["end"],
        frequency=frequency or settings.get("frequency", "1h"),
    )
    if settings["frequency"] not in FREQUENCIES:
        raise ValueError("Frequency must be 1m, 1h or 1d")
    if date.fromisoformat(settings["start"]) >= date.fromisoformat(settings["end"]):
        raise ValueError("start must precede exclusive end")
    registry = tomllib.loads(
        (root / "configs/acquisition/datasets.toml").read_text(encoding="utf-8")
    )["datasets"]
    if instruments is not None and all_instruments:
        raise ValueError("Choose --instruments or --all")
    names = settings["datasets"] if instruments is None else instruments
    ids = selected_instruments(names, registry)
    settings["datasets"] = ids
    entries = []
    for dataset_id in ids:
        dataset = dict(registry[dataset_id], dataset_id=dataset_id)
        if dataset["source"] in ("ibkr", "databento"):
            dataset["granularity"] = settings["frequency"]
            field = "bar_size" if dataset["source"] == "ibkr" else "schema"
            dataset[field] = FREQUENCIES[settings["frequency"]][field]
            if (
                dataset["source"] == "ibkr"
                and settings["frequency"] == "1m"
                and "chunk_months" in dataset
            ):
                # Keep minute-bar requests modest rather than fetching an entire month.
                dataset.pop("chunk_months")
                dataset["chunk_days"] = 1
        entries.extend(requests_for(dataset, settings["start"], settings["end"]))
    existing = {}
    if not refresh:
        for path, manifest in manifests(root):
            if manifest.get("kind") != "legacy_import" and verified_artifact(
                manifest, root
            ):
                existing[manifest["request_fingerprint"]] = path.relative_to(
                    root
                ).as_posix()
    for entry in entries:
        entry["cached_manifest"] = existing.get(entry["request_fingerprint"])
    run_id = (
        "ACQ-"
        + utc_now().replace(":", "").replace("-", "")[:15]
        + "-"
        + uuid.uuid4().hex[:8]
    )
    docs = root / "warehouse/docs"
    run = {
        "version": 2,
        "run_id": run_id,
        "created_at_utc": utc_now(),
        "settings": settings,
        "refresh": refresh,
        "requests": entries,
        "request_set_fingerprint": fingerprint(
            sorted(r["request_fingerprint"] for r in entries)
        ),
        "provenance": provenance(root),
        "methodology_documents": {
            path.relative_to(root).as_posix(): sha256_file(path)
            for path in (docs / "methodology.json", docs / "METHODOLOGY.md")
            if path.exists()
        },
        "status": "prepared",
        "records": [],
        "failures": [],
    }
    return root / "warehouse/reports" / (run_id + ".json"), run


def load_run(path, root=ROOT):
    path = Path(path).resolve()
    if path.parent != (root / "warehouse/reports").resolve():
        raise ValueError("Run report must live in this repository's warehouse/reports")
    report = json.loads(path.read_text(encoding="utf-8"))
    # Accept historical detailed reports; new reports have only the six summary fields.
    if "requests" in report:
        run = report
    else:
        with connect(root) as connection:
            row = connection.execute(
                "SELECT state_json FROM acquisition_runs WHERE run_id = ?", (path.stem,)
            ).fetchone()
        connection.close()
        if row is None:
            raise ValueError("Resume state is missing from warehouse/meta.sqlite")
        run = json.loads(row[0])
    if path.name != run.get("run_id", "") + ".json" or not re.fullmatch(
        r"ACQ-[A-Za-z0-9T+]+-[a-f0-9]{8}", run["run_id"]
    ):
        raise ValueError("Invalid run report identifier")
    for request in run["requests"]:
        unsigned = {
            k: v
            for k, v in request.items()
            if k not in ("request_fingerprint", "cached_manifest")
        }
        if fingerprint(unsigned) != request["request_fingerprint"]:
            raise ValueError(
                "Request checksum mismatch; use a new download command to change settings"
            )
    if (
        fingerprint(sorted(r["request_fingerprint"] for r in run["requests"]))
        != run["request_set_fingerprint"]
    ):
        raise ValueError("Run request checksum mismatch")
    return run


# ============================================================================
# Six-field summaries, internal cost records and resumable execution
# ============================================================================


REPORT_FIELDS = (
    "date_ran",
    "amount_usd",
    "start_time",
    "finish_time",
    "status",
    "note",
)


def run_summary(run):
    start = run.get("started_at_utc", run["created_at_utc"])
    ids = run.get("settings", {}).get("datasets", [])
    if not ids:
        ids = list(dict.fromkeys(r["dataset_id"] for r in run["requests"]))
    complete = len(run.get("records", []))
    failures = len(run.get("failures", []))
    note = f"{', '.join(ids)}: {complete}/{len(run['requests'])} chunks complete or cached; {failures} failed."
    if run.get("blocked_reason"):
        note += " " + run["blocked_reason"].replace("_", " ") + "."
    if run.get("status") == "interrupted":
        note += " Interrupted; resume can retry unfinished chunks."
    if run.get("failures"):
        first = run["failures"][0]
        note += " " + sanitized_warning(
            first.get("error", first.get("error_type", "Request failed"))
        )
    return dict(
        zip(
            REPORT_FIELDS,
            (
                start[:10],
                run.get("reserved_estimated_usd", "0"),
                start,
                run.get("finished_at_utc"),
                run["status"],
                note,
            ),
        )
    )


def store_run(root, run):
    # Internal checkpoint data is not duplicated in the human-readable log.
    with connect(root) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO acquisition_runs VALUES (?, ?)",
            (run["run_id"], json.dumps(run)),
        )
    connection.close()
    path = root / "warehouse/reports" / (run["run_id"] + ".json")
    write_json(path, run_summary(run), replace=path.exists())
    return path


def save_estimate(directory, estimate, *, purpose="review", run_id=None):
    root = directory.parent.parent
    if purpose != "review":
        return root / "warehouse/meta.sqlite"
    review_id = uuid.uuid4().hex
    with connect(root) as connection:
        connection.execute(
            "INSERT INTO acquisition_cost_reviews VALUES (?, ?, ?, ?, ?)",
            (
                review_id,
                estimate["created_at_utc"],
                estimate["request_set_fingerprint"],
                int(estimate["all_costs_known"]),
                estimate["total_estimated_usd"],
            ),
        )
    connection.close()
    start = estimate["created_at_utc"]
    ids = list(dict.fromkeys(r["dataset_id"] for r in estimate["requests"]))
    summary = dict(
        zip(
            REPORT_FIELDS,
            (
                start[:10],
                estimate["total_estimated_usd"]
                if estimate["all_costs_known"]
                else None,
                start,
                utc_now(),
                "estimated" if estimate["all_costs_known"] else "blocked",
                f"{', '.join(ids)}: cost estimate only; no observation data requested."
                if estimate["all_costs_known"]
                else f"{', '.join(ids)}: some costs or entitlements are unknown.",
            ),
        )
    )
    path = directory / ((run_id or "QUOTE") + "-estimate-" + review_id[:8] + ".json")
    write_json(path, summary)
    return path


def print_estimate(value):
    cost = value["total_estimated_usd"] if value["all_costs_known"] else "unknown"
    print(f"Estimated download cost: USD {cost}", flush=True)


def reserve_cost(root, run_id, **fields):
    """Reserve the quote durably before the provider can incur a charge."""
    with connect(root) as connection:
        connection.execute(
            "INSERT INTO acquisition_reservations VALUES (?, ?, ?, ?, ?)",
            (
                fields["artifact_id"],
                run_id,
                fields["request_fingerprint"],
                fields["estimated_usd"],
                utc_now(),
            ),
        )
    connection.close()


def sanitized_warning(message):
    text = str(message)
    for key in ("DATABENTO_API_KEY", "FRED_API_KEY"):
        value = os.getenv(key)
        if value:
            text = text.replace(value, "[REDACTED]")
    return re.sub(
        r"(?i)(api_key|token|authorization)([=:]\s*)[^\s&]+", r"\1\2[REDACTED]", text
    )


def completed(root, run):
    output = {}
    for path, manifest in manifests(root):
        if (
            manifest.get("kind") == "legacy_import"
            or manifest.get("status") == "invalid"
        ):
            continue
        if run["refresh"] and manifest["run_id"] != run["run_id"]:
            continue
        if verified_artifact(manifest, root):
            register(root, path, manifest)
            output[manifest["request_fingerprint"]] = path.relative_to(root).as_posix()
    return output


def spent_reservations(root, run_id):
    with connect(root) as connection:
        rows = connection.execute(
            "SELECT estimated_usd FROM acquisition_reservations WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    connection.close()
    return sum((amount(row[0]) for row in rows), amount("0"))


def has_review(directory, run):
    now = datetime.now(timezone.utc)
    with connect(directory.parent.parent) as connection:
        rows = connection.execute(
            "SELECT reviewed_at FROM acquisition_cost_reviews WHERE request_set_fingerprint = ? AND all_costs_known = 1",
            (run["request_set_fingerprint"],),
        ).fetchall()
    connection.close()
    return any(
        timedelta(0) <= now - datetime.fromisoformat(row[0]) < timedelta(hours=24)
        for row in rows
    )


def execute(root, run, *, max_cost="0", approve_cost=False, acknowledge_ibkr=False):
    run.setdefault("started_at_utc", utc_now())
    run["finished_at_utc"] = None
    try:
        return execute_requests(
            root,
            run,
            max_cost=max_cost,
            approve_cost=approve_cost,
            acknowledge_ibkr=acknowledge_ibkr,
        )
    except KeyboardInterrupt:
        run.update(status="interrupted", finished_at_utc=utc_now())
        store_run(root, run)
        raise


def execute_requests(
    root, run, *, max_cost="0", approve_cost=False, acknowledge_ibkr=False
):
    cap = amount(max_cost)
    directory = root / "warehouse/reports"

    def record(status, **values):
        run.update(at_utc=utc_now(), status=status, **values)
        if status in ("complete", "failed", "blocked"):
            run["finished_at_utc"] = utc_now()
        store_run(root, run)

    def block(message, reason):
        record("blocked", blocked_reason=reason)
        raise RuntimeError(message)

    with run_lock(root / "warehouse"):
        existing = completed(root, run)
        pending = [
            dict(r, cached_manifest=None)
            for r in run["requests"]
            if r["request_fingerprint"] not in existing
        ]
        estimate = estimate_requests(run, pending, acknowledge_ibkr=acknowledge_ibkr)
        estimate_path = save_estimate(
            directory, estimate, purpose="execution", run_id=run["run_id"]
        )
        reserved = spent_reservations(root, run["run_id"])
        print_estimate(estimate)
        record(
            "reviewing_cost",
            estimate=estimate_path.relative_to(root).as_posix(),
            reserved_estimated_usd=str(reserved),
        )
        if not estimate["all_costs_known"]:
            block(
                "Costs/entitlements are unknown. Inspect the estimate or explicitly acknowledge existing IBKR entitlements.",
                "unknown_cost_or_entitlement",
            )
        total = amount(estimate["total_estimated_usd"])
        if total + reserved > cap or (total > 0 and not approve_cost):
            block(
                "Download stopped before data requests. Review estimate, then pass --approve-cost and an adequate --max-cost-usd for paid data.",
                "spending_not_approved",
            )
        if total > 0:
            if not has_review(directory, run):
                block(
                    "Run estimate with the same instruments, dates and frequency and review its output before approving paid data.",
                    "separate_review_required",
                )
        approval = {
            "max_cost_usd": str(cap),
            "approve_cost": approve_cost,
            "acknowledge_ibkr_entitlements": acknowledge_ibkr,
        }
        record("running", approval=approval, execution_provenance=provenance(root))
        records, failures = [], []
        estimates = {e["request_fingerprint"]: e for e in estimate["requests"]}
        remaining_total = total
        for request in run["requests"]:
            key = request["request_fingerprint"]
            if key in existing:
                records.append(
                    {
                        "dataset_id": request["dataset_id"],
                        "status": "cached",
                        "manifest": existing[key],
                    }
                )
                continue
            artifact_id = uuid.uuid4().hex
            started = utc_now()
            quote = estimates[key]
            if request["source"] == "databento":
                updated = estimate_requests(run, [dict(request, cached_manifest=None)])[
                    "requests"
                ][0]
                if updated["amount_usd"] is None:
                    record("running", records=records, failures=failures)
                    block(
                        "Could not refresh provider quote before the next data request.",
                        "quote_refresh_failed",
                    )
                remaining_total += amount(updated["amount_usd"]) - amount(
                    quote["amount_usd"]
                )
                quote = updated
                if remaining_total + spent_reservations(root, run["run_id"]) > cap or (
                    amount(quote["amount_usd"]) > 0 and not approve_cost
                ):
                    record("running", records=records, failures=failures)
                    block(
                        "Refreshed costs exceed approval; review a new estimate.",
                        "quote_changed_exceeds_approval",
                    )
                if amount(quote["amount_usd"]) > 0 and not has_review(directory, run):
                    record("running", records=records, failures=failures)
                    block(
                        "The quote became chargeable; run a separate estimate before approving paid data.",
                        "separate_review_required",
                    )
            remaining_total -= amount(quote["amount_usd"])
            reserve_cost(
                root,
                run["run_id"],
                request_fingerprint=key,
                artifact_id=artifact_id,
                estimated_usd=quote["amount_usd"],
            )
            try:
                snapshot = (
                    root
                    / "warehouse/data/raw"
                    / request["source"]
                    / request["dataset_id"]
                    / artifact_id
                )
                files = []
                with warnings.catch_warnings(record=True) as captured:
                    warnings.simplefilter("always")
                    for payload in ADAPTERS[request["source"]].download(request):
                        path = snapshot / payload.filename
                        if path.name != payload.filename or payload.filename in (
                            ".",
                            "..",
                        ):
                            raise ValueError("Adapter returned an unsafe filename")
                        write_bytes(path, payload.content)
                        # Provider headers stay in raw responses, not inspection reports.
                        inspection = {
                            k: v
                            for k, v in payload.inspection.items()
                            if k != "provider_header_rows"
                        }
                        file = {
                            "path": path.relative_to(root).as_posix(),
                            "bytes": len(payload.content),
                            "sha256": sha256_file(path),
                            "inspection": inspection,
                        }
                        files.append(file)
                if not files:
                    raise ValueError("Adapter did not return any artifacts")
                manifest = {
                    "version": 2,
                    "kind": "provider_snapshot",
                    "status": "complete",
                    "artifact_id": artifact_id,
                    "run_id": run["run_id"],
                    "dataset_id": request["dataset_id"],
                    "request_fingerprint": key,
                    "request_started_at_utc": started,
                    "acquired_at_utc": utc_now(),
                    "request": request,
                    "request_set_fingerprint": run["request_set_fingerprint"],
                    "methodology_ref": request.get("methodology_ref"),
                    "methodology_documents": run["methodology_documents"],
                    "cost": {**quote, "actual_charged_usd": None},
                    "approval": approval,
                    "publication_time": None,
                    "publication_time_note": "Download time is not historical publication time.",
                    "provider_warnings": [
                        {
                            "category": w.category.__name__,
                            "message": sanitized_warning(w.message),
                        }
                        for w in captured
                    ],
                    "files": files,
                }
                path = snapshot / "manifest.json"
                write_json(path, manifest)
                register(root, path, manifest)
                records.append(
                    {
                        "dataset_id": request["dataset_id"],
                        "status": "complete",
                        "manifest": path.relative_to(root).as_posix(),
                        "inspections": [file["inspection"] for file in files],
                    }
                )
            except Exception as exc:
                failure = {
                    "dataset_id": request["dataset_id"],
                    "request_fingerprint": key,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
                if isinstance(exc, AcquisitionError):
                    failure["error"] = str(exc)
                failures.append(failure)
                if request["source"] == "ibkr" or amount(quote["amount_usd"]) > 0:
                    break
            finally:
                record(
                    "running",
                    records=records,
                    failures=failures,
                    reserved_estimated_usd=str(spent_reservations(root, run["run_id"])),
                )
        run.pop("blocked_reason", None)
        record(
            "failed" if failures else "complete",
            requested_chunks=len(run["requests"]),
            completed_or_cached_chunks=len(records),
            records=records,
            failures=failures,
            reserved_estimated_usd=str(spent_reservations(root, run["run_id"])),
            note="Acquisition completion does not certify calendar coverage or point-in-time availability.",
        )
        print(json.dumps(run_summary(run), indent=2), flush=True)
        return dict(run)


# ============================================================================
# Command-line interface
# ============================================================================


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("estimate", "download"):
        command = commands.add_parser(name)
        command.add_argument(
            "--config", type=Path, default=ROOT / "configs/acquisition/hyp_1.toml"
        )
        selection = command.add_mutually_exclusive_group()
        selection.add_argument(
            "--instruments",
            nargs="+",
            help="IDs or aliases: audusd, gc/gold, hg/copper, es, au_2y, us_2y, vix",
        )
        selection.add_argument(
            "--all",
            action="store_true",
            dest="all_instruments",
            help="All datasets listed in the run config (also the default)",
        )
        command.add_argument(
            "--start", help="Inclusive YYYY-MM-DD; overrides the run config"
        )
        command.add_argument(
            "--end", help="Exclusive YYYY-MM-DD; overrides the run config"
        )
        command.add_argument(
            "--frequency",
            choices=FREQUENCIES,
            help="AUDUSD/futures bars; default from config (1h). Public daily/monthly series keep native frequency",
        )
        command.add_argument(
            "--refresh",
            action="store_true",
            help="Acquire a new snapshot even when matching data is cached",
        )
        command.add_argument("--acknowledge-ibkr-entitlements", action="store_true")
        if name == "download":
            command.add_argument("--max-cost-usd", default="0")
            command.add_argument("--approve-cost", action="store_true")
    resume = commands.add_parser(
        "resume", help="Continue the same run using its report"
    )
    resume.add_argument("--report", type=Path, required=True)
    resume.add_argument("--max-cost-usd", default="0")
    resume.add_argument("--approve-cost", action="store_true")
    resume.add_argument("--acknowledge-ibkr-entitlements", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("--report", type=Path, required=True)
    commands.add_parser("rebuild-catalogue")
    commands.add_parser(
        "check-ibkr",
        help="Check the configured local API port once; no market-data requests",
    )
    legacy = commands.add_parser("import-legacy")
    legacy.add_argument("--dataset", required=True)
    legacy.add_argument("--files", type=Path, nargs="+", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        load_env()
        if args.command == "check-ibkr":
            check_ibkr_port()
            return 0
        if args.command == "rebuild-catalogue":
            print(f"Indexed {rebuild(ROOT)} artifact manifests")
            return 0
        if args.command == "import-legacy":
            print(
                f"Registered external archive: {import_legacy(ROOT, args.dataset, args.files)}"
            )
            return 0
        if args.command in ("resume", "status"):
            run = load_run(args.report, root=ROOT)
            if args.command == "status":
                print(json.dumps(run_summary(run), indent=2))
                return 0
        else:
            _, run = create_run(
                args.config,
                root=ROOT,
                instruments=args.instruments,
                all_instruments=args.all_instruments,
                start=args.start,
                end=args.end,
                frequency=args.frequency,
                refresh=args.refresh,
            )
            if args.command == "estimate":
                value = estimate_requests(
                    run, acknowledge_ibkr=args.acknowledge_ibkr_entitlements
                )
                path = save_estimate(
                    ROOT / "warehouse/reports", value, run_id=run["run_id"]
                )
                print_estimate(value)
                print(path.read_text(encoding="utf-8").strip())
                return 0 if value["all_costs_known"] else 2
        report = execute(
            ROOT,
            run,
            max_cost=args.max_cost_usd,
            approve_cost=args.approve_cost,
            acknowledge_ibkr=args.acknowledge_ibkr_entitlements,
        )
        return 0 if report["status"] == "complete" else 1
    except KeyboardInterrupt:
        print("Interrupted; run summary saved.")
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Stopped: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
