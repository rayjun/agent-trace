#!/usr/bin/env python3
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.storage.sqlite_store import TraceStore  # noqa: E402


class SessionCLIIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.db = Path(self.tmp.name) / 'test.db'
        self.store = TraceStore(self.db)
        for agent, amount in [('hermes', 10), ('codex', 100)]:
            self.add_session('shared', agent, amount)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_session(self, sid, agent, tokens):
        conn = self.store._conn()
        ts = '2026-11-14T00:00:00.000Z'
        end = '2026-11-14T00:00:01.000Z'
        conn.execute('INSERT INTO sessions(session_id,adapter_id,first_ts,last_ts) VALUES(?,?,?,?)',
                     (sid, agent, ts, end))
        trace = agent + ':' + sid
        for span, kind, synthetic, count in [(trace, 'task', 1, None), ('call', 'llm', 0, tokens)]:
            conn.execute('INSERT INTO spans(trace_id,span_id,session_id,adapter_id,kind,name,status,ended,synthetic,start_ts,end_ts,duration_ms,input_tokens,output_tokens) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (trace, span, sid, agent, kind, kind, 'ok', 1, synthetic, ts, end, 1000, count, 1 if count is not None else None))
        conn.commit()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'cli/agenttrace.py'), 'session', *args, '--db', str(self.db)],
                              cwd=ROOT, env={**os.environ, 'AGENTTRACE_PLATFORM_DIR': self.tmp.name},
                              capture_output=True, text=True, timeout=20)

    def test_exact_session_respects_agent(self):
        for agent, tokens in [('hermes', 10), ('codex', 100)]:
            result = self.run_cli('shared', '--agent', agent)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('adapter   ' + agent, result.stdout)
            self.assertIn(f'tokens    in {tokens}  out 1', result.stdout)
            self.assertIn('turns     1', result.stdout)

    def test_ambiguous_session_is_readable_failure(self):
        result = self.run_cli('shared')
        self.assertEqual(result.returncode, 1)
        self.assertIn('agent', result.stderr.lower())
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('tokens', result.stdout)

    def test_suffix_resolution_respects_namespace(self):
        self.add_session('prefix-tail', 'hermes', 7)
        self.add_session('other-tail', 'codex', 70)
        result = self.run_cli('tail', '--agent', 'hermes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('session   prefix-tail', result.stdout)
        self.assertIn('tokens    in 7  out 1', result.stdout)

    def test_missing_agent_namespace_does_not_fall_back(self):
        self.add_session('hermes-only', 'hermes', 7)
        result = self.run_cli('hermes-only', '--agent', 'codex')
        self.assertEqual(result.returncode, 1)
        self.assertIn('no session matching', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
