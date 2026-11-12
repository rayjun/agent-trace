#!/usr/bin/env python3
"""Isolated lifecycle and loopback export tests; never installs a real service."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service = load("agenttrace_service", "agenttrace-service.py")
export = load("export_span", "export-span.py")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config with spaces %"
        self.environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config), "AGENTTRACE_PLATFORM_DIR": str(Path(self.temp.name) / 'state % $HOME "quoted"')})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.commands = patch.object(service.subprocess, "run", side_effect=self.run_command)
        self.mock_run = self.commands.start()
        self.addCleanup(self.commands.stop)
        self.output = io.StringIO()
        for stream in (contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output)):
            stream.__enter__()
            self.addCleanup(stream.__exit__, None, None, None)

    def run_command(self, args, **kwargs):
        answer = "yes" if args[0] == "loginctl" else "enabled" if "is-enabled" in args else "active"
        return subprocess.CompletedProcess(args, 0, answer + "\n", "")

    def test_install_uninstall_order_and_private_unit(self):
        self.assertEqual(service.main(["install", "--port", "9876"]), 0)
        path = service.unit_path()
        unit = path.read_text()
        self.assertTrue(unit.startswith(service.MARKER + "\n"))
        self.assertIn('"--port" "9876"', unit)
        self.assertIn('"--host" "127.0.0.1"', unit)
        self.assertIn('"--foreground"', unit)
        for setting in ("Restart=on-failure", "RestartSec=3", "UMask=0077", "WantedBy=default.target"):
            self.assertIn(setting, unit)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        commands = [call.args[0] for call in self.mock_run.call_args_list]
        self.assertEqual(commands[:2], [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "agenttrace.service"]])
        self.assertIn("linger: yes", self.output.getvalue())
        self.mock_run.reset_mock()
        self.assertEqual(service.main(["uninstall"]), 0)
        self.assertFalse(path.exists())
        self.assertEqual([call.args[0] for call in self.mock_run.call_args_list], [["systemctl", "--user", "disable", "--now", "agenttrace.service"], ["systemctl", "--user", "daemon-reload"]])

    def test_quoting(self):
        with patch.object(service, "ROOT", Path('/opt/repo space/%n/$HOME/"quote"')), patch.object(sys, "executable", '/opt/python space/%n/$USER/python'):
            unit = service.render_unit(8741)
        self.assertIn('ExecStart=":/opt/python space/%%n/$USER/python"', unit)
        self.assertIn('/opt/repo space/%%n/$HOME/\\"quote\\"/scripts/serve-daemon.py"', unit)
        self.assertIn('Environment="AGENTTRACE_PLATFORM_DIR=', unit)
        self.assertIn('state %% $HOME \\"quoted\\"', unit)
        self.assertEqual(service.quote_unit("a\nb\rc\td\\e"), '"a\\nb\\rc\\td\\\\e"')

    def test_dry_runs_have_no_writes_or_commands(self):
        for action in ("install", "status", "uninstall"):
            self.assertEqual(service.main([action, "--dry-run"]), 0)
        self.assertFalse(self.config.exists())
        self.mock_run.assert_not_called()

    def test_foreign_refusal_including_dry_run(self):
        path = service.unit_path()
        path.parent.mkdir(parents=True)
        path.write_text("[Service]\nExecStart=/bin/true\n")
        for action in ("install", "uninstall"):
            for extra in ([], ["--dry-run"]):
                self.assertEqual(service.main([action, *extra]), 1)
        self.assertEqual(path.read_text(), "[Service]\nExecStart=/bin/true\n")
        self.mock_run.assert_not_called()

    def test_symlink_refusal(self):
        path = service.unit_path()
        path.parent.mkdir(parents=True)
        path.symlink_to(Path(self.temp.name) / "missing")
        self.assertEqual(service.main(["install"]), 1)
        self.assertEqual(service.main(["uninstall"]), 1)
        self.mock_run.assert_not_called()

    def test_status_linger_instructions_no_command_output_leak(self):
        self.mock_run.side_effect = lambda args, **kw: subprocess.CompletedProcess(args, 1, "SENSITIVE", "SENSITIVE")
        self.assertEqual(service.main(["status"]), 0)
        self.assertIn("active: unknown", self.output.getvalue())
        self.assertIn("enabled: unknown", self.output.getvalue())
        self.assertIn("loginctl enable-linger", self.output.getvalue())
        self.assertNotIn("SENSITIVE", self.output.getvalue())

    def test_install_failure_and_non_linux(self):
        self.mock_run.side_effect = lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "SECRET")
        self.assertEqual(service.main(["install"]), 1)
        self.assertNotIn("SECRET", self.output.getvalue())
        self.mock_run.reset_mock()
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(service.main(["status"]), 1)
        self.mock_run.assert_not_called()


class ExportTests(unittest.TestCase):
    requests: list[tuple[str, str | None]] = []
    response_status = 200
    response_body = b"{}"

    @classmethod
    def setUpClass(cls):
        owner = cls

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Authorization")))
                self.send_response(owner.response_status)
                if owner.response_status == 302:
                    self.send_header("Location", "http://example.invalid/steal")
                self.end_headers()
                self.wfile.write(owner.response_body)

            def log_message(self, format, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.token = "private-test-token"
        (self.directory / "server.token").write_text(self.token)
        self.env = patch.dict(os.environ, {"AGENTTRACE_PLATFORM_DIR": str(self.directory), "http_proxy": "http://example.invalid:1", "no_proxy": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.trace = "trace /?#% Unicode中文"
        self.span = "span /?#%"
        self.payload = {"trace_id": self.trace, "span_id": self.span, "session_id": "session", "input": "FULL_SECRET_PAYLOAD", "output": {"full": "result"}}
        type(self).response_status = 200
        type(self).response_body = json.dumps(self.payload).encode()
        type(self).requests = []
        self.output = self.directory / "export.json"
        self.stream = io.StringIO()
        for stream in (contextlib.redirect_stdout(self.stream), contextlib.redirect_stderr(self.stream)):
            stream.__enter__()
            self.addCleanup(stream.__exit__, None, None, None)

    def cli(self):
        return export.main(["--trace", self.trace, "--span", self.span, "--output", str(self.output), "--port", str(self.server.server_port)])

    def test_full_export_permissions_escaping_and_no_logging(self):
        self.assertEqual(self.cli(), 0)
        self.assertEqual(json.loads(self.output.read_text()), self.payload)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        path, auth = self.requests[0]
        self.assertEqual(path, "/api/traces/" + export.urllib.parse.quote(self.trace, safe="") + "/spans/" + export.urllib.parse.quote(self.span, safe=""))
        self.assertEqual(auth, "Bearer " + self.token)
        self.assertNotIn(self.token, self.stream.getvalue())
        self.assertNotIn("FULL_SECRET_PAYLOAD", self.stream.getvalue())

    def test_overwrite_refused_before_request(self):
        self.output.write_text("original")
        self.assertEqual(self.cli(), 1)
        self.assertEqual(self.output.read_text(), "original")
        self.assertEqual(self.requests, [])

    def test_atomic_exclusive_write(self):
        self.output.write_text("original")
        with self.assertRaises(FileExistsError):
            export.write_export(self.output, self.payload)
        self.assertEqual(self.output.read_text(), "original")

    def test_wrong_identities_and_non_object(self):
        for payload in ({**self.payload, "trace_id": "wrong"}, {**self.payload, "span_id": "wrong"}, {}, [self.payload]):
            type(self).response_body = json.dumps(payload).encode()
            self.assertEqual(self.cli(), 1)
            self.assertFalse(self.output.exists())

    def test_redirect_not_followed_and_http_body_not_logged(self):
        for code in (302, 401, 404, 500):
            type(self).response_status = code
            type(self).response_body = b"FULL_SECRET_PAYLOAD"
            self.assertEqual(self.cli(), 1)
            self.assertFalse(self.output.exists())
        self.assertEqual(len(self.requests), 4)
        self.assertNotIn("FULL_SECRET_PAYLOAD", self.stream.getvalue())
        self.assertNotIn(self.token, self.stream.getvalue())

    def test_invalid_json_and_token(self):
        type(self).response_body = b"not JSON FULL_SECRET_PAYLOAD"
        self.assertEqual(self.cli(), 1)
        (self.directory / "server.token").write_text("invalid\r\ntoken")
        self.assertEqual(self.cli(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.output.exists())

    def test_default_token_directory(self):
        directory = self.directory / ".agenttrace"
        directory.mkdir()
        (directory / "server.token").write_text(self.token)
        with patch.dict(os.environ, {"AGENTTRACE_PLATFORM_DIR": ""}), patch.object(export.Path, "home", return_value=self.directory):
            self.assertEqual(self.cli(), 0)

    def test_timeout_and_loopback_request(self):
        with patch.object(export.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = TimeoutError
            with self.assertRaises(TimeoutError):
                export.fetch_span(self.trace, self.span, 8741)
            args, kwargs = build.return_value.open.call_args
            self.assertTrue(args[0].full_url.startswith("http://127.0.0.1:8741/"))
            self.assertEqual(kwargs["timeout"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
