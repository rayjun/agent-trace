#!/usr/bin/env python3
"""Synthetic SDK, real loopback HTTP and export namespace regressions."""
import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from unittest.mock import patch
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import query  # noqa: E402
from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402
from server.main import Handler, make_server  # noqa: E402

spec = importlib.util.spec_from_file_location('namespace_export', ROOT / 'scripts/export-span.py')
assert spec is not None and spec.loader is not None
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)
TS = '2026-11-12T00:00:00Z'
END = '2026-11-12T00:00:10Z'

class ClosingHandler(Handler):
    def finish(self):
        try:
            super().finish()
        finally:
            self.state.store.close()

class NamespaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.directory = Path(self.temp.name)
        self.store = TraceStore(self.directory / 'test.db')
        self.server = make_server(self.store, Indexer(self.store, []), host='127.0.0.1', port=0, token='synthetic-token')
        self.server.RequestHandlerClass = ClosingHandler
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def insert(self, table, **values):
        conn = self.store._conn()
        conn.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
        conn.commit()

    def span(self, agent='a', session='same', trace='a', span='own', **kw):
        values = dict(adapter_id=agent, session_id=session, trace_id=trace, span_id=span,
                      kind='llm', name=span, status='ok', start_ts=TS, end_ts=END,
                      ended=1, synthetic=0, input_tokens=3, input_json=json.dumps(agent + '-payload'))
        values.update(kw)
        self.insert('spans', **values)

    def event(self, agent, session, name, trace='a', resource=None):
        payload = dict(type='event', event_id=name, ts=TS, adapter_id=agent,
                       session_id=session, trace_id=trace, event={'name': name}, resource=resource)
        self.insert('events', event_id=name, type='event', ts=TS, adapter_id=agent,
                    session_id=session, trace_id=trace, payload=json.dumps(payload))

    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request('GET', path, headers={'Authorization': 'Bearer synthetic-token'})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_ambiguous_trace_and_foreign_span_fail_closed(self):
        self.span()
        self.span(agent='b', span='foreign', input_tokens=900)
        for path in ['/api/traces/a', '/api/traces/a?summary=1', '/api/traces/a/spans/foreign']:
            self.assertEqual(self.get(path)[0], 409)
        for fn, args in [(query.get_trace, ('a',)), (query.get_span, ('a', 'foreign'))]:
            with self.assertRaises(query.AmbiguousTraceError):
                fn(self.store, *args)
            for agent, session in [('missing', 'same'), ('a', 'missing'), ('', 'same')]:
                self.assertIsNone(fn(self.store, *args, agent=agent, session=session))
        self.assertEqual(self.get('/api/traces/a/spans/foreign?agent=a&session=same')[0], 404)
        self.assertEqual(self.get('/api/traces/a?agent=a&session=missing')[0], 404)
        for summary in (False, True):
            data = query.get_trace(self.store, 'a', agent='a', session='same', summary=summary)
            assert data is not None
            self.assertEqual((data['adapter_id'], data['session_id']), ('a', 'same'))
            self.assertEqual([s['span_id'] for s in data['spans']], ['own'])
            status, http = self.get('/api/traces/a?' + urlencode(dict(agent='a', session='same', summary=int(summary))))
            self.assertEqual(status, 200)
            self.assertEqual(http, data)
        self.span(session='other', span='other')
        self.assertEqual(self.get('/api/traces/a?agent=a')[0], 409)
        self.assertEqual(self.get('/api/traces/a?agent=a&session=other')[1]['spans'][0]['span_id'], 'other')

    def test_metadata_payload_workspace_duration_scoping(self):
        self.span()
        self.span(agent='b', span='foreign')
        self.span(agent='b', trace='different', span='nested', start_ts='2026-11-12T00:00:05Z')
        self.span(session='other', trace='different2', span='other', start_ts='2026-11-12T00:00:05Z')
        self.event('b', 'same', 'foreign-event', resource={'git': {'root': 'foreign'}})
        self.event('a', 'other', 'other-event', resource={'git': {'root': 'other'}})
        self.event('a', 'same', 'own-event', resource={'git': {'root': 'own'}})
        data = query.get_trace(self.store, 'a', agent='a', session='same')
        assert data is not None
        self.assertEqual(data['workspace'], {'git': {'root': 'own'}})
        self.assertEqual([e['name'] for e in data['events']], ['own-event'])
        self.assertEqual(data['tokens']['input'], 3)
        self.assertFalse(data['spans'][0]['duration_suspect'])
        span = query.get_span(self.store, 'a', 'own', agent='a', session='same')
        assert span is not None
        self.assertEqual(span['input'], 'a-payload')
        self.assertFalse(span['duration_suspect'])

    def test_literal_once_decode_unique_legacy_and_export(self):
        trace, span, agent, session = '雪/spans/%2F', 'p/%2F', '适配器/%2F+', 'same/%2F+'
        self.span(agent=agent, session=session, trace=trace, span=span)
        route = '/api/traces/' + quote(trace, safe='')
        selectors = urlencode(dict(agent=agent, session=session))
        for suffix in ('?summary=1&' + selectors, '?' + selectors):
            self.assertEqual(self.get(route + suffix)[1]['adapter_id'], agent)
        path = route + '/spans/' + quote(span, safe='')
        self.assertEqual(self.get(path + '?' + selectors)[1]['session_id'], session)
        self.assertEqual(self.get(path)[0], 200)
        (self.directory / 'server.token').write_text('synthetic-token')
        with patch.dict(os.environ, {'AGENTTRACE_PLATFORM_DIR': str(self.directory)}):
            for scoped in (False, True):
                output = self.directory / ('scoped.json' if scoped else 'legacy.json')
                args = ['--trace', trace, '--span', span, '--port', str(self.server.server_port), '--output', str(output)]
                if scoped:
                    args += ['--agent', agent, '--session', session]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(export.main(args), 0)
                self.assertEqual(json.loads(output.read_text())['adapter_id'], agent)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.span(agent='foreign', session=session, trace=trace, span='foreign')
            with self.assertRaises(HTTPError) as raised:
                export.fetch_span(trace, span, self.server.server_port)
            self.assertEqual(raised.exception.code, 409)
            raised.exception.close()
            self.assertEqual(export.fetch_span(trace, span, self.server.server_port, agent=agent, session=session)['span_id'], span)

    def test_export_rejects_context_and_id_mismatch(self):
        (self.directory / 'server.token').write_text('synthetic-token')
        base = dict(trace_id='a', span_id='own', adapter_id='a', session_id='same')
        for field in ('trace_id', 'span_id', 'adapter_id', 'session_id'):
            class Response(io.StringIO):
                status = 200
            response = Response(json.dumps({**base, field: 'foreign'}))
            with patch.dict(os.environ, {'AGENTTRACE_PLATFORM_DIR': str(self.directory)}), patch.object(export.urllib.request.OpenerDirector, 'open', return_value=response):
                with self.assertRaises(ValueError):
                    export.fetch_span('a', 'own', self.server.server_port, agent='a', session='same')

if __name__ == '__main__':
    unittest.main()
