"""Provider settings remain literal and never replace process credentials."""

from pathlib import Path
from contextlib import redirect_stdout
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from src.acquisition.acquire import load_env, ROOT


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / ".env"

    def test_missing_env_and_repository_location(self):
        environment = {}
        self.assertEqual(load_env(self.path, environ=environment), [])
        self.assertEqual(environment, {})
        self.assertEqual(ROOT, Path(__file__).resolve().parents[1])

    def test_process_values_win_and_empty_file_values_use_defaults(self):
        self.path.write_text(
            "DATABENTO_API_KEY=file-key\nIB_HOST= # use the default\nIB_PORT=4002\nIB_CLIENT_ID=''\n",
            encoding="utf-8",
        )
        environment = {"DATABENTO_API_KEY": "process-key", "IB_PORT": "7497"}
        self.assertEqual(load_env(self.path, environ=environment), [])
        self.assertEqual(
            environment, {"DATABENTO_API_KEY": "process-key", "IB_PORT": "7497"}
        )

    def test_literal_quoted_values_comments_and_supported_keys(self):
        self.path.write_text(
            "\ufeff# Local provider settings\n"
            'export DATABENTO_API_KEY="literal-${OTHER}-$(command)#key" # comment\n'
            "IB_HOST='127.0.0.1'\nIB_PORT=4001 # port\nIB_CLIENT_ID=71\n"
            "UNRELATED_SETTING=ignored\n",
            encoding="utf-8",
        )
        environment = {}
        loaded = load_env(self.path, environ=environment)
        self.assertEqual(
            set(loaded), {"DATABENTO_API_KEY", "IB_HOST", "IB_PORT", "IB_CLIENT_ID"}
        )
        self.assertEqual(
            environment,
            {
                "DATABENTO_API_KEY": "literal-${OTHER}-$(command)#key",
                "IB_HOST": "127.0.0.1",
                "IB_PORT": "4001",
                "IB_CLIENT_ID": "71",
            },
        )

    def test_malformed_known_values_do_not_partially_load_or_leak(self):
        for invalid in (
            'DATABENTO_API_KEY="secret-key',
            'DATABENTO_API_KEY="secret-key" garbage',
            "DATABENTO_API_KEY",
        ):
            with self.subTest(invalid=invalid):
                self.path.write_text(f"IB_PORT=4002\n{invalid}\n", encoding="utf-8")
                environment = {}
                with self.assertRaises(ValueError) as caught:
                    load_env(self.path, environ=environment)
                self.assertNotIn("secret-key", str(caught.exception))
                self.assertEqual(environment, {})

    def test_cli_loads_repository_env_before_dispatch(self):
        from src.acquisition.acquire import main

        self.path.write_text(
            "DATABENTO_API_KEY=fixture-only-key\nIB_PORT=4002\n", encoding="utf-8"
        )

        def dispatch(root):
            self.assertEqual(os.environ.get("DATABENTO_API_KEY"), "fixture-only-key")
            self.assertEqual(os.environ.get("IB_PORT"), "4002")
            return 0

        output = io.StringIO()
        with (
            patch("src.acquisition.acquire.ROOT", self.path.parent),
            patch.dict(os.environ, {}, clear=True),
            patch("src.acquisition.acquire.rebuild", side_effect=dispatch),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["rebuild-catalogue"]), 0)
        self.assertNotIn("fixture-only-key", output.getvalue())
