#!/usr/bin/env python3
"""Native rollout -> live capture -> indexed sessions, without installed hooks."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "cli"), str(ROOT / "common")]
from tui_data import LocalBackend  # noqa: E402
from tui_sources import CodexSync  # noqa: E402
from core.convert.v1_to_v2 import EpisodeTracker, convert_record  # noqa: E402

spec = importlib.util.spec_from_file_location("codex_live_importer", ROOT / "codex/codex_import.py")
assert spec is not None and spec.loader is not None
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


def line(kind, payload, second):
    return {"type": kind, "payload": payload, "timestamp": f"2026-11-15T00:00:{second:02}.000Z"}


def opening(session="session-a", turn="t1"):
    return [line("session_meta", {"id": session, "cwd": "/fixture/project",
                                  "base_instructions": {"text": "SYSTEM CAPTURE_FIXTURE"}}, 0),
            line("turn_context", {"turn_id": turn, "model": "fixture-model", "cwd": "/fixture/project"}, 1),
            line("event_msg", {"type": "user_message", "turn_id": turn, "message": "CAPTURE_FIXTURE"}, 2),
            line("response_item", {"type": "function_call", "name": "read_file", "call_id": "c1",
                                   "arguments": '{"path":"file.txt"}'}, 3)]


def closing(turn="t1"):
    return [line("response_item", {"type": "function_call_output", "call_id": "c1", "output": "tool reply"}, 4),
            line("event_msg", {"type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 100, "output_tokens": 7, "cached_input_tokens": 80}}}, 5),
            line("event_msg", {"type": "task_complete", "turn_id": turn,
                               "last_agent_message": "model reply", "duration_ms": 5000}, 6)]


def write(path, rows, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class LiveCapture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "codex"
        self.target = self.home / "traces"
        self.path = self.home / "sessions" / "2026/11/15/rollout-session-a.jsonl"
        self.environment = dict(os.environ, CODEX_HOME=str(self.home),
                                AGENTTRACE_CODEX_DIR=str(self.target),
                                AGENTTRACE_STATE_DIR=str(self.target / ".state"),
                                AGENTTRACE_CAPTURE="full")

    def records(self):
        return [json.loads(row) for path in self.target.glob("*.jsonl")
                for row in path.read_text().splitlines() if row]

    def capture(self, *paths):
        result = subprocess.run([sys.executable, str(ROOT / "codex/codex_import.py"),
                                 "--live", *map(str, paths or [self.path])],
                                env=self.environment, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_tail_recovers_metadata_and_tool_context_without_premature_end(self):
        write(self.path, opening())
        self.capture()
        self.assertFalse(any(r["event"] == "llm_response" for r in self.records()))
        write(self.path, closing(), "a")
        self.capture()
        records = self.records()
        self.assertTrue(all(r["session_id"] == "session-a" for r in records))
        responses = [r for r in records if r["event"] == "llm_response"]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["model"], "fixture-model")
        self.assertEqual(responses[0]["response"]["usage"]["input_tokens"], 100)
        result = next(r for r in records if r["event"] == "tool_result")
        self.assertEqual(result["tool"]["name"], "read_file")
        self.capture()
        self.assertEqual(self.records(), records)

    def test_partial_utf8_line_is_retried_and_not_consumed(self):
        write(self.path, opening())
        n, _, _ = importer.parse_rollout(self.path, live=True)
        row = line("response_item", {"type": "function_call_output", "call_id": "c1", "output": "中文🙂"}, 4)
        data = (json.dumps(row, ensure_ascii=False) + "\n").encode()
        cut = data.index("中".encode()) + 1
        with self.path.open("ab") as fh:
            fh.write(data[:cut])
        partial, records, meta = importer.parse_rollout(self.path, n, live=True)
        self.assertEqual(partial, n)
        self.assertEqual(records, [])
        self.assertEqual(meta["session_id"], "session-a")
        with self.path.open("ab") as fh:
            fh.write(data[cut:])
        complete, records, _ = importer.parse_rollout(self.path, n, live=True)
        self.assertEqual(complete, n + 1)
        self.assertEqual(records[0]["response"]["content"], "中文🙂")

    def test_concurrent_hook_and_poller_do_not_duplicate_records(self):
        write(self.path, opening() + closing())
        command = [sys.executable, str(ROOT / "codex/codex_import.py"), "--live", str(self.path)]
        first = subprocess.Popen(command, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(command, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for process in (first, second):
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(len(self.records()), 7)
        self.assertEqual(len(list((self.target / ".state").glob("cursor-*.json"))), 1)

    def test_live_capture_honors_metadata_only_mode(self):
        write(self.path, opening() + closing())
        self.environment["AGENTTRACE_CAPTURE"] = "metadata"
        self.capture()
        self.assertNotIn("CAPTURE_FIXTURE", json.dumps(self.records()))

    def test_failed_trace_write_does_not_advance_the_cursor(self):
        write(self.path, opening() + closing())
        with patch.object(importer, "_state_dir", self.target / ".state"), patch.object(
                importer, "_trace_dir", self.target):
            with patch.object(importer.common, "emit_record", return_value=None):
                with self.assertRaisesRegex(OSError, "cursor was not advanced"):
                    importer.import_rollout(self.path, live=True)
            self.assertFalse(list((self.target / ".state").glob("cursor-*.json")))
            self.assertEqual(importer.import_rollout(self.path, live=True), 7)

    def test_date_change_does_not_reimport_already_captured_history(self):
        write(self.path, opening() + closing())
        with patch.object(importer, "_state_dir", self.target / ".state"), patch.object(
                importer, "_trace_dir", self.target):
            self.assertEqual(importer.import_rollout(self.path, live=True), 7)
            with patch.object(importer, "_today_trace_file", return_value=self.target / "codex-next-day.jsonl"):
                with patch.object(importer, "_emit", side_effect=AssertionError("history was emitted twice")):
                    self.assertEqual(importer.import_rollout(self.path, live=True), 0)

    def test_metadata_only_and_old_cursor_sessions_become_selectable(self):
        write(self.path, opening()[:1])
        with patch.object(importer, "_state_dir", self.target / ".state"), patch.object(
                importer, "_trace_dir", self.target):
            # Simulate the previous importer consuming a header without writing
            # any session fact. An existing daily trace must not mask the gap.
            self.target.mkdir(parents=True)
            importer._today_trace_file().touch()
            cur = importer._cursor_path(self.path)
            cur.parent.mkdir(parents=True)
            cur.write_text(json.dumps({"path": str(self.path), "line": 1}))
            self.assertEqual(importer.import_rollout(self.path, live=True), 1)
            records = self.records()
            self.assertEqual(records[0]["event"], "session_start")
            self.assertEqual(records[0]["ts"], "2026-11-15T00:00:00.000Z")
            self.assertFalse(any(r["event"].startswith("llm_") for r in records))
            self.assertEqual(importer.import_rollout(self.path, live=True), 0)

    def test_header_never_changes_existing_or_offline_turn_identities(self):
        write(self.path, opening() + closing())
        _, live, _ = importer.parse_rollout(self.path, live=True)
        _, offline, _ = importer.parse_rollout(self.path)

        def canonical(records):
            tracker = EpisodeTracker()
            events = []
            for rec in records:
                row = {"v": 1, "agent": "codex", **rec}
                converted = convert_record(row, tracker.step(row))
                if converted and converted["type"].startswith("span_"):
                    events.append((converted["event_id"], converted["trace_id"]))
            return events

        self.assertEqual(canonical(live), canonical(offline))
        # Backfill an old header between an already captured request and the
        # result that will arrive later. This must not split the span in two.
        header = next(r for r in live if r["event"] == "session_start")
        start = next(r for r in live if r["event"] == "llm_request")
        finish = next(r for r in live if r["event"] == "llm_response")
        self.assertEqual(canonical([start, finish]), canonical([start, header, finish]))

    def test_background_capture_publishes_new_and_resumed_sessions(self):
        write(self.path, opening())
        with patch.dict(os.environ, self.environment):
            backend = LocalBackend(self.base / "index.db", [self.target], capture=True, interval=.05)
            backend.start()
            try:
                def wait(predicate):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        if predicate():
                            return
                        time.sleep(.03)
                    self.fail(f"capture did not publish data: {backend.errors}")

                wait(lambda: backend.dashboard()["sessions"]["total"] == 1)
                key = ("codex", "session-a")
                turn = backend.session(key)["turns"]["traces"][0]
                self.assertEqual(turn["status"], "incomplete")
                write(self.path, closing(), "a")
                wait(lambda: backend.session(key)["turns"]["traces"][0]["status"] == "ok")
                detail = backend.span(key, turn["trace_id"], "llm:codex-t1")
                self.assertEqual(detail["output"]["content"], "model reply")
                second = self.path.with_name("rollout-session-b.jsonl")
                write(second, opening("session-b") + closing())
                wait(lambda: backend.dashboard()["sessions"]["total"] == 2)
                expected = {r["session_id"] for r in backend.dashboard()["sessions"]["sessions"]}
                self.assertEqual(expected, {"session-a", "session-b"})
                write(self.path, [line("turn_context", {"turn_id": "t2", "model": "fixture-model"}, 10),
                                  line("event_msg", {"type": "user_message", "turn_id": "t2", "message": "resumed"}, 11)], "a")
                wait(lambda: backend.session(key)["turns"]["total"] == 2)
                self.assertEqual(backend.dashboard()["sessions"]["total"], 2)
                self.assertEqual(backend.errors, [])
                scans, size = backend.scans, sum(p.stat().st_size for p in self.target.glob("*.jsonl"))
                wait(lambda: backend.scans >= scans + 2)
                self.assertEqual(sum(p.stat().st_size for p in self.target.glob("*.jsonl")), size)
                self.assertFalse(backend.store.scan_was_incomplete())
                self.assertEqual(backend.store.duplicate_conflicts, 0)
            finally:
                backend.close()
                backend._thread.join(5)
                self.assertFalse(backend._thread.is_alive())

    def test_metadata_only_native_session_is_visible_with_zero_turns(self):
        write(self.path, [opening()[0]])
        with patch.dict(os.environ, self.environment):
            source = CodexSync([self.target])
            source.poll(threading.Event())
            backend = LocalBackend(self.base / "index.db", [self.target], index=False)
            backend.start()
            try:
                from core.storage.indexer import Indexer
                report = Indexer(backend.store, [self.target]).scan_once()
                self.assertEqual(report.errors + report.problems, [])
                sessions = backend.dashboard()["sessions"]
                self.assertEqual(sessions["total"], 1)
                self.assertEqual(sessions["sessions"][0]["session_id"], "session-a")
                self.assertEqual(sessions["sessions"][0]["trace_count"], 0)
                self.assertEqual(backend.session(("codex", "session-a"))["turns"]["total"], 0)
            finally:
                backend.close()

    def test_poll_skips_unchanged_sources_and_reports_import_failure(self):
        write(self.path, opening() + closing())
        with patch.dict(os.environ, self.environment):
            source = CodexSync([self.target])
            original = source.importer
            source.importer = self.base / "missing-importer.py"
            source.poll(threading.Event())
            self.assertEqual(len(source.errors), 1)
            self.assertEqual(source.fingerprints, {})
            source.importer = original
            source.poll(threading.Event())
            self.assertEqual(source.errors, [])
            size = sum(p.stat().st_size for p in self.target.glob("*.jsonl"))
            with patch("tui_sources.subprocess.run", side_effect=AssertionError("unchanged source was re-imported")):
                source.poll(threading.Event())
            self.assertEqual(sum(p.stat().st_size for p in self.target.glob("*.jsonl")), size)

    def test_no_index_never_imports_native_sessions(self):
        write(self.path, opening() + closing())
        with patch.dict(os.environ, self.environment):
            backend = LocalBackend(self.base / "index.db", [self.target], index=False, capture=True)
            backend.start()
            try:
                self.assertIsNone(backend.capture)
                self.assertFalse(self.target.exists())
                self.assertEqual(backend.dashboard()["sessions"]["total"], 0)
            finally:
                backend.close()


if __name__ == "__main__":
    unittest.main()
