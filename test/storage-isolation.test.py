#!/usr/bin/env python3
"""Exercise file rollback and namespace isolation through the real archive."""
import builtins
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import query  # noqa: E402
from core.schema.model import CanonicalEvent  # noqa: E402
from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore, _SPANS_TABLE  # noqa: E402

TS = "2026-11-12T00:00:00.000Z"
END = "2026-11-12T00:00:01.000Z"


def span(agent="a", session="same", *, end=False):
    return CanonicalEvent(
        type="span_end" if end else "span_start", adapter_id=agent,
        ts=END if end else TS, session_id=session, trace_id="shared",
        span_id="child", parent_span_id="shared",
        span={"kind": "llm", "name": agent, "status": "ok" if end else "running",
              "input": {"owner": [agent, session]},
              "attributes": {"model": "model-" + agent, "cwd": "/workspace/" + agent,
                             "usage": {"input_tokens": 7 if agent == "a" else 90}}},
    ).to_dict()


def event(agent="a", session="same", name="user_prompt"):
    return CanonicalEvent(type="event", adapter_id=agent, ts=TS,
                          session_id=session, trace_id="shared",
                          event={"name": name}).to_dict()


class Isolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.store = TraceStore(self.base / "index.db")
        self.addCleanup(lambda: self.store.close())

    def test_unreadable_source_does_not_block_healthy_tail(self):
        directory = self.base / "traces"
        directory.mkdir()
        good, bad = directory / "a.jsonl", directory / "z.jsonl"
        first, second = span(), span(end=True)
        good.write_text(json.dumps(first) + "\n")
        bad.write_text(json.dumps(span("b")) + "\n")
        original = builtins.open

        def deny(path, *args, **kwargs):
            if Path(path) == bad:
                raise PermissionError("unreadable source")
            return original(path, *args, **kwargs)

        indexer = Indexer(self.store, [directory])
        with patch("builtins.open", deny):
            report = indexer.scan_once()
            self.assertEqual(report.events_added, 1)
            self.assertTrue(report.errors)
            with good.open("a") as fh:
                fh.write(json.dumps(second) + "\n")
            self.assertEqual(indexer.scan_once().events_added, 1)
        self.assertFalse(indexer.scan_once().errors)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM events")[0], 3)
        self.assertEqual(indexer.scan_once().events_added, 0)
        self.assertEqual(query.get_span(self.store, "shared", "child", agent="a")["status"], "ok")

    def test_recovered_v1_source_replays_episode_order_after_restart(self):
        directory = self.base / "traces"
        directory.mkdir()

        def record(name, second, **kw):
            return dict(v=1, agent="a", session_id="same", turn_id="0",
                        ts=f"2026-11-12T00:00:{second:02}.000Z", event=name, **kw)

        bad = directory / "a.jsonl"
        bad.write_text("".join(json.dumps(r) + "\n" for r in [
            record("llm_request", 1, request_id="r1"),
            record("llm_response", 2, request_id="r1")]))
        good = directory / "z.jsonl"
        good.write_text("".join(json.dumps(r) + "\n" for r in [
            record("user_prompt", 10), record("llm_request", 11, request_id="r2"),
            record("llm_response", 12, request_id="r2")]))
        original = builtins.open

        def deny(path, *args, **kwargs):
            if Path(path) == bad:
                raise PermissionError("unreadable source")
            return original(path, *args, **kwargs)

        indexer = Indexer(self.store, [directory])
        with patch("builtins.open", deny):
            self.assertEqual(indexer.scan_once().events_added, 3)
            with good.open("a") as fh:
                fh.write(json.dumps(record("llm_request", 13, request_id="r3")) + "\n")
            self.assertEqual(indexer.scan_once().events_added, 1)
        self.store.close()
        self.store = TraceStore(self.base / "index.db")
        self.assertTrue(self.store.scan_was_incomplete())
        report = Indexer(self.store, [directory]).scan_once()
        self.assertFalse(report.errors)
        self.assertFalse(self.store.scan_was_incomplete())
        clean = TraceStore(self.base / "clean.db")
        try:
            Indexer(clean, [directory]).full_rebuild()
            for sql in ("SELECT event_id FROM events ORDER BY event_id",
                        "SELECT trace_id, span_id, status FROM spans ORDER BY trace_id, span_id"):
                self.assertEqual([tuple(r) for r in self.store.query(sql)],
                                 [tuple(r) for r in clean.query(sql)])
            self.assertEqual(self.store.session_seeds(), clean.session_seeds())
        finally:
            clean.close()

    def test_unmounted_source_healing_defers_replay_until_remount_after_restart(self):
        volume, live = self.base / "volume", self.base / "live"
        volume.mkdir()
        live.mkdir()

        def record(name, second, **kw):
            return dict(v=1, agent="a", session_id="same", turn_id="0",
                        ts=f"2026-11-12T00:00:{second:02}.000Z", event=name, **kw)

        def write(path, records):
            path.write_text("".join(json.dumps(r) + "\n" for r in records))

        archived = volume / "a.jsonl"
        write(archived, [record("llm_request", 1, request_id="r1"),
                         record("llm_response", 2, request_id="r1")])
        good = live / "z.jsonl"
        write(good, [record("user_prompt", 10),
                     record("llm_request", 11, request_id="r2"),
                     record("llm_response", 12, request_id="r2")])
        indexer = Indexer(self.store, [volume, live])
        self.assertFalse(indexer.scan_once().errors)
        state = dict(self.store.file_state(str(archived)))
        old_rows = [tuple(r) for r in self.store.query(
            "SELECT * FROM events WHERE source_file=? ORDER BY event_id", (str(archived),))]
        detached = self.base / "detached"
        volume.rename(detached)
        bad = live / "bad.jsonl"
        write(bad, [record("user_prompt", 20)])
        original = builtins.open

        def deny(path, *args, **kwargs):
            if Path(path) == bad:
                raise PermissionError("synthetic unreadable source")
            return original(path, *args, **kwargs)

        with patch("builtins.open", deny):
            self.assertTrue(indexer.scan_once().errors)
        self.assertFalse(indexer.scan_once().errors)
        # Healing another file must not rewind or re-identify the detached one.
        self.assertEqual(dict(self.store.file_state(str(archived))), state)
        self.assertEqual(old_rows, [tuple(r) for r in self.store.query(
            "SELECT * FROM events WHERE source_file=? ORDER BY event_id", (str(archived),))])
        self.assertTrue(self.store.scan_was_incomplete())
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM events")[0], 6)
        # Healthy tails still publish while ordered identity repair is deferred.
        with good.open("a") as fh:
            fh.write(json.dumps(record("llm_request", 21, request_id="r3")) + "\n")
        self.assertEqual(indexer.scan_once().events_added, 1)
        self.assertEqual(self.store.file_state(str(good))["indexed_bytes"], good.stat().st_size)
        self.store.close()
        self.store = TraceStore(self.base / "index.db")
        self.assertTrue(self.store.scan_was_incomplete())
        detached.rename(volume)
        indexer = Indexer(self.store, [volume, live])
        self.assertFalse(indexer.scan_once().errors)
        self.assertFalse(self.store.scan_was_incomplete())
        self.assertFalse(self.store.replay_required())
        clean = TraceStore(self.base / "clean.db")
        try:
            self.assertFalse(Indexer(clean, [volume, live]).full_rebuild().errors)
            for sql in ("SELECT * FROM events ORDER BY event_id",
                        "SELECT * FROM spans ORDER BY adapter_id, session_id, trace_id, span_id",
                        "SELECT * FROM sessions ORDER BY adapter_id, session_id"):
                self.assertEqual([tuple(r) for r in self.store.query(sql)],
                                 [tuple(r) for r in clean.query(sql)])
            self.assertEqual(self.store.session_seeds(), clean.session_seeds())
            self.assertEqual(indexer.scan_once().events_added, 0)
        finally:
            clean.close()

    def test_pending_replay_preserves_detached_cursor_and_healthy_progress(self):
        for mutation in ("delete", "replace"):
            with self.subTest(mutation=mutation):
                self.store.clear_archive()
                volume, live = self.base / (mutation + "-volume"), self.base / (mutation + "-live")
                volume.mkdir()
                live.mkdir()
                archived, good, changed = volume / "a.jsonl", live / "z.jsonl", live / "b.jsonl"
                archived.write_text(json.dumps(dict(v=1, agent="a", session_id="same", turn_id="0",
                    ts=TS, event="llm_request", request_id="archived")) + "\n")
                good.write_text(json.dumps(span()) + "\n")
                changed.write_text(json.dumps(event("b")) + "\n")
                indexer = Indexer(self.store, [volume, live])
                self.assertFalse(indexer.scan_once().errors)
                state = dict(self.store.file_state(str(archived)))
                archived_rows = [tuple(r) for r in self.store.query(
                    "SELECT * FROM events WHERE source_file=?", (str(archived),))]
                detached = self.base / (mutation + "-detached")
                volume.rename(detached)
                if mutation == "replace":
                    # Keep the old inode allocated, avoiding unlink/recreate reuse.
                    changed.rename(live / "old.source")
                    changed.write_text(json.dumps(event("b", name="session_end")) + "\n")
                else:
                    changed.unlink()
                with good.open("a") as fh:
                    fh.write(json.dumps(span(end=True)) + "\n")
                report = indexer.scan_once()
                self.assertFalse(report.errors)
                self.assertEqual(report.files_dropped if mutation == "delete" else report.files_invalidated, 1)
                self.assertGreaterEqual(report.events_added, 1)
                self.assertEqual(dict(self.store.file_state(str(archived))), state)
                self.assertEqual(archived_rows, [tuple(r) for r in self.store.query(
                    "SELECT * FROM events WHERE source_file=?", (str(archived),))])
                self.assertEqual(self.store.file_state(str(good))["indexed_bytes"], good.stat().st_size)
                self.assertTrue(self.store.replay_required())
                # The store boundary itself also refuses a partial destructive reset.
                seeds = self.store.session_seeds()
                with self.store.transaction():
                    with self.assertRaisesRegex(OSError, "unavailable sources"):
                        self.store.prepare_replay([str(good)])
                self.assertEqual(self.store.session_seeds(), seeds)
                self.store.close()
                self.store = TraceStore(self.base / "index.db")
                detached.rename(volume)
                indexer = Indexer(self.store, [volume, live])
                self.assertFalse(indexer.scan_once().errors)
                self.assertFalse(self.store.replay_required())
                self.assertFalse(self.store.scan_was_incomplete())
                clean = TraceStore(self.base / (mutation + "-clean.db"))
                try:
                    self.assertFalse(Indexer(clean, [volume, live]).full_rebuild().errors)
                    for sql in ("SELECT * FROM events ORDER BY event_id",
                                "SELECT * FROM spans ORDER BY adapter_id, session_id, trace_id, span_id"):
                        self.assertEqual([tuple(r) for r in self.store.query(sql)],
                                         [tuple(r) for r in clean.query(sql)])
                    self.assertEqual(self.store.session_seeds(), clean.session_seeds())
                    self.assertEqual(indexer.scan_once().events_added, 0)
                finally:
                    clean.close()

    def test_failed_ordered_replay_rolls_back_then_publishes_healthy_tail(self):
        directory = self.base / "traces"
        directory.mkdir()
        bad, good = directory / "a.jsonl", directory / "z.jsonl"
        bad.write_text(json.dumps(span("b")) + "\n")
        good.write_text(json.dumps(span()) + "\n")
        indexer = Indexer(self.store, [directory])
        self.assertFalse(indexer.scan_once().errors)
        state = dict(self.store.file_state(str(bad)))
        self.store.mark_scan_incomplete()
        with good.open("a") as fh:
            fh.write(json.dumps(span(end=True)) + "\n")
        original = builtins.open

        def deny(path, *args, **kwargs):
            if Path(path) == bad:
                raise PermissionError("synthetic unreadable source")
            return original(path, *args, **kwargs)

        with patch("builtins.open", deny):
            report = indexer.scan_once()
        self.assertEqual(report.events_added, 1)
        self.assertEqual(dict(self.store.file_state(str(bad))), state)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM events")[0], 3)
        self.assertEqual(self.store.file_state(str(good))["indexed_bytes"], good.stat().st_size)
        self.assertTrue(self.store.scan_was_incomplete())
        self.assertFalse(indexer.scan_once().errors)
        self.assertFalse(self.store.scan_was_incomplete())
        self.assertEqual(indexer.scan_once().events_added, 0)

    def test_mid_file_failure_rolls_back_events_counters_and_episode_seed(self):
        directory = self.base / "traces"
        directory.mkdir()
        rows = [dict(v=1, agent="a", session_id="same", turn_id="0", ts=TS,
                     event="user_prompt"),
                dict(v=1, agent="a", session_id="same", turn_id="0", ts=END,
                     event="llm_request", request_id="r1")]
        path = directory / "a.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        (directory / "z.jsonl").write_text(json.dumps(span("b")) + "\n")
        indexer = Indexer(self.store, [directory])
        enrich = indexer._enrich

        def fail_second(e):
            if e["adapter_id"] == "a" and e["type"] == "span_start":
                raise OSError("read interrupted after a complete record")
            return enrich(e)

        with patch.object(indexer, "_enrich", fail_second):
            report = indexer.scan_once()
        self.assertTrue(report.errors)
        self.assertEqual(report.events_added, 1)
        self.assertIsNone(self.store.file_state(str(path)))
        self.assertEqual(self.store.session_seeds(), {})
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM events")[0], 1)
        self.assertFalse(indexer.scan_once().errors)
        actual = sorted(r[0] for r in self.store.query("SELECT event_id FROM events"))
        clean = TraceStore(self.base / "clean.db")
        try:
            Indexer(clean, [directory]).scan_once()
            self.assertEqual(actual, sorted(r[0] for r in clean.query("SELECT event_id FROM events")))
        finally:
            clean.close()

    def test_real_projection_and_updates_keep_same_named_sessions_separate(self):
        for agent in ("a", "b"):
            self.assertTrue(self.store.ingest(span(agent)))
        self.store.ingest(event(name="session_end"))
        self.store.refresh_projections()
        sessions = {r["adapter_id"]: r for r in query.list_sessions(self.store)["sessions"]}
        for agent in ("a", "b"):
            self.assertEqual(sessions[agent]["model"], "model-" + agent)
            self.assertEqual(sessions[agent]["cwd"], "/workspace/" + agent)
            self.assertEqual(sessions[agent]["ended"], agent == "a")
        self.store.ingest(span(end=True))
        self.store.refresh_projections()
        self.assertEqual(query.get_span(self.store, "shared", "child", agent="a")["status"], "ok")
        self.assertEqual(query.get_span(self.store, "shared", "child", agent="b")["status"], "incomplete")
        listed = query.list_traces(self.store)
        self.assertEqual(listed["total"], 2)
        self.assertEqual({r["adapter_id"]: r["tokens"]["input"] for r in listed["traces"]}, {"a": 7, "b": 90})
        self.assertEqual(query.overview(self.store)["traces"], 2)

    def test_event_only_roots_are_independent_and_null_is_not_empty_session(self):
        contexts = [("a", "same"), ("b", "same"), ("a", "other"), ("a", None), ("a", "")]
        for agent, session in contexts:
            self.store.ingest(event(agent, session))
        for _ in range(2):
            self.store.refresh_projections()
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM spans")[0], len(contexts))
        for agent, session in contexts:
            # An absent session selector means unqualified; inspect NULL directly.
            if session is None:
                self.assertEqual(self.store.one("SELECT COUNT(*) FROM spans WHERE session_id IS NULL")[0], 1)
                continue
            trace = query.get_trace(self.store, "shared", agent=agent, session=session)
            self.assertIsNotNone(trace)
            self.assertEqual(len(trace["events"]), 1)
            self.assertEqual(trace["session_id"], session)
        with self.assertRaises(query.AmbiguousTraceError):
            query.get_trace(self.store, "shared")
        self.assertEqual(query.list_traces(self.store)["total"], len(contexts))
        self.assertEqual(query.overview(self.store)["traces"], len(contexts))
        self.assertEqual({r["adapter_id"]: r["traces"] for r in query.overview(self.store)["per_agent"]}, {"a": 4, "b": 1})

    def test_native_span_null_and_empty_sessions_do_not_duplicate_or_merge(self):
        for session in (None, ""):
            self.store.ingest(span(session=session))
            self.store.ingest(span(session=session, end=True))
        self.store.refresh_projections()
        self.store.rebuild_projections()
        for session in (None, ""):
            rows = self.store.query("SELECT * FROM spans WHERE session_id IS ?", (session,))
            self.assertEqual(len(rows), 2)
            child = next(r for r in rows if r["span_id"] == "child")
            self.assertEqual(child["status"], "ok")
            self.assertEqual(json.loads(child["input_json"])["owner"], ["a", session])

    def test_native_task_only_replaces_its_own_synthetic_root(self):
        for agent in ("a", "b"):
            self.store.ingest(span(agent))
        self.store.refresh_projections()
        for end in (False, True):
            native = span(end=end)
            native["span_id"] = "shared"
            native.pop("parent_span_id")
            native["span"] = {"kind": "task", "name": "real task",
                              "status": "error" if end else "running"}
            self.store.ingest(native)
        self.store.refresh_projections()
        own = query.get_span(self.store, "shared", "shared", agent="a")
        other = query.get_span(self.store, "shared", "shared", agent="b")
        self.assertFalse(own["synthetic"])
        self.assertEqual((own["name"], own["status"]), ("real task", "error"))
        self.assertTrue(other["synthetic"])

    def test_interrupted_migration_recovery_retries_without_touching_archive(self):
        for agent in ("a", "b"):
            self.store.ingest(event(agent))
        self.store.refresh_projections()
        self.store.close()
        self.use_legacy_projection()
        failed = TraceStore.__new__(TraceStore)
        try:
            with patch.object(TraceStore, "refresh_projections", side_effect=RuntimeError("interrupted")):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    failed.__init__(self.base / "index.db")
            self.assertIsNotNone(failed.one("SELECT 1 FROM meta WHERE key='namespace_rebuild'"))
            self.assertEqual(failed.one("SELECT COUNT(*) FROM events")[0], 2)
        finally:
            failed.close()
        self.store = TraceStore(self.base / "index.db")
        for agent in ("a", "b"):
            self.assertEqual(len(query.get_trace(self.store, "shared", agent=agent)["events"]), 1)
        self.assertIsNone(self.store.one("SELECT 1 FROM meta WHERE key='namespace_rebuild'"))

    def use_legacy_projection(self):
        # Recreate the pre-fix projection, retaining only the first namespace.
        with closing(sqlite3.connect(self.base / "index.db")) as conn, conn:
            conn.execute("ALTER TABLE spans RENAME TO retained")
            ddl = _SPANS_TABLE.split("CREATE INDEX IF NOT EXISTS idx_spans_span")[0]
            ddl = ddl[:ddl.index("-- SQLite composite")]
            ddl = ddl.replace("PRIMARY KEY (adapter_id, session_id, trace_id, span_id)",
                              "PRIMARY KEY (trace_id, span_id)")
            conn.execute(ddl)
            conn.execute("INSERT INTO spans SELECT * FROM retained WHERE adapter_id='a'")
            conn.execute("DROP TABLE retained")
            conn.execute("UPDATE sessions SET model='wrong', cwd='wrong', ended=1")

    def test_old_key_migration_repairs_collapsed_roots_and_preserves_archive(self):
        for agent in ("a", "b"):
            self.store.ingest(span(agent))
        self.store.refresh_projections()
        archived = [tuple(r) for r in self.store.query("SELECT * FROM events ORDER BY event_id")]
        self.store.note_file("untouched.jsonl", 10, 1, 10)
        self.store.close()
        self.use_legacy_projection()
        self.store = TraceStore(self.base / "index.db")
        self.assertEqual(archived, [tuple(r) for r in self.store.query("SELECT * FROM events ORDER BY event_id")])
        self.assertEqual(self.store.file_state("untouched.jsonl")["indexed_bytes"], 10)
        for agent in ("a", "b"):
            self.assertEqual(query.get_span(self.store, "shared", "child", agent=agent)["input"], {"owner": [agent, "same"]})
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM spans")[0], 4)
        self.assertIsNone(self.store.one("SELECT 1 FROM meta WHERE key='namespace_rebuild'"))
        sessions = query.list_sessions(self.store)["sessions"]
        self.assertTrue(all(not r["ended"] and r["model"] == "model-" + r["adapter_id"] for r in sessions))
        self.store.close()
        self.store = TraceStore(self.base / "index.db")
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM spans")[0], 4)


if __name__ == "__main__":
    unittest.main()
