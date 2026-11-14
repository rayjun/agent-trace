#!/usr/bin/env python3
"""Isolated adversarial JSONL/cursor/recovery regressions; no real trace store."""
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import sqlite3
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.schema.model import CanonicalEvent  # noqa: E402
from core.storage.indexer import Indexer, _SourceChanged  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402


def event(n):
    ts = f"2026-10-01T00:00:{n:02}.000Z"
    return CanonicalEvent(type="span_start", adapter_id="probe", event_id=f"event-{n:04}",
                          ts=ts, observed_at=ts, session_id="session", trace_id=f"trace-{n}",
                          span_id=f"span-{n}", span={"kind": "llm", "name": "test", "status": "running"}).to_dict()


def write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def snapshot(store):
    return {table: sorted(tuple(r) for r in store.query(f"SELECT * FROM {table}"))
            for table in ("events", "spans", "sessions")}


if len(sys.argv) > 1 and sys.argv[1] == "child":
    base, mode = Path(sys.argv[2]), sys.argv[3]
    store = TraceStore(base / "index.db")
    if mode in ("archive", "direct"):
        store._apply_to_spans = lambda e: os._exit(77)
    elif mode != "replay":
        store.refresh_projections = lambda: os._exit(78)
    if mode == "direct":
        store.ingest(event(1), source_file=str(base / "traces" / "a.jsonl"), source_line=1)
    else:
        idx = Indexer(store, [base / "traces"])
        if mode == "replay":
            original = idx._ingest_from
            def after_file(*args, **kwargs):
                original(*args, **kwargs)
                os._exit(79)
            idx._ingest_from = after_file
        idx.scan_once()
    sys.exit(99)


