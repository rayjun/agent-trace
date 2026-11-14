#!/usr/bin/env python3
"""Isolated namespace/ISO contracts: python3 test/session-identity.test.py.

Only disposable SQLite databases and an ephemeral loopback HTTP listener; no
indexer scan, profile files, token generation, or running service are touched.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import query
from core.storage.indexer import Indexer
from core.storage.sqlite_store import TraceStore
from server.main import Handler, make_server

TS = "2026-11-12T00:00:00.000Z"


class ClosingHandler(Handler):
    def finish(self):
        try:
            super().finish()
        finally:
            self.state.store.close()  # Disposable request thread connection.


class SessionIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.store = TraceStore(Path(self.tmp.name) / "isolated.db")
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.thread.join(5)
            self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def insert(self, table, **values):
        conn = self.store._conn()
        conn.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES"
                     f" ({','.join('?' for _ in values)})", tuple(values.values()))
        conn.commit()

    def session(self, sid="same", agent="a", ts=TS, model="model_%"):
        self.insert("sessions", session_id=sid, adapter_id=agent,
                    first_ts=ts, last_ts=ts, model=model, cwd="/workspace")

    def span(self, sid="same", agent="a", trace="t", span="p", **kw):
        values = dict(session_id=sid, adapter_id=agent, trace_id=trace,
                      span_id=span, kind="llm", name=f"{agent}-{span}",
                      start_ts=TS, end_ts=TS, duration_ms=10, ended=1,
                      synthetic=0, status="ok", input_tokens=3,
                      output_tokens=2, cache_read_tokens=1, model="model_%")
        values.update(kw)
        self.insert("spans", **values)

    def populate(self, sid="same"):
        for agent, n in (("a", 3), ("b", 100)):
            self.session(sid, agent)
            self.span(sid, agent, trace=agent, span=agent,
                      kind="task", synthetic=1, input_tokens=None,
                      output_tokens=None, cache_read_tokens=None)
            self.span(sid, agent, trace=agent, span=agent + "-child",
                      input_tokens=n, duration_ms=n,
                      status="error" if agent == "b" else "ok",
                      ended=0 if agent == "b" else 1)

    def get(self, path):
        if self.server is None:
            self.server = make_server(self.store, Indexer(self.store, []),
                                      host="127.0.0.1", port=0, token="test-token")
            self.server.RequestHandlerClass = ClosingHandler
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            conn.request("GET", path, headers={"Authorization": "Bearer test-token"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_list_correlated_totals_and_exact_filters(self):
        self.populate()
        self.span(agent="b", trace="b2", span="extra", input_tokens=7)
        rows = {r["adapter_id"]: r for r in query.list_sessions(self.store)["sessions"]}
        self.assertEqual(rows["a"]["tokens"], {"input": 3, "output": 2})
        self.assertEqual(rows["a"]["trace_count"], 1)
        self.assertEqual(rows["b"]["tokens"]["input"], 107)
        self.assertEqual(rows["b"]["trace_count"], 2)
        for agent in ("a", "b", "a%", "' OR 1=1 --", ""):
            result = query.list_sessions(self.store, agent=agent, model="model_%")
            self.assertEqual(result["total"], int(agent in ("a", "b")))
        self.assertEqual(query.list_sessions(self.store, model="model")["total"], 0)
        status, result = self.get("/api/sessions?" + urlencode(dict(agent="a", model="model_%")))
        self.assertEqual(status, 200)
        self.assertEqual(result["sessions"], [rows["a"]])

    def test_resolution_http_and_sql(self):
        self.populate()
        for fn in (query.session_detail, query.session_metrics):
            with self.assertRaises(query.AmbiguousSessionError) as raised:
                fn(self.store, "same")
            self.assertIsInstance(raised.exception, ValueError)
            self.assertIn("--agent", str(raised.exception))
            self.assertEqual(raised.exception.adapters, ["a", "b"])
            for selected in ("unknown", "a%", "' OR 1=1 --", ""):
                self.assertIsNone(fn(self.store, "same", agent=selected))
            self.assertIsNone(fn(self.store, "absent"))
        for suffix in ("", "/metrics"):
            path = "/api/sessions/same" + suffix
            status, result = self.get(path)
            self.assertEqual(status, 409)
            self.assertIn("choose agent", result["error"])
            self.assertEqual(result["adapters"], ["a", "b"])
            for selected in ("unknown", "a%", ""):
                self.assertEqual(self.get(path + "?" + urlencode(dict(agent=selected)))[0], 404)
            self.assertEqual(self.get("/api/sessions/absent" + suffix)[0], 404)
            for selected in ("a", "b"):
                status, result = self.get(path + "?agent=" + selected)
                self.assertEqual(status, 200)
                self.assertEqual(result["adapter_id"], selected)
                self.assertEqual(result["tokens"]["input"], 3 if selected == "a" else 100)
        metrics = query.session_metrics(self.store, "same", agent="a")
        assert metrics is not None
        self.assertEqual((metrics["spans"], metrics["traces"], metrics["errors"], metrics["unclosed"]),
                         (2, 1, 0, 0))
        self.assertEqual(metrics["llm"]["max_ms"], 3)
        self.assertEqual([r["name"] for r in metrics["slowest"]], ["a-a-child"])
        self.assertEqual([r["adapter_id"] for r in metrics["recent_traces"]], ["a"])
        detail = query.session_detail(self.store, "same", agent="a")
        assert detail is not None
        self.assertEqual((detail["turn_count"], detail["turns"][0]["spans"], detail["errors"]), (1, 1, 0))

    def test_unique_backcompat_and_literal_once_decode(self):
        for i, sid in enumerate(("Unicode雪/part%2F/metrics", "Unicode雪/part/metrics", "  raw % /  ")):
            self.session(sid)
            self.span(sid, trace=str(i), span=str(i), kind="task", synthetic=1)
            for suffix, fn in (("", query.session_detail), ("/metrics", query.session_metrics)):
                status, result = self.get("/api/sessions/" + quote(sid, safe="") + suffix)
                self.assertEqual(status, 200)
                self.assertEqual(result["session_id"], sid)
                self.assertEqual(result["adapter_id"], "a")
                direct = fn(self.store, sid)
                assert direct is not None
                self.assertEqual(direct["adapter_id"], "a")

    def test_literal_adapter_once_decode(self):
        for i, agent in enumerate(("适配器%2F/raw", "适配器//raw")):
            self.session(agent=agent)
            self.span(agent=agent, trace=str(i), span=str(i), kind="task")
        for agent in ("适配器%2F/raw", "适配器//raw"):
            for suffix in ("", "/metrics"):
                status, result = self.get("/api/sessions/same" + suffix + "?" +
                                          urlencode(dict(agent=agent)))
                self.assertEqual(status, 200)
                self.assertEqual(result["adapter_id"], agent)

    def test_trace_collisions_stay_in_namespace(self):
        # Current schema permits shared trace IDs with distinct span IDs. This
        # catches both unscoped child reads and root JOIN multiplication/leaks.
        self.session()
        self.session(agent="b")
        self.session("other", "a")
        self.span(trace="shared", span="shared", kind="task", synthetic=1,
                  input_tokens=None, output_tokens=None, cache_read_tokens=None)
        self.span(trace="shared", span="own", input_tokens=5)
        self.span(agent="b", trace="shared", span="foreign", input_tokens=900,
                  duration_ms=900, status="error")
        self.span(sid="other", trace="shared", span="other-session", input_tokens=800)
        # A foreign root matches trace/span IDs unless the join is namespaced.
        self.span(agent="b", trace="collision", span="collision", kind="task", name="foreign-root")
        self.span(trace="collision", span="own-child", input_tokens=7)
        detail = query.session_detail(self.store, "same", agent="a")
        assert detail is not None
        self.assertEqual(detail["tokens"]["input"], 5)
        self.assertEqual(detail["turns"][0]["spans"], 1)
        metrics = query.session_metrics(self.store, "same", agent="a")
        assert metrics is not None
        self.assertEqual(metrics["tokens"]["input"], 12)
        self.assertEqual(metrics["errors"], 0)
        self.assertEqual(metrics["spans"], 3)
        recent = {r["trace_id"]: r for r in metrics["recent_traces"]}
        self.assertEqual(recent["shared"]["tokens"]["input"], 5)
        self.assertEqual(recent["collision"]["name"], "trace")
        self.assertEqual(recent["collision"]["span_count"], 1)

    def test_since_equivalent_instants_inclusive_sql_and_http(self):
        timestamps = (TS, "2026-11-12T08:00:00+08:00", "2026-11-12T00:00:00Z",
                      "2026-11-12", "2026-11-11T19:00:00-05:00")
        for i, ts in enumerate(timestamps):
            self.session(str(i), ts=ts)
            self.span(str(i), trace=str(i), span=str(i), start_ts=ts, end_ts=ts)
        self.session("older", ts="2026-11-11T23:59:59.999999Z")
        self.span("older", trace="older", span="older", start_ts="2026-11-11T23:59:59.999999Z")
        for since in timestamps:
            for route, fn in (("sessions", query.list_sessions), ("traces", query.list_traces)):
                with self.subTest(since=since, route=route):
                    result = fn(self.store, since=since)
                    self.assertEqual(result["total"], len(timestamps))
                    status, http = self.get("/api/" + route + "?" + urlencode(dict(since=since)))
                    self.assertEqual(status, 200)
                    self.assertEqual(http["total"], len(timestamps))
        self.assertEqual(query.list_sessions(self.store, since="2026-11-12T00:00:00.000001Z")["total"], 0)

    def test_invalid_since_is_400(self):
        for since in ("2026-02-30", "2026-13-01", "2026-11-12T25:00:00Z", "yesterday", "",
                      "2026-11-12T00:00:00", "2026-11-12T00:00:00+08:99", "2026-11-12' OR 1=1 --"):
            for route, fn in (("sessions", query.list_sessions), ("traces", query.list_traces)):
                with self.subTest(since=since, route=route):
                    with self.assertRaises(query.InvalidSinceError):
                        fn(self.store, since=since)
                    status, result = self.get("/api/" + route + "?" + urlencode(dict(since=since)))
                    self.assertEqual(status, 400)
                    self.assertIn("UTC", result["error"])


if __name__ == "__main__":
    unittest.main()
