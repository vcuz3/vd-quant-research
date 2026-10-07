"""Acquisition invariants; all provider interactions are offline fixtures."""

from contextlib import redirect_stdout
from datetime import datetime
from decimal import Decimal
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from src.acquisition.acquire import rebuild
from src.acquisition.acquire import fred_csv, rba_xlsx
from src.acquisition.acquire import amount, estimate_requests, save_estimate
from src.acquisition.acquire import import_legacy
from src.acquisition.acquire import Estimate, Payload
from src.acquisition.acquire import create_run, load_run, requests_for
from src.acquisition.acquire import execute
from src.acquisition.acquire import write_bytes


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "configs/acquisition").mkdir(parents=True)
        (self.root / "configs/acquisition/datasets.toml").write_text(
            '[datasets.example]\nsource="fred"\nsymbol="DGS2"\ngranularity="daily"\n',
            encoding="utf-8",
        )
        self.config = self.root / "run.toml"
        self.config.write_text(
            '[run]\nstart="2020-01-01"\nend="2020-01-04"\ndatasets=["example"]\n',
            encoding="utf-8",
        )
        self.adapter = SimpleNamespace(
            estimate=lambda r: Estimate("0", "known", "fixture"),
            download=lambda r: [
                Payload("response.csv", b"native response\n", {"warnings": []})
            ],
        )
        self.prov = patch(
            "src.acquisition.acquire.provenance", return_value={"fixture": True}
        )
        self.prov.start()
        self.addCleanup(self.prov.stop)
        self.adapters = patch.dict(
            "src.acquisition.acquire.ADAPTERS", {"fred": self.adapter}
        )
        self.adapters.start()
        self.addCleanup(self.adapters.stop)

    def build_run(self, **kwargs):
        return create_run(self.config, root=self.root, **kwargs)

    def execute(self, run, **kwargs):
        with redirect_stdout(io.StringIO()):
            return execute(self.root, run, **kwargs)

    def test_finite_nonnegative_costs(self):
        for value in ("-1", "NaN", "Infinity", "bogus"):
            with self.assertRaises(ValueError):
                amount(value)
        self.assertEqual(amount("1.25"), Decimal("1.25"))

    def test_unknown_cost_blocks_before_download(self):
        _, run = self.build_run()
        self.adapter.estimate = lambda r: Estimate(None, "unknown", "fixture")
        with patch.object(self.adapter, "download") as download:
            with self.assertRaises(RuntimeError):
                self.execute(run)
            download.assert_not_called()

    def test_nonzero_cost_and_bad_cap_block_before_download(self):
        _, run = self.build_run()
        self.adapter.estimate = lambda r: Estimate("2", "known", "fixture")
        with patch.object(self.adapter, "download") as download:
            for kwargs in (
                {},
                {"max_cost": "3"},
                {"max_cost": "1", "approve_cost": True},
                {"max_cost": "NaN"},
            ):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.execute(run, **kwargs)
            download.assert_not_called()

    def test_paid_download_requires_separate_estimate(self):
        _, run = self.build_run()
        self.adapter.estimate = lambda r: Estimate("2", "known", "fixture")
        with self.assertRaises(RuntimeError):
            self.execute(run, max_cost="3", approve_cost=True)
        # The automatically saved execution estimate is not a reviewed quote.
        with patch.object(self.adapter, "download") as download:
            with self.assertRaises(RuntimeError):
                self.execute(run, max_cost="3", approve_cost=True)
            download.assert_not_called()

    def test_reviewed_paid_download_and_failed_reservation(self):
        path, run = self.build_run()
        self.adapter.estimate = lambda r: Estimate("2", "known", "fixture")
        save_estimate(path.parent, estimate_requests(run))
        with patch.object(
            self.adapter, "download", side_effect=RuntimeError("api_key=private")
        ):
            report = self.execute(run, max_cost="2", approve_cost=True)
        self.assertEqual(report["status"], "failed")
        self.assertNotIn("private", path.read_text())
        self.assertNotIn("private", json.dumps(load_run(path, self.root)))
        # A failed attempt may have incurred charges; resume must not reset its cap.
        with self.assertRaises(RuntimeError):
            self.execute(load_run(path, self.root), max_cost="2", approve_cost=True)

    def test_resume_does_not_redownload_verified_chunk(self):
        _, run = self.build_run()
        self.assertEqual(self.execute(run)["status"], "complete")
        with patch.object(self.adapter, "download") as download:
            report = self.execute(run)
            download.assert_not_called()
        self.assertEqual(report["records"][0]["status"], "cached")

    def test_corrupt_artifact_is_reacquired_as_new_snapshot(self):
        _, run = self.build_run()
        first = self.execute(run)
        manifest_path = self.root / first["records"][0]["manifest"]
        manifest = json.loads(manifest_path.read_text())
        old = self.root / manifest["files"][0]["path"]
        old.write_bytes(b"corrupted")
        second = self.execute(run)
        self.assertNotEqual(
            first["records"][0]["manifest"], second["records"][0]["manifest"]
        )
        self.assertEqual(old.read_bytes(), b"corrupted")

    def test_refresh_preserves_old_snapshot_and_resumes_itself(self):
        _, run = self.build_run()
        first = self.execute(run)
        _, refresh = self.build_run(refresh=True)
        second = self.execute(refresh)
        self.assertNotEqual(
            first["records"][0]["manifest"], second["records"][0]["manifest"]
        )
        with patch.object(self.adapter, "download") as download:
            self.execute(refresh)
            download.assert_not_called()

    def test_rebuild_after_catalogue_loss(self):
        _, run = self.build_run()
        self.execute(run)
        (self.root / "warehouse/meta.sqlite").unlink()
        self.assertEqual(rebuild(self.root), 1)

    def test_manifest_survives_catalogue_registration_failure(self):
        _, run = self.build_run()
        with patch(
            "src.acquisition.acquire.register", side_effect=RuntimeError("crash")
        ):
            self.assertEqual(self.execute(run)["status"], "failed")
        with patch.object(self.adapter, "download") as download:
            self.assertEqual(self.execute(run)["status"], "complete")
            download.assert_not_called()

    def test_report_tampering_is_rejected(self):
        path, run = self.build_run()
        self.execute(run)
        self.assertEqual(load_run(path, self.root)["run_id"], run["run_id"])
        run["requests"][0]["symbol"] = "OTHER"
        path.write_text(json.dumps(run))
        with self.assertRaises(ValueError):
            load_run(path, self.root)

    def test_request_identity_includes_granularity_and_price_type(self):
        dataset = {
            "source": "ibkr",
            "dataset_id": "fx",
            "granularity": "1m",
            "price_type": "MIDPOINT",
            "chunk_days": 2,
        }
        requests = list(requests_for(dataset, "2020-01-01", "2020-01-06"))
        self.assertEqual(
            [(r["start"], r["end"]) for r in requests],
            [
                ("2020-01-01", "2020-01-03"),
                ("2020-01-03", "2020-01-05"),
                ("2020-01-05", "2020-01-06"),
            ],
        )
        other = list(
            requests_for(dict(dataset, price_type="BID"), "2020-01-01", "2020-01-06")
        )
        self.assertNotEqual(
            requests[0]["request_fingerprint"], other[0]["request_fingerprint"]
        )

    def test_calendar_month_chunks_cover_leap_year_and_partial_edges(self):
        from src.acquisition.acquire import ibkr_duration
        from datetime import date

        dataset = {
            "source": "ibkr",
            "dataset_id": "audusd",
            "granularity": "1h",
            "chunk_months": 1,
        }
        requests = list(requests_for(dataset, "2024-01-15", "2024-04-10"))
        self.assertEqual(
            [(r["start"], r["end"]) for r in requests],
            [
                ("2024-01-15", "2024-02-01"),
                ("2024-02-01", "2024-03-01"),
                ("2024-03-01", "2024-04-01"),
                ("2024-04-01", "2024-04-10"),
            ],
        )
        durations = [
            ibkr_duration(date.fromisoformat(r["start"]), date.fromisoformat(r["end"]))
            for r in requests
        ]
        self.assertEqual(durations, ["17 D", "1 M", "1 M", "9 D"])

    def test_configured_monthly_window_has_201_requests(self):
        dataset = {
            "source": "ibkr",
            "dataset_id": "audusd",
            "granularity": "1h",
            "chunk_months": 1,
        }
        requests = list(requests_for(dataset, "2010-01-01", "2026-10-01"))
        self.assertEqual(len(requests), 201)
        self.assertEqual(requests[0]["start"], "2010-01-01")
        self.assertEqual(requests[-1]["end"], "2026-10-01")
        self.assertTrue(
            all(
                left["end"] == right["start"]
                for left, right in zip(requests, requests[1:])
            )
        )

    def test_padded_month_duration_changes_request_identity(self):
        from src.acquisition.acquire import ibkr_duration
        from datetime import date

        dataset = {"source": "ibkr", "dataset_id": "audusd", "chunk_months": 1}
        original = list(requests_for(dataset, "2026-09-01", "2026-10-01"))
        padded = list(
            requests_for(
                dict(dataset, boundary_padding_days=1), "2026-09-01", "2026-10-01"
            )
        )
        self.assertNotEqual(
            original[0]["request_fingerprint"], padded[0]["request_fingerprint"]
        )
        self.assertEqual(
            ibkr_duration(date(2026, 9, 1), date(2026, 10, 1), padding_days=1), "31 D"
        )
        self.assertEqual(
            ibkr_duration(date(2010, 1, 1), date(2010, 2, 1), padding_days=1), "32 D"
        )

    def test_monthly_config_still_uses_daily_chunks_for_minute_bars(self):
        self.selection_registry()
        registry = self.root / "configs/acquisition/datasets.toml"
        registry.write_text(
            registry.read_text().replace("chunk_days=7", "chunk_months=1")
        )
        _, run = self.build_run(instruments=["audusd"], frequency="1m")
        self.assertEqual(len(run["requests"]), 3)
        self.assertTrue(
            all(
                r["chunk_days"] == 1 and "chunk_months" not in r
                for r in run["requests"]
            )
        )

    def test_native_fred_missing_values_are_preserved(self):
        data = b"observation_date,DGS2\n2020-01-01,.\n2020-01-02,1.5\n2020-01-02,1.6\n"
        inspection = fred_csv(data, "DGS2", "2020-01-01", "2020-01-04")
        self.assertEqual(inspection["missing_value_rows"], 1)
        self.assertEqual(inspection["duplicate_dates"], 1)
        self.assertIn(b",.\n", data)

    def test_original_rba_workbook_selected_series_coverage(self):
        output = io.BytesIO()
        serial = (datetime(2013, 9, 2) - datetime(1899, 12, 30)).days
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(
                "xl/workbook.xml",
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                f"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
                <row><c r="B1" t="inlineStr"><is><t>FCMYGBAG2D</t></is></c></row>
                <row><c r="A2"><v>{serial - 1}</v></c><c r="B2"/></row>
                <row><c r="A3"><v>{serial}</v></c><c r="B3"><v>2.50</v></c></row>
                </sheetData></worksheet>""",
            )
        result = rba_xlsx(output.getvalue(), "FCMYGBAG2D", "2010-01-01", "2026-10-01")
        self.assertEqual(result["returned_start"], "2013-09-01")
        self.assertEqual(result["series_nonmissing_start"], "2013-09-02")
        self.assertEqual(result["missing_value_rows"], 1)
        self.assertNotIn("provider_header_rows", result)

    def test_sdk_warning_redacts_secret(self):
        _, run = self.build_run()

        def download(request):
            warnings.warn("degraded date; api_key=private", UserWarning)
            return [Payload("response.csv", b"native")]

        self.adapter.download = download
        report = self.execute(run)
        manifest = (self.root / report["records"][0]["manifest"]).read_text()
        self.assertIn("degraded date", manifest)
        self.assertNotIn("private", manifest)

    def test_legacy_import_cannot_claim_new_request_coverage(self):
        archive = self.root / "old.csv"
        archive.write_bytes(b"unverified archive")
        path = import_legacy(self.root, "example", [archive])
        manifest = json.loads(path.read_text())
        self.assertIsNone(manifest["acquired_at_utc"])
        _, run = self.build_run()
        self.assertIsNone(run["requests"][0]["cached_manifest"])

    def test_immutable_write_does_not_replace_file(self):
        path = self.root / "response"
        write_bytes(path, b"first")
        with self.assertRaises(FileExistsError):
            write_bytes(path, b"second")
        self.assertEqual(path.read_bytes(), b"first")

    def test_mid_download_failure_preserves_already_received_raw_file(self):
        path, run = self.build_run()

        def download(request):
            yield Payload("bars.csv", b"original bars")
            raise RuntimeError("companion request failed")

        self.adapter.download = download
        report = self.execute(run)
        self.assertEqual(report["status"], "failed")
        files = list((self.root / "warehouse/data/raw").rglob("bars.csv"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b"original bars")
        self.assertFalse(path.with_suffix(".events.jsonl").exists())
        self.assertFalse(
            list((self.root / "warehouse/data/raw").rglob("manifest.json"))
        )

    def test_refreshed_provider_quote_cannot_exceed_approval(self):
        registry = self.root / "configs/acquisition/datasets.toml"
        registry.write_text(
            '[datasets.example]\nsource="databento"\nsymbol="ES.v.0"\ngranularity="1m"\n',
            encoding="utf-8",
        )
        with patch.dict(
            "src.acquisition.acquire.ADAPTERS", {"databento": self.adapter}
        ):
            path, run = self.build_run()
            self.adapter.estimate = lambda r: Estimate("1", "known", "fixture")
            save_estimate(path.parent, estimate_requests(run))
            values = iter(("1", "3"))
            self.adapter.estimate = lambda r: Estimate(next(values), "known", "fixture")
            with patch.object(self.adapter, "download") as download:
                with self.assertRaises(RuntimeError):
                    self.execute(run, max_cost="2", approve_cost=True)
                download.assert_not_called()

    def test_ibkr_is_read_only_and_does_not_cache_empty_results(self):
        from src.acquisition.acquire import ibkr_download
        from src.acquisition.acquire import AcquisitionError

        class Client:
            def connect(self, host, port, **kwargs):
                import asyncio

                self.loop = asyncio.get_event_loop()
                self.connect_args = kwargs

            def qualifyContracts(self, contract):
                pass

            def reqHistoricalData(self, contract, **kwargs):
                self.request_args = kwargs
                return []

            def disconnect(self):
                pass

        client = Client()
        module = SimpleNamespace(
            IB=lambda: client, Forex=lambda symbol: SimpleNamespace(symbol=symbol)
        )
        with (
            patch.dict("sys.modules", {"ib_async": module}),
            patch("src.acquisition.acquire.time.sleep"),
        ):
            with self.assertRaises(AcquisitionError):
                ibkr_download(
                    {
                        "symbol": "AUDUSD",
                        "start": "2020-01-01",
                        "end": "2020-01-03",
                        "bar_size": "1 min",
                        "price_type": "MIDPOINT",
                    }
                )
        self.assertTrue(client.connect_args["readonly"])
        self.assertTrue(client.RaiseRequestErrors)
        self.assertEqual(client.request_args["formatDate"], 2)
        self.assertFalse(client.request_args["keepUpToDate"])
        self.assertTrue(client.loop.is_closed())

    def test_databento_quote_includes_definitions(self):
        from src.acquisition.acquire import databento_estimate

        calls = []

        def quote(**kwargs):
            calls.append(kwargs)
            return "0.25" if kwargs["schema"] == "ohlcv-1m" else "0.5"

        client = SimpleNamespace(metadata=SimpleNamespace(get_cost=quote))
        with patch("src.acquisition.acquire.databento_client", return_value=client):
            result = databento_estimate(
                {
                    "dataset": "GLBX.MDP3",
                    "schema": "ohlcv-1m",
                    "symbol": "GC.v.0",
                    "start": "2020-01-01",
                    "end": "2020-01-03",
                }
            )
        self.assertEqual(result.amount_usd, "0.75")
        self.assertEqual({v["schema"] for v in calls}, {"ohlcv-1m", "definition"})

    def test_ibkr_connection_retry_cap_is_three_attempts_with_backoff(self):
        from src.acquisition import acquire

        action = SimpleNamespace(call=None)
        with (
            patch.object(
                action, "call", side_effect=ConnectionRefusedError("private")
            ) as attempted,
            patch.object(acquire.time, "sleep") as sleep,
        ):
            with self.assertRaises(acquire.AcquisitionError) as caught:
                acquire.ibkr_retry(action.call, host="127.0.0.1", port=7496)
        self.assertEqual(attempted.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [10, 20])
        self.assertIn("2 retries", str(caught.exception))
        self.assertIn("7496", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    def test_ibkr_transient_failure_can_succeed_on_last_retry(self):
        from src.acquisition import acquire

        with (
            patch.object(
                acquire,
                "ibkr_download_once",
                side_effect=[
                    ConnectionRefusedError(),
                    TimeoutError(),
                    [Payload("bars.json", b"native")],
                ],
            ) as attempted,
            patch.object(acquire.time, "sleep"),
        ):
            result = acquire.ibkr_download({"pacing_seconds": 10})
        self.assertEqual(attempted.call_count, 3)
        self.assertEqual(result[0].inspection["attempts"], 3)

    def test_ibkr_provider_rejection_is_not_retried(self):
        from src.acquisition import acquire

        with (
            patch.object(
                acquire,
                "ibkr_download_once",
                side_effect=acquire.AcquisitionError("No entitled data"),
            ) as attempted,
            patch.object(acquire.time, "sleep"),
        ):
            with self.assertRaises(acquire.AcquisitionError):
                acquire.ibkr_download({})
        self.assertEqual(attempted.call_count, 1)

    def test_ibkr_failure_stops_before_later_chunks(self):
        from src.acquisition import acquire

        registry = self.root / "configs/acquisition/datasets.toml"
        registry.write_text(
            '[datasets.example]\nsource="ibkr"\nsymbol="AUDUSD"\nprice_type="MIDPOINT"\nchunk_days=1\n',
            encoding="utf-8",
        )
        path, run = self.build_run(end="2020-01-05")
        adapter = SimpleNamespace(download=acquire.ibkr_download)
        with (
            patch.dict(acquire.ADAPTERS, {"ibkr": adapter}),
            patch.object(
                acquire, "ibkr_download_once", side_effect=ConnectionRefusedError()
            ) as attempted,
            patch.object(acquire.time, "sleep"),
        ):
            report = self.execute(run, acknowledge_ibkr=True)
        self.assertEqual(attempted.call_count, 3)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["failures"]), 1)
        summary = json.loads(path.read_text())
        self.assertIn("2 retries", summary["note"])
        self.assertEqual(len(summary), 6)

    def test_check_ibkr_only_checks_one_socket(self):
        from src.acquisition import acquire

        with (
            patch("socket.create_connection") as opened,
            patch.dict(
                "os.environ",
                {"IB_HOST": "127.0.0.1", "IB_PORT": "7496", "IB_CLIENT_ID": "71"},
            ),
            redirect_stdout(io.StringIO()),
        ):
            acquire.check_ibkr_port()
        opened.assert_called_once_with(("127.0.0.1", 7496), timeout=3)

    def selection_registry(self):
        path = self.root / "configs/acquisition/datasets.toml"
        path.write_text(
            """[datasets.audusd]
source="ibkr"
symbol="AUDUSD"
price_type="MIDPOINT"
granularity="1m"
bar_size="1 min"
chunk_days=7
[datasets.gold]
source="databento"
symbol="GC.v.0"
dataset="GLBX.MDP3"
granularity="1m"
schema="ohlcv-1m"
[datasets.us_2y]
source="fred"
symbol="DGS2"
granularity="daily"
""",
            encoding="utf-8",
        )
        self.config.write_text(
            '[run]\nstart="2020-01-01"\nend="2020-01-04"\nfrequency="1h"\ndatasets=["audusd","gold","us_2y"]\n',
            encoding="utf-8",
        )

    def test_alias_selection_and_hourly_provider_requests(self):
        self.selection_registry()
        _, run = self.build_run(instruments=["AUDUSD", "GC", "dgs2"])
        requests = {r["dataset_id"]: r for r in run["requests"]}
        self.assertEqual(set(requests), {"audusd", "gold", "us_2y"})
        self.assertEqual(requests["audusd"]["bar_size"], "1 hour")
        self.assertEqual(requests["gold"]["schema"], "ohlcv-1h")
        self.assertEqual(requests["audusd"]["granularity"], "1h")
        self.assertEqual(requests["us_2y"]["granularity"], "daily")

    def test_all_configured_instruments_and_duplicate_alias_rejection(self):
        self.selection_registry()
        _, run = self.build_run(all_instruments=True)
        self.assertEqual(
            {r["dataset_id"] for r in run["requests"]}, {"audusd", "gold", "us_2y"}
        )
        _, single = self.build_run(instruments=["audusd"])
        self.assertEqual({r["dataset_id"] for r in single["requests"]}, {"audusd"})
        with self.assertRaises(ValueError):
            self.build_run(instruments=["gold", "gc"])

    def test_date_frequency_overrides_and_no_plan_output(self):
        self.selection_registry()
        _, hourly = self.build_run(
            instruments=["audusd", "gc"], start="2020-02-01", end="2020-02-03"
        )
        _, minutes = self.build_run(
            instruments=["audusd", "gc"],
            start="2020-02-01",
            end="2020-02-03",
            frequency="1m",
        )
        self.assertEqual(hourly["requests"][0]["start"], "2020-02-01")
        self.assertEqual(hourly["requests"][0]["end"], "2020-02-03")
        self.assertNotEqual(
            hourly["request_set_fingerprint"], minutes["request_set_fingerprint"]
        )
        self.assertEqual(minutes["requests"][0]["bar_size"], "1 min")
        self.assertEqual(minutes["requests"][1]["schema"], "ohlcv-1m")
        self.assertFalse((self.root / "warehouse").exists())

    def test_paid_review_matches_selection_across_run_ids(self):
        path, reviewed = self.build_run()
        self.adapter.estimate = lambda r: Estimate("1", "known", "fixture")
        save_estimate(path.parent, estimate_requests(reviewed))
        _, download = self.build_run()
        self.assertNotEqual(reviewed["run_id"], download["run_id"])
        self.assertEqual(
            self.execute(download, max_cost="1", approve_cost=True)["status"],
            "complete",
        )
        _, changed = self.build_run(end="2020-01-05")
        with patch.object(self.adapter, "download") as provider:
            with self.assertRaises(RuntimeError):
                self.execute(changed, max_cost="1", approve_cost=True)
            provider.assert_not_called()

    def test_zero_quote_becoming_paid_still_requires_review(self):
        registry = self.root / "configs/acquisition/datasets.toml"
        registry.write_text(
            '[datasets.example]\nsource="databento"\nsymbol="ES.v.0"\ngranularity="1h"\n',
            encoding="utf-8",
        )
        with patch.dict(
            "src.acquisition.acquire.ADAPTERS", {"databento": self.adapter}
        ):
            _, run = self.build_run()
            values = iter(("0", "1"))
            self.adapter.estimate = lambda r: Estimate(next(values), "known", "fixture")
            with patch.object(self.adapter, "download") as provider:
                with self.assertRaises(RuntimeError):
                    self.execute(run, max_cost="2", approve_cost=True)
                provider.assert_not_called()

    def test_reports_are_flat_and_resume_from_report(self):
        path, run = self.build_run()
        self.execute(run)
        stored = load_run(path, self.root)
        with patch.object(self.adapter, "download") as provider:
            self.execute(stored)
            provider.assert_not_called()
        self.assertFalse(path.with_suffix(".events.jsonl").exists())
        self.assertEqual(list(path.parent.iterdir()), [path])
        summary = json.loads(path.read_text())
        self.assertEqual(
            set(summary),
            {"date_ran", "amount_usd", "start_time", "finish_time", "status", "note"},
        )
        self.assertLess(path.stat().st_size, 1000)
        self.assertFalse(any(p.is_dir() for p in path.parent.iterdir()))
        self.assertFalse(list(self.root.rglob("plan.json")))

    def test_many_chunks_keep_summary_and_console_small(self):
        registry = self.root / "configs/acquisition/datasets.toml"
        registry.write_text(
            '[datasets.example]\nsource="ibkr"\nsymbol="AUDUSD"\nprice_type="MIDPOINT"\nchunk_days=1\n',
            encoding="utf-8",
        )
        self.adapters.stop()
        self.adapters = patch.dict(
            "src.acquisition.acquire.ADAPTERS", {"ibkr": self.adapter}
        )
        self.adapters.start()
        path, run = self.build_run(end="2020-01-31")
        output = io.StringIO()
        with redirect_stdout(output):
            execute(self.root, run, acknowledge_ibkr=True)
        summary = json.loads(path.read_text())
        self.assertEqual(summary["amount_usd"], "0")
        self.assertIn("30/30", summary["note"])
        self.assertLess(path.stat().st_size, 1000)
        self.assertLess(len(output.getvalue().splitlines()), 15)
        self.assertNotIn("Downloading", output.getvalue())
        self.assertGreaterEqual(
            datetime.fromisoformat(summary["finish_time"]),
            datetime.fromisoformat(summary["start_time"]),
        )

    def test_estimate_report_has_only_six_fields(self):
        path, run = self.build_run()
        estimate_path = save_estimate(path.parent, estimate_requests(run))
        summary = json.loads(estimate_path.read_text())
        self.assertEqual(
            set(summary),
            {"date_ran", "amount_usd", "start_time", "finish_time", "status", "note"},
        )
        self.assertEqual(summary["status"], "estimated")
        self.assertNotIn("requests", summary)

    def test_interruption_has_finish_time_and_can_resume(self):
        path, run = self.build_run()
        with patch.object(self.adapter, "download", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.execute(run)
        summary = json.loads(path.read_text())
        self.assertEqual(summary["status"], "interrupted")
        self.assertIsNotNone(summary["finish_time"])
        self.assertEqual(self.execute(load_run(path, self.root))["status"], "complete")

    def test_cli_download_only_selected_instruments_and_hourly_schema(self):
        from src.acquisition import acquire

        self.selection_registry()
        received = []

        def download(request):
            received.append(request)
            return [Payload("native.json", b"native fields")]

        adapter = SimpleNamespace(
            estimate=lambda r: Estimate("0", "known", "fixture"), download=download
        )
        with (
            patch.object(acquire, "ROOT", self.root),
            patch.dict(acquire.ADAPTERS, {"ibkr": adapter, "databento": adapter}),
            redirect_stdout(io.StringIO()),
        ):
            result = acquire.main(
                [
                    "download",
                    "--config",
                    str(self.config),
                    "--instruments",
                    "audusd",
                    "gc",
                    "--frequency",
                    "1h",
                    "--acknowledge-ibkr-entitlements",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual({r["dataset_id"] for r in received}, {"audusd", "gold"})
        self.assertEqual(received[0]["bar_size"], "1 hour")
        self.assertEqual(received[1]["schema"], "ohlcv-1h")
        self.assertFalse(list(self.root.rglob("plan.json")))


if __name__ == "__main__":
    unittest.main()