class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.traces = self.base / "traces"
        self.traces.mkdir()
        self.path = self.traces / "a.jsonl"
        self.store = TraceStore(self.base / "index.db")
        self.addCleanup(lambda: self.store.close())
        self.idx = Indexer(self.store, [self.traces])

    def count(self, table):
        return self.store.one(f"SELECT COUNT(*) FROM {table}")[0]

    def equivalent(self):
        clean = TraceStore(self.base / "clean.db")
        try:
            Indexer(clean, [self.traces]).full_rebuild()
            # Source line is intentionally unavailable on a tail.
            def normalized(s):
                snap = snapshot(s)
                snap["events"] = [r[:-1] for r in snap["events"]]
                return snap
            self.assertEqual(normalized(self.store), normalized(clean))
        finally:
            clean.close()

    def test_partial_utf8_and_valid_json_without_newline(self):
        row = event(1)
        row["span"]["name"] = "分行"
        data = (json.dumps(row, ensure_ascii=False) + "\n").encode()
        for cut in (len(data) // 2, data.index("分".encode()) + 1, len(data) - 1):
            with self.subTest(cut=cut):
                self.store.clear_archive()
                self.path.write_bytes(data[:cut])
                r = self.idx.scan_once()
                self.assertEqual((r.corrupt_lines, self.count("events")), (0, 0))
                self.assertEqual(self.store.file_state(str(self.path))["indexed_bytes"], 0)
                with self.path.open("ab") as f:
                    f.write(data[cut:])
                self.idx.scan_once()
                self.assertEqual(self.count("events"), 1)
                self.equivalent()

    def test_append_at_final_stat(self):
        write(self.path, [event(1)])
        old_size = self.path.stat().st_size
        original, fired = Path.stat, []
        def race(path, *args, **kwargs):
            if path == self.path and inspect.currentframe().f_back.f_code.co_name == "_ingest_from" and not fired:
                fired.append(True)
                with path.open("a") as f:
                    f.write(json.dumps(event(2)) + "\n")
            return original(path, *args, **kwargs)
        with patch.object(Path, "stat", race):
            self.idx.scan_once()
        self.assertTrue(fired)
        state = self.store.file_state(str(self.path))
        self.assertEqual(state["indexed_bytes"], old_size)
        self.assertGreater(state["size"], old_size)
        self.idx.scan_once()
        self.assertEqual(self.count("events"), 2)
        self.equivalent()

    def test_source_mutation_at_final_stat(self):
        for mode in ("replace", "truncate"):
            with self.subTest(mode=mode):
                self.store.clear_archive()
                write(self.path, [event(1), event(2)])
                self.idx.scan_once()
                with self.path.open("a") as f:
                    f.write(json.dumps(event(3)) + "\n")
                original, fired = Path.stat, []
                def race(path, *args, mode=mode, fired=fired, original=original, **kwargs):
                    if path == self.path and inspect.currentframe().f_back.f_code.co_name == "_ingest_from" and not fired:
                        fired.append(True)
                        if mode == "replace":
                            other = self.base / "replacement"
                            write(other, [event(4)])
                            os.replace(other, path)
                        else:
                            write(path, [event(4)])
                    return original(path, *args, **kwargs)
                with patch.object(Path, "stat", race):
                    self.idx.scan_once()
                self.assertTrue(fired)
                self.idx.scan_once()
                self.equivalent()

    def test_abrupt_exit_boundaries(self):
        for mode in ("archive", "refresh", "direct"):
            with self.subTest(mode=mode):
                self.store.clear_archive()
                write(self.path, [event(1)])
                self.store.close()
                child = subprocess.run([sys.executable, __file__, "child", str(self.base), mode])
                self.assertEqual(child.returncode, 77 if mode != "refresh" else 78)
                self.store = TraceStore(self.base / "index.db")
                self.idx = Indexer(self.store, [self.traces])
                if mode == "direct":
                    self.assertEqual((self.count("spans"), self.count("sessions")), (1, 1))
                self.idx.scan_once()
                self.assertEqual((self.count("events"), self.count("spans"), self.count("sessions")), (1, 1, 1))
                self.assertEqual(self.idx.scan_once().events_added, 0)
                self.equivalent()

    def test_removing_episode_boundary_reassigns_surviving_records(self):
        def rec(name, n, **kw):
            return dict(v=1, agent="probe", session_id="session", turn_id="0",
                        ts=f"2026-10-01T00:00:{n:02}.000Z", event=name, **kw)
        write(self.path, [rec("user_prompt", 1)])
        other = self.traces / "z.jsonl"
        write(other, [rec("llm_request", 2, request_id="r1"),
                      rec("llm_response", 3, request_id="r1")])
        self.idx.scan_once()
        self.path.unlink()
        self.idx.scan_once()
        self.equivalent()

    def test_replay_crash_retains_durable_start_state(self):
        rows = [dict(v=1, agent="probe", session_id="session", turn_id="0",
                     ts=f"2026-10-01T00:00:{n:02}.000Z", event=name, **kw)
                for name, n, kw in [("llm_request", 1, {"request_id": "r1"}),
                                    ("llm_response", 2, {"request_id": "r1"}),
                                    ("user_prompt", 10, {}),
                                    ("llm_request", 11, {"request_id": "r2"}),
                                    ("llm_response", 12, {"request_id": "r2"})]]
        write(self.path, rows)
        other = self.traces / "z.jsonl"
        write(other, rows)
        self.idx.scan_once()
        self.store.invalidate_file(str(other))
        self.store.close()
        child = subprocess.run([sys.executable, __file__, "child", str(self.base), "replay"])
        self.assertEqual(child.returncode, 79)
        self.store = TraceStore(self.base / "index.db")
        self.assertTrue(self.store.replay_required())
        self.idx = Indexer(self.store, [self.traces])
        self.idx.scan_once()
        self.assertFalse(self.store.replay_required())
        self.equivalent()

    def test_direct_ingest_exception_recovers_on_same_process_scan(self):
        with patch.object(self.store, "_apply_to_spans", side_effect=RuntimeError("fault")):
            with self.assertRaises(RuntimeError):
                self.store.ingest(event(1))
        self.assertEqual(self.count("events"), 1)
        self.assertEqual(self.count("spans"), 0)
        self.idx.scan_once()
        self.assertEqual((self.count("events"), self.count("spans"), self.count("sessions")), (1, 1, 1))

    def test_additive_recovery_schema_needs_no_identity_rebuild(self):
        self.store.close()
        import sqlite3
        with sqlite3.connect(self.base / "index.db") as conn:
            conn.execute("DROP TABLE projection_pending")
        self.store = TraceStore(self.base / "index.db")
        self.assertIsNone(self.store.one("SELECT value FROM meta WHERE key='needs_rebuild'"))
        self.assertIsNotNone(self.store.one("SELECT name FROM sqlite_master WHERE name='projection_pending'"))

    def test_v1_cursor_seed_commit_survives_refresh_crash(self):
        rows = [dict(v=1, agent="probe", session_id="session", turn_id="0",
                     ts=f"2026-10-01T00:00:{n:02}.000Z", event=name, **kw)
                for name, n, kw in [("llm_request", 1, {"request_id": "r1"}),
                                    ("llm_response", 2, {"request_id": "r1"}),
                                    ("user_prompt", 10, {}),
                                    ("llm_request", 11, {"request_id": "r2"}),
                                    ("llm_response", 12, {"request_id": "r2"})]]
        write(self.path, rows)
        self.store.close()
        child = subprocess.run([sys.executable, __file__, "child", str(self.base), "refresh"])
        self.assertEqual(child.returncode, 78)
        import sqlite3
        with sqlite3.connect(self.base / "index.db") as conn:
            self.assertEqual(conn.execute("SELECT indexed_bytes FROM ingest_files").fetchone()[0], self.path.stat().st_size)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_episodes").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='projections_dirty'").fetchone()[0], "1")
        with patch.object(TraceStore, "rebuild_projections", side_effect=AssertionError("no full projection rebuild")):
            self.store = TraceStore(self.base / "index.db")
            self.idx = Indexer(self.store, [self.traces])
            self.assertEqual(self.idx.scan_once().events_added, 0)
        self.equivalent()

    def test_independent_store_cannot_advance_seed_during_scan(self):
        def rec(name, n, **kw):
            return dict(v=1, agent="probe", session_id="session", turn_id="0",
                        ts=f"2026-10-01T00:00:{n:02}.000Z", event=name, **kw)
        write(self.path, [rec("user_prompt", 1)])
        other = self.traces / "z.jsonl"
        write(other, [])
        self.idx.scan_once()
        paused, resume, finished = (threading.Event() for _ in range(3))
        failures, reports = [], []
        original = self.idx._scan_file

        def pause_after_seeds(path, report):
            if not paused.is_set():
                paused.set()
                if not resume.wait(5):
                    raise AssertionError("competitor never released scan")
            return original(path, report)

        def scan_a():
            try:
                reports.append(self.idx.scan_once())
            except BaseException as exc:
                failures.append(exc)
            finally:
                self.store.close()  # Close this thread's independent connection.
                finished.set()

        peer = TraceStore(self.base / "index.db")
        self.addCleanup(peer.close)
        # A nonblocking write attempt makes the handoff deterministic: either B
        # commits (the original bug), or SQLite refuses B until A has finished.
        peer._conn().execute("PRAGMA busy_timeout=0")
        competitor = Indexer(peer, [self.traces])
        with patch.object(self.idx, "_scan_file", pause_after_seeds):
            worker = threading.Thread(target=scan_a)
            worker.start()
            try:
                self.assertTrue(paused.wait(5), "A did not reach its seed snapshot")
                # An independent WAL reader must work even while A owns the writer.
                self.assertEqual(peer.query("SELECT COUNT(*) FROM events")[0][0], 1)
                with self.path.open("a") as f:
                    f.write(json.dumps(rec("user_prompt", 10)) + "\n")
                blocked = False
                try:
                    reports.append(competitor.scan_once())
                except sqlite3.OperationalError as exc:
                    self.assertIn("locked", str(exc))
                    blocked = True
                if not blocked:
                    write(other, [rec("llm_request", 11, request_id="r2"),
                                  rec("llm_response", 12, request_id="r2")])
            finally:
                resume.set()
                worker.join(5)
        self.assertFalse(worker.is_alive(), "scan deadlocked")
        self.assertTrue(finished.is_set())
        self.assertEqual(failures, [])
        if blocked:
            reports.append(competitor.scan_once())
            write(other, [rec("llm_request", 11, request_id="r2"),
                          rec("llm_response", 12, request_id="r2")])
        reports.append(self.idx.scan_once())
        # An ordinary idle scan must not be needed to repair incorrectly minted IDs.
        self.assertEqual(self.idx.scan_once().events_added, 0)
        self.assertTrue(all(not r.errors for r in reports), reports)
        self.equivalent()
        self.assertTrue(blocked, "independent writer entered A's scan snapshot")

    def test_source_retry_rolls_back_whole_scan_before_invalidation(self):
        write(self.path, [event(1)])
        other = self.traces / "z.jsonl"
        write(other, [event(2)])
        for replay in (False, True):
            with self.subTest(replay=replay):
                self.store.clear_archive()
                if replay:
                    self.store.invalidate_file(str(self.path))
                original_scan = self.idx._scan_file
                original_invalidate = self.store.invalidate_file
                fired, checked = [], []

                def changed(path, report, fired=fired, original_scan=original_scan):
                    if path == other and not fired:
                        fired.append(True)
                        self.assertEqual(self.count("events"), 1)
                        raise _SourceChanged(path)
                    return original_scan(path, report)

                def invalidate(path, checked=checked, original_invalidate=original_invalidate):
                    self.assertFalse(self.store._conn().in_transaction)
                    self.assertEqual(self.count("events"), 0)
                    self.assertIsNone(self.store.file_state(str(self.path)))
                    checked.append(True)
                    return original_invalidate(path)

                with patch.object(self.idx, "_scan_file", changed), patch.object(
                        self.store, "invalidate_file", invalidate):
                    report = self.idx.scan_once()
                self.assertEqual(report.errors, [])
                self.assertEqual(checked, [True])
                self.assertEqual(report.events_added, 2)
                self.equivalent()

    def test_full_rebuild_excludes_independent_writer_and_compacts_after_commit(self):
        write(self.path, [event(1)])
        self.idx.scan_once()
        peer = TraceStore(self.base / "index.db")
        self.addCleanup(peer.close)
        peer._conn().execute("PRAGMA busy_timeout=0")
        original_ingest, original_compact = self.idx._ingest_from, self.store.compact
        checked = []

        def ingest(*args, **kwargs):
            # The rebuild's clear is not visible to independent WAL readers.
            self.assertEqual(peer.query("SELECT COUNT(*) FROM events")[0][0], 1)
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                Indexer(peer, [self.traces]).scan_once()
            checked.append(True)
            return original_ingest(*args, **kwargs)

        def compact():
            self.assertFalse(self.store._conn().in_transaction)
            return original_compact()

        with patch.object(self.idx, "_ingest_from", ingest), patch.object(
                self.store, "compact", compact):
            report = self.idx.full_rebuild()
        self.assertEqual(report.errors, [])
        self.assertEqual(checked, [True])
        self.equivalent()

    def test_rewind_episode_start_state(self):
        def rec(name, n, **kw):
            return dict(v=1, agent="probe", session_id="session", turn_id="0",
                        ts=f"2026-10-01T00:00:{n:02}.000Z", event=name, **kw)
        rows = [rec("llm_request", 1, request_id="r1"), rec("llm_response", 2, request_id="r1"),
                rec("user_prompt", 10), rec("llm_request", 11, request_id="r2"),
                rec("llm_response", 12, request_id="r2")]
        write(self.path, rows)
        other = self.traces / "z.jsonl"
        write(other, [event(3)])
        self.idx.scan_once()
        other.write_text("")
        self.idx.scan_once()
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM spans WHERE span_id='llm:r1'")[0], 1)
        self.equivalent()
        other.unlink()
        self.idx.scan_once()
        self.equivalent()


if __name__ == "__main__":
    unittest.main()
