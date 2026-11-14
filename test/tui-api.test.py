#!/usr/bin/env python3
"""Isolated TUI query + real authenticated HTTP contracts (stdlib only).

Run: python3 test/tui-api.test.py
All state lives in a disposable SQLite DB; no indexer/service is started.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import query  # noqa: E402
from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402
from server.main import make_server  # noqa: E402

PAYLOADS = ("input", "output", "error", "attributes", "privacy", "source", "resource")
TS = "2026-11-12T00:00:00.000Z"


class TuiAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.store = TraceStore(Path(self.tmp.name) / "test.db")
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.thread.join(timeout=5)
            self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def session(self, sid="s", **kw):
        values = dict(session_id=sid, adapter_id="agent", first_ts=TS,
                      last_ts=TS, model=None, cwd=None)
        values.update(kw)
        self.insert("sessions", values)

    def span(self, span_id="p", trace_id="t", session_id="s", **kw):
        values = dict(span_id=span_id, trace_id=trace_id, session_id=session_id,
                      adapter_id="agent", kind="llm", name=span_id,
                      status="ok", ended=1, synthetic=0, start_ts=TS,
                      end_ts="2026-11-12T00:00:01.000Z", duration_ms=100)
        values.update(kw)
        self.insert("spans", values)

    def insert(self, table, values):
        conn = self.store._conn()
        conn.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES"
                     f" ({','.join('?' for _ in values)})", tuple(values.values()))
        conn.commit()

    def get(self, path):
        if not self.server:
            self.server = make_server(self.store, Indexer(self.store, []),
                                      host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            conn.request("GET", path, headers={"Authorization": "Bearer test-token"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_pagination_stable_ties_and_bounds(self):
        for sid in ("c", "a", "b"):
            self.session(sid)
        page = query.list_sessions(self.store, limit=1, offset=1)
        self.assertEqual(set(page), {"sessions", "total", "limit", "offset", "has_more"})
        self.assertEqual((page["total"], page["limit"], page["offset"], page["has_more"]),
                         (3, 1, 1, True))
        self.assertEqual(page["sessions"][0]["session_id"], "b")
        self.assertFalse(query.list_sessions(self.store, offset=3)["has_more"])
        bounded = query.list_sessions(self.store, limit=99999, offset=-100)
        self.assertEqual((bounded["limit"], bounded["offset"]), (200, 0))
        self.assertEqual(query.list_sessions(self.store, limit=-1)["sessions"], [])
        self.assertEqual(query.list_sessions(self.store, limit=0)["limit"], 0)

    def test_literal_search_and_exact_filters(self):
        self.session("id_%", model="model_%", cwd=r"/tmp/a\_b", adapter_id="agent_%")
        self.session("idXX", model="modelXX", cwd="/tmp/axb", adapter_id="agentXX")
        self.session("older", last_ts="2025-01-01", model="model_%", adapter_id="agent_%")
        for search in ("id_%", "model_%", r"a\_b", "agent_%", "%", "_", "' OR 1=1 --"):
            with self.subTest(search=search):
                result = query.list_sessions(self.store, search=search)
                expected = [] if search == "' OR 1=1 --" else (
                    ["id_%", "older"] if search in ("model_%", "agent_%", "%", "_") else ["id_%"])
                self.assertEqual([s["session_id"] for s in result["sessions"]], expected)
                self.assertEqual(result["total"], len(expected))
        result = query.list_sessions(self.store, search="%", agent="agent_%",
                                     model="model_%", since=TS)
        self.assertEqual(result["total"], 1)
        self.assertEqual(query.list_sessions(self.store, agent="agent")["total"], 0)
        self.assertEqual(query.list_sessions(self.store, model="model")["total"], 0)

    def test_http_list_parameters(self):
        self.session("a_%", model="m", cwd="/w")
        self.session("b_%", model="m", cwd="/w")
        self.session("other", model="m")
        status, result = self.get("/api/sessions?" + urlencode(dict(
            search="_%", agent="agent", model="m", since=TS, limit=1, offset=1)))
        self.assertEqual(status, 200)
        self.assertEqual((result["total"], result["offset"], result["has_more"]), (2, 1, False))
        self.assertEqual(result["sessions"][0]["session_id"], "b_%")
        _, result = self.get("/api/sessions?limit=99999&offset=-5")
        self.assertEqual((result["limit"], result["offset"]), (200, 0))
        _, result = self.get("/api/sessions?limit=bad&offset=bad")
        self.assertEqual((result["limit"], result["offset"]), (100, 0))

    def test_metrics_scope_counters_latency_and_tokens(self):
        self.session()
        for i, duration in enumerate((10, 20, 30, 40, 100)):
            self.span(str(i), duration_ms=duration, input_tokens=2, output_tokens=0)
        self.span("tool", kind="tool", duration_ms=60, status="error")
        self.span("open", kind="tool", ended=0, status="running", duration_ms=99999)
        self.span("synthetic", kind="task", synthetic=1, status="interrupted", ended=0,
                  duration_ms=999999)
        self.span("negative", duration_ms=-10)
        self.span("zero", duration_ms=0)
        self.span("unknown", duration_ms=None)
        self.span("infinite", duration_ms=float("inf"))
        self.span("other", trace_id="other", session_id="other", input_tokens=9000,
                  status="error", ended=0)
        metrics = query.session_metrics(self.store, "s")
        assert metrics is not None
        self.assertEqual((metrics["traces"], metrics["spans"], metrics["errors"], metrics["unclosed"]),
                         (1, 12, 1, 1))
        self.assertEqual(metrics["tokens"], {"input": 10, "output": 0, "cache_read": None})
        self.assertEqual(metrics["llm"], {"n": 5, "p50_ms": 30, "p95_ms": 100, "max_ms": 100})
        self.assertEqual(metrics["tools"], {"n": 1, "p50_ms": 60, "p95_ms": None, "max_ms": 60})
        self.assertEqual([s["duration_ms"] for s in metrics["slowest"]], [100, 60, 40, 30, 20, 10])
        self.assertEqual(metrics["recent_traces"][0]["status"], "error")
        self.assertEqual(metrics["recent_traces"][0]["unclosed"], 1)
        for span in metrics["slowest"]:
            self.assertEqual((span["trace_id"], span["session_id"]), ("t", "s"))
            self.assertFalse(set(span) & set(PAYLOADS))

    def test_metrics_missing_empty_and_small_samples(self):
        self.session("empty")
        empty = query.session_metrics(self.store, "empty")
        assert empty is not None
        self.assertEqual(empty["tokens"], {"input": None, "output": None, "cache_read": None})
        self.assertEqual(empty["llm"], {"n": 0, "p50_ms": None, "p95_ms": None, "max_ms": None})
        self.assertEqual((empty["traces"], empty["spans"], empty["errors"], empty["unclosed"]), (0, 0, 0, 0))
        self.assertEqual(empty["slowest"], [])
        self.assertEqual(empty["recent_traces"], [])
        self.assertIsNone(query.session_metrics(self.store, "missing"))
        self.assertIsNone(query.list_sessions(self.store)["sessions"][0]["tokens"]["input"])
        for i in range(4):
            self.span(str(i), duration_ms=i + 1)
        metrics = query.session_metrics(self.store, "s")
        assert metrics is not None
        self.assertIsNone(metrics["llm"]["p95_ms"])
        self.assertIsNone(metrics["recent_traces"][0]["tokens"]["input"])

    def test_metrics_recent_limit_order_shape_and_root_collision(self):
        for i in range(30):
            self.span(str(i), trace_id=f"t{i:02}", start_ts=f"2026-11-12T00:00:{i:02}.000Z")
        # A root-shaped span with a reused span_id in a different trace must not multiply joins.
        self.span("t29", trace_id="foreign", session_id="other", kind="task")
        result = query.session_metrics(self.store, "s")
        assert result is not None
        self.assertEqual(len(result["slowest"]), 10)
        self.assertEqual(len(result["recent_traces"]), 24)
        self.assertEqual([t["trace_id"] for t in result["recent_traces"]],
                         [f"t{i:02}" for i in range(29, 5, -1)])
        self.assertEqual(set(result["recent_traces"][0]),
                         set(query.list_traces(self.store)["traces"][0]))
        self.assertTrue(all(t["span_count"] == 1 for t in result["recent_traces"]))

    def test_summary_sql_projection_no_payload_access_or_decoding(self):
        self.span("root", kind="task", **{p + "_json": '{"large":"secret"}' for p in PAYLOADS})
        self.span("child", parent_span_id="root", ended=0)
        reads = []
        def authorizer(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
                if table == "events" or column.endswith("_json"):
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn = self.store._conn()
        conn.set_authorizer(authorizer)
        try:
            with patch.object(query, "_load", side_effect=AssertionError("payload decoded")), \
                 patch.object(self.store, "iter_events", side_effect=AssertionError("events read")):
                result = query.get_trace(self.store, "t", summary=True)
        finally:
            conn.set_authorizer(None)
        assert result is not None
        self.assertTrue(reads)
        self.assertEqual(set(result), {"trace_id", "session_id", "adapter_id", "spans"})
        self.assertEqual(result["spans"][1]["parent_span_id"], "root")
        self.assertEqual(result["spans"][1]["status"], "incomplete")
        self.assertTrue(all(not (set(s) & set(PAYLOADS)) for s in result["spans"]))
        self.assertIsNone(query.get_trace(self.store, "missing", summary=True))

    def test_full_trace_and_single_span_payloads(self):
        self.span(**{p + "_json": json.dumps({p: "value"}) for p in PAYLOADS})
        full = query.get_trace(self.store, "t")
        single = query.get_span(self.store, "t", "p")
        assert full is not None and single is not None
        self.assertEqual(single, {**full["spans"][0], "trace_id": "t", "session_id": "s", "adapter_id": "agent"})
        for p in PAYLOADS:
            self.assertEqual(single[p], {p: "value"})
        self.assertIsNone(query.get_span(self.store, "wrong", "p"))
        self.assertIsNone(query.get_span(self.store, "t", "missing"))

    def test_http_encoded_ids_once_and_wrong_trace(self):
        sid, tid, pid = "session/metrics %2F?中", "trace/spans/%2F?中", "span/%2F?中"
        self.session(sid)
        self.span(pid, tid, sid, input_json='{"hello":"world"}')
        base = "/api/traces/" + quote(tid, safe="")
        status, summary = self.get(base + "?summary=1")
        self.assertEqual(status, 200)
        self.assertEqual((summary["trace_id"], summary["session_id"]), (tid, sid))
        self.assertNotIn("input", summary["spans"][0])
        status, full = self.get(base)
        self.assertEqual(status, 200)
        self.assertEqual(full["spans"][0]["input"], {"hello": "world"})
        status, single = self.get(base + "/spans/" + quote(pid, safe=""))
        self.assertEqual(status, 200)
        self.assertEqual((single["trace_id"], single["session_id"], single["span_id"]), (tid, sid, pid))
        self.assertEqual(self.get("/api/traces/wrong/spans/" + quote(pid, safe=""))[0], 404)
        self.assertEqual(self.get(base + "/spans/missing")[0], 404)
        self.assertEqual(self.get("/api/traces/missing?summary=1")[0], 404)
        status, metrics = self.get("/api/sessions/" + quote(sid, safe="") + "/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(metrics["session_id"], sid)
        self.assertEqual(self.get("/api/sessions/missing/metrics")[0], 404)
        # Never fall back to decoding twice, even when that would find a record.
        self.session("real/path")
        self.span("real/path", "real/trace", "real/path", kind="task")
        self.assertEqual(self.get("/api/sessions/real%252Fpath/metrics")[0], 404)
        self.assertEqual(self.get("/api/sessions/real%252Fpath")[0], 404)
        self.assertEqual(self.get("/api/traces/real%252Ftrace?summary=1")[0], 404)
        self.assertEqual(self.get("/api/traces/real%2Ftrace/spans/real%252Fpath")[0], 404)
        self.assertEqual(self.get("/api/sessions/real%2Fpath")[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
