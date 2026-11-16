#!/usr/bin/env python3
"""Textual backend contracts, using a disposable archive and real file tails."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "cli"), str(ROOT / "common")]
from core.schema.model import CanonicalEvent  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402
from tui_data import LocalBackend  # noqa: E402


def event(agent, session="same", n=0):
    return CanonicalEvent(
        type="span_end", adapter_id=agent, session_id=session,
        trace_id="shared", span_id=f"call-{n}", parent_span_id="shared",
        ts="2026-11-15T00:00:01.000Z",
        span={"kind": "llm", "name": "request", "status": "ok",
              "input": {"owner": agent}, "output": "reply-" + agent,
              "attributes": {"model": "model-" + agent,
                             "usage": {"input_tokens": 100, "output_tokens": 4}}},
    ).to_dict()


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.dirs = self.base / "traces"
        self.dirs.mkdir()
        self.db = self.base / "test.db"
        self.backend = None

    def tearDown(self):
        if self.backend:
            self.backend.close()
            if self.backend._thread:
                self.backend._thread.join(5)
                self.assertFalse(self.backend._thread.is_alive())
        self.tmp.cleanup()

    def test_same_named_sessions_and_traces_preserve_namespace(self):
        store = TraceStore(self.db)
        for agent in ("a", "b"):
            store.ingest(event(agent))
        store.refresh_projections()
        store.close()
        self.backend = LocalBackend(self.db, [self.dirs], index=False)
        self.backend.start()
        dashboard = self.backend.dashboard()
        self.assertEqual(dashboard["sessions"]["total"], 2)
        self.assertEqual(dashboard["overview"]["traces"], 2)
        for agent in ("a", "b"):
            selected = self.backend.session((agent, "same"))
            self.assertEqual(selected["turns"]["total"], 1)
            self.assertEqual(selected["turns"]["traces"][0]["adapter_id"], agent)
            trace = self.backend.trace((agent, "same"), "shared")
            self.assertEqual(trace["adapter_id"], agent)
            detail = self.backend.span((agent, "same"), "shared", "call-0")
            self.assertEqual(detail["input"], {"owner": agent})
        self.assertEqual(self.backend.dashboard(search="model-b")["sessions"]["total"], 1)
        self.assertEqual(self.backend.dashboard(agent="a")["sessions"]["total"], 1)
        self.assertIsNone(self.backend.store._local.conn)

    def test_index_thread_ingests_append_without_a_server(self):
        path = self.dirs / "live.jsonl"
        path.write_text(json.dumps(event("a")) + "\n")
        self.backend = LocalBackend(self.db, [self.dirs], interval=.05)
        self.backend.start()

        def wait_for(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(.02)
            self.fail("indexer did not publish the file tail")

        wait_for(lambda: self.backend.scans >= 1)
        self.assertEqual(self.backend.dashboard()["sessions"]["total"], 1)
        with path.open("a") as fh:
            fh.write(json.dumps(event("b")) + "\n")
        wait_for(lambda: self.backend.dashboard()["sessions"]["total"] == 2)
        self.assertEqual(self.backend.errors, [])

    def test_health_reports_corrupt_lines(self):
        source = self.dirs / "broken.jsonl"
        source.write_text("not-json\n" + json.dumps(event("a")) + "\n")
        self.backend = LocalBackend(self.db, [self.dirs], interval=60)
        self.backend.start()
        deadline = time.monotonic() + 5
        while not self.backend.scans and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertGreaterEqual(self.backend.scans, 1)
        health = self.backend.dashboard()["health"]
        self.assertIn("1 unparseable line(s)", health["errors"])
        self.assertEqual(self.backend.dashboard()["sessions"]["total"], 1)

    def test_health_includes_contract_conflicts_and_io_errors(self):
        from unittest.mock import patch
        from core.storage.indexer import Indexer, ScanReport
        report = ScanReport(events_invalid=2, event_id_conflicts=3,
                            errors=["synthetic unreadable source"])
        self.backend = LocalBackend(self.db, [self.dirs], interval=60)
        with patch.object(Indexer, "scan_once", return_value=report):
            self.backend.start()
            deadline = time.monotonic() + 5
            while not self.backend.scans and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertGreaterEqual(self.backend.scans, 1)
            self.assertEqual(self.backend.dashboard()["health"]["errors"],
                             report.errors + report.problems)

    def test_resolve_session_is_exact_and_independent_of_pages(self):
        store = TraceStore(self.db)
        for i in range(31):
            sid = "target" if i == 0 else f"target-copy-{i:02}"
            record = event("a", sid, i)
            record["ts"] = f"2026-11-15T00:00:{i:02}.000Z"
            record["trace_id"] = f"trace-{i}"
            store.ingest(record)
        store.ingest(event("b", "shared"))
        store.ingest(event("a", "shared"))
        store.refresh_projections()
        store.close()
        self.backend = LocalBackend(self.db, [], index=False)
        self.backend.start()
        first_page = self.backend.dashboard(search="target", agent="a")["sessions"]
        self.assertEqual(first_page["total"], 31)
        self.assertNotIn("target", [r["session_id"] for r in first_page["sessions"]])
        self.assertEqual(self.backend.resolve_session("target", "a"), ("a", "target"))
        self.assertEqual(self.backend.resolve_session("target"), ("a", "target"))
        self.assertEqual(self.backend.resolve_session("shared", "b"), ("b", "shared"))
        with self.assertRaises(ValueError):
            self.backend.resolve_session("shared")
        with self.assertRaises(LookupError):
            self.backend.resolve_session("target", "b")
        with self.assertRaises(LookupError):
            self.backend.resolve_session("target-cop", "a")
        self.assertIsNone(self.backend.store._local.conn)

    def test_no_index_does_not_read_or_change_source_files(self):
        source = self.dirs / "ignored.jsonl"
        content = json.dumps(event("a")) + "\n"
        source.write_text(content)
        self.backend = LocalBackend(self.db, [self.dirs], index=False)
        self.backend.start()
        self.assertEqual(self.backend.dashboard()["sessions"]["total"], 0)
        self.assertEqual(source.read_text(), content)
        self.assertIsNone(self.backend._thread)

    def test_event_only_turns_expose_recorded_events_in_their_namespace(self):
        store = TraceStore(self.db)
        for agent in ("a", "b"):
            store.ingest(CanonicalEvent(
                type="event", adapter_id=agent, session_id="same", trace_id="shared",
                ts="2026-11-15T00:00:01.000Z",
                event={"name": "user_prompt", "attributes": {"text": "prompt-" + agent}},
            ).to_dict())
        store.refresh_projections()
        store.close()
        self.backend = LocalBackend(self.db, [self.dirs], index=False)
        self.backend.start()
        self.assertFalse(self.backend.dashboard()["cache_known"])
        for agent in ("a", "b"):
            data = self.backend.span((agent, "same"), "shared", "shared")
            self.assertEqual([e["attributes"]["text"] for e in data["events"]], ["prompt-" + agent])


if __name__ == "__main__":
    unittest.main()
