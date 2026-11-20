#!/usr/bin/env python3
"""Work review acceptance: honest evidence, isolation, bounded reads and CLI gates."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "cli"), str(ROOT / "common")]
from core.review import BODY_LIMIT, command_of, inbox, is_check, markdown, session_review  # noqa: E402
from core.schema.model import CanonicalEvent  # noqa: E402
from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402
from server.main import make_server  # noqa: E402


class WorkReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = TraceStore(self.base / "work.db")
        self.addCleanup(self.store.close)
        self.n = 0

    def add(self, name="exec_command", cmd="pytest -q", output="2 passed", code=0,
            status="ok", agent="codex", session="work", trace="turn", ended=True):
        self.n += 1
        sid = f"call-{self.n}"
        span = dict(kind="tool", name=name, status=status,
                    input={"args": {"cmd": cmd}},
                    output={"result": output} if output is not None else None,
                    attributes={"exit_code": code} if code is not None else {})
        if name == "write_stdin":
            span["input"] = {"args": {"session_id": cmd}}
        event = CanonicalEvent(type="span_end" if ended else "span_start", adapter_id=agent,
                               session_id=session, trace_id=trace, span_id=sid,
                               ts=f"2026-11-19T10:{self.n:02}:00.000Z", span=span).to_dict()
        self.assertTrue(self.store.ingest(event), self.store.invalid)
        self.store.refresh_projections()
        return sid

    def review(self, **kwargs):
        return session_review(self.store, "work", agent="codex", **kwargs)

    def test_checks_are_not_the_agent_claim_or_the_last_command_only(self):
        failed = self.add(cmd="pytest tests/unit", output="1 failed", code=1)
        self.add(cmd="npm run lint", output="All checks passed", code=0)
        review = self.review()
        self.assertEqual(review["state"], "failed")
        self.assertEqual(review["checks"][0]["span_id"], failed)
        self.assertEqual(len(review["checks"]), 2)
        self.assertIn("current worktree", review["scope"])

    def test_successful_exact_rerun_supersedes_failure_but_different_check_does_not(self):
        self.add(output="1 failed", code=1)
        last = self.add(output="2 passed", code=0)
        review = self.review()
        self.assertEqual(review["state"], "passed")
        self.assertEqual(review["checks"][0]["span_id"], last)
        self.assertEqual(len(review["failures"]), 0)  # process exit is not the wrapper tool status

    def test_missing_output_and_unknown_exit_never_become_a_pass(self):
        self.add(output=None, code=None)
        self.assertEqual(self.review()["state"], "unknown")

    def test_informational_shell_lists_and_stdout_only_are_not_success_evidence(self):
        self.add(cmd="pytest --version", output="pytest 8.4.0")
        self.assertEqual(self.review()["state"], "unverified")
        self.add(cmd="pytest -q; echo finished", output="finished")
        self.assertEqual(self.review()["state"], "unknown")
        self.add(cmd="pytest -q", output="2 passed", code=None, trace="fresh")
        self.assertEqual(self.review()["state"], "unknown")
        self.add(cmd="pytest -q", output="2 passed\nProcess exited with code 0", code=None, trace="fresh")
        self.assertEqual(self.review()["state"], "unknown")
        self.add(cmd="pytest -q",
                 output="Chunk ID: test\nWall time: 0.2 seconds\nProcess exited with code 0\nFinal output:\n2 passed",
                 code=None, trace="fresh")
        self.assertEqual(self.review()["state"], "passed")

    def test_open_turn_and_check_are_not_approval_or_proof_of_a_live_agent(self):
        self.add(ended=False, status="running", output=None, code=None)
        review = self.review()
        self.assertEqual(review["state"], "open")
        self.assertIn("does not prove", review["next_action"])

    def test_latest_turn_does_not_inherit_old_pass_or_other_agent(self):
        self.add(trace="old")
        self.add(name="read_file", cmd="file.txt", code=None, trace="new")
        self.assertEqual(self.review()["state"], "unverified")
        self.add(agent="hermes", code=1, output="FAILED")
        self.assertEqual(self.review()["state"], "unverified")
        with self.assertRaises(ValueError):
            session_review(self.store, "work")
        page = inbox(self.store, agent="hermes")
        self.assertEqual(page["reviews"][0]["state"], "failed")
        self.assertEqual(page["total"], 1)

    def test_async_process_poll_is_correlated_with_original_check(self):
        self.add(output={"session_id": 91, "output": "starting"}, code=None)
        # The wrapper closed, but the child's completion was not captured.
        # This does not establish that the native turn is still open.
        self.assertEqual(self.review()["state"], "unknown")
        sid = self.add(name="write_stdin", cmd=91,
                       output={"exit_code": 1, "output": "assertion failed"}, code=None)
        review = self.review()
        self.assertEqual(review["state"], "failed")
        self.assertEqual(review["checks"][0]["span_id"], sid)
        self.assertEqual(review["checks"][0]["command"], "pytest -q")

    def test_exec_wrapper_poll_and_nested_stdout_do_not_fake_a_running_process(self):
        self.add(output={"session_id": 91, "output": "starting"}, code=None)
        event = CanonicalEvent(type="span_end", adapter_id="codex", session_id="work", trace_id="turn",
            span_id="poll", ts="2026-11-19T10:03:00.000Z", span={"kind": "tool", "name": "exec", "status": "ok",
            "input": {"args": 'text(await tools.write_stdin({session_id:91, chars:""}));'},
            "output": {"result": 'Script completed\nWall time: 1 second\nOutput:\n'
                + json.dumps({"exit_code": 0, "output": '2 passed; printed JSON {"session_id":123}'})}}).to_dict()
        self.assertTrue(self.store.ingest(event))
        self.store.refresh_projections()
        self.assertEqual(self.review()["state"], "passed")
        self.assertEqual(self.review()["checks"][0]["span_id"], "poll")

    def test_contradictory_success_exit_and_failure_summary_is_unknown(self):
        self.add(output="FAILED test_something", code=0)
        self.assertEqual(self.review()["state"], "unknown")

    def test_cancelled_or_interrupted_checks_do_not_pass_from_old_output(self):
        for status in ("cancelled", "interrupted"):
            self.add(status=status, code=0, output="2 passed")
            self.assertEqual(self.review()["state"], "failed")

    def test_wrapper_excerpt_shows_failed_output_instead_of_later_success(self):
        self.add(code=None, output='Script completed\nWall time: 1 second\nOutput:\n'
                 + json.dumps({"exit_code": 1, "output": "cache assertion failed"})
                 + json.dumps({"exit_code": 0, "output": "lint passed"}))
        review = self.review()
        self.assertEqual(review["state"], "failed")
        self.assertEqual(review["checks"][0]["output_excerpt"], "cache assertion failed")
        self.add(code=None, output='Script completed\nWall time: 1 second\nOutput:\n'
                 + json.dumps([{"exit_code": 1, "output": "cache assertion failed"},
                               {"exit_code": 0, "output": "lint passed"}]))
        self.assertEqual(self.review()["state"], "failed")
        self.add(code=None, output="Script completed\nWall time: 1 second\nOutput:\n2 passed")
        self.assertEqual(self.review()["state"], "unknown")

    def test_call_limit_discloses_omissions_and_prevents_a_pass(self):
        self.add()
        self.add(cmd="npm run lint")
        self.add(cmd="cargo test", code=1, output="1 failed")
        with patch("core.review.CALL_LIMIT", 2):
            review = self.review()
        self.assertTrue(review["limited"])
        self.assertEqual(review["state"], "unknown")

    def test_tool_failure_has_a_source_even_without_check_invocations(self):
        sid = self.add(name="read_file", cmd="missing.py", code=None, output=None, status="error")
        review = self.review()
        self.assertEqual(review["state"], "unverified")
        self.assertEqual(review["failures"][0]["span_id"], sid)
        self.assertEqual(review["failures"][0]["trace_id"], "turn")
        self.assertIn("may have been recovered", review["next_action"])

    def test_review_reads_one_snapshot_while_another_store_publishes(self):
        self.add()
        original = self.store.one
        published = False
        def one(sql, params=()):
            nonlocal published
            if "SELECT * FROM sessions" in sql and not published:
                published = True
                writer = TraceStore(self.store.path)
                try:
                    event = CanonicalEvent(type="span_end", adapter_id="codex", session_id="work",
                        trace_id="turn", span_id="concurrent", ts="2026-11-19T10:50:00.000Z",
                        span={"kind": "tool", "name": "exec_command", "status": "ok",
                              "input": {"args": {"cmd": "pytest -q"}},
                              "output": {"result": "1 failed"}, "attributes": {"exit_code": 1}}).to_dict()
                    self.assertTrue(writer.ingest(event))
                    writer.refresh_projections()
                finally:
                    writer.close()
            return original(sql, params)
        with patch.object(self.store, "one", side_effect=one):
            self.assertEqual(self.review()["state"], "passed")
        self.assertTrue(published)
        self.assertEqual(self.review()["state"], "failed")

    def test_truncated_capture_and_masked_exit_do_not_greenlight(self):
        self.add(output="2 passed " + "x" * BODY_LIMIT)
        self.assertEqual(self.review()["state"], "unknown")
        self.assertTrue(self.review()["limited"])
        self.add(cmd="pytest || true", output="", code=0, trace="second")
        self.assertEqual(self.review()["state"], "unknown")

    def test_truncated_exit_metadata_does_not_become_a_pass_from_stdout(self):
        event = CanonicalEvent(type="span_end", adapter_id="codex", session_id="work",
            trace_id="turn", span_id="large-metadata", ts="2026-11-19T10:00:00.000Z",
            span={"kind": "tool", "name": "exec_command", "status": "ok",
                  "input": {"args": {"cmd": "pytest -q"}}, "output": {"result": "2 passed"},
                  "attributes": {"detail": "x" * BODY_LIMIT, "exit_code": 1}}).to_dict()
        self.assertTrue(self.store.ingest(event))
        self.store.refresh_projections()
        review = self.review()
        self.assertTrue(review["limited"])
        self.assertEqual(review["state"], "unknown")

    def test_recognition_is_tool_and_command_aware(self):
        for cmd in ["pytest -q", "python3 -m unittest", ".venv/bin/python test/work-review.test.py",
                    "SKIP_E2E=1 bash test/run-all.sh", "npm run typecheck", "go test ./...",
                    "cargo check", "node --test test/app.test.mjs", "cd repo\npytest -q"]:
            self.assertTrue(is_check(cmd), cmd)
        for cmd in ["echo 'pytest failed'", "grep pytest README.md", "python -c 'print(\"pytest\")'",
                    "cat test/work-review.test.py", "git show HEAD:test/app.py"]:
            self.assertFalse(is_check(cmd), cmd)
        self.assertFalse(is_check("cat <<'EOF'\npytest -q\nEOF"))
        self.assertTrue(is_check("cat <<'EOF'\nexample\nEOF\npytest -q"))
        wrapper = "text(await tools.exec_command({cmd:" + json.dumps("echo 'fixture cmd: \"pytest\"'") + "}));"
        self.assertEqual(command_of("exec", {"args": wrapper}), "echo 'fixture cmd: \"pytest\"'")
        self.assertFalse(is_check(command_of("exec", {"args": wrapper})))
        self.assertIsNone(command_of("exec", {"args": 'if (false) { text(await tools.exec_command({cmd:"pytest"})); }'}))
        self.assertIsNone(command_of("exec", {"args": 'const unused = () => tools.exec_command({cmd:"pytest"});'}))
        self.assertIsNone(command_of("read_file", {"args": {"cmd": "pytest"}}))
        self.assertEqual(command_of("functions.exec", {"args": {"code": 'text(await tools.exec_command({cmd:"pytest -q"}));'}}), "pytest -q")

    def test_empty_metadata_session_is_not_a_pass(self):
        self.store.ingest(CanonicalEvent(type="event", adapter_id="codex", session_id="work",
                         ts="2026-11-19T10:00:00.000Z", event={"name": "session_start"}).to_dict())
        self.store.refresh_projections()
        self.assertEqual(self.review()["state"], "empty")
        self.assertIsNone(session_review(self.store, "missing"))

    def test_task_and_claim_are_labeled_separately_and_brief_has_source_ids(self):
        self.add(output="1 failed", code=1)
        for name, attrs in [("user_prompt", {"text": "Fix Unicode 中文"}),
                            ("assistant_message", {"content": "All done, tests pass!"})]:
            self.store.ingest(CanonicalEvent(type="event", adapter_id="codex", session_id="work",
                              trace_id="turn", ts="2026-11-19T10:02:00.000Z",
                              event={"name": name, "attributes": attrs}).to_dict())
        self.store.refresh_projections()
        review = self.review()
        self.assertEqual(review["task"], "Fix Unicode 中文")
        self.assertEqual(review["state"], "failed")
        brief = markdown(review)
        for text in ["codex / work", "Turn: ` turn `", "unverified self-report", "call-1", "pytest -q"]:
            self.assertIn(text, brief)

    def test_cli_gate_and_machine_readable_handoff(self):
        self.add(code=1, output="1 failed")
        env = dict(os.environ, AGENTTRACE_DB=str(self.store.path))
        cmd = [sys.executable, str(ROOT / "cli/agenttrace.py"), "review", "--session", "work", "--agent", "codex"]
        result = subprocess.run(cmd + ["--check", "--json"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "failed")
        self.add(code=0, output="2 passed")
        result = subprocess.run(cmd + ["--check", "--markdown"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("# Agent work review", result.stdout)
        self.add(trace="new", code=None, output=None)
        result = subprocess.run(cmd + ["--check"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_refresh_can_start_from_native_codex_without_hooks_or_server(self):
        home, capture = self.base / "codex", self.base / "captured"
        source = home / "sessions" / "rollout-test.jsonl"
        source.parent.mkdir(parents=True)
        def row(kind, payload, second):
            return {"type": kind, "payload": payload, "timestamp": f"2026-11-19T12:00:{second:02}.000Z"}
        rows = [row("session_meta", {"id": "native-work", "cwd": str(self.base)}, 0),
                row("event_msg", {"type": "task_started", "turn_id": "native-turn"}, 1),
                row("response_item", {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "Fix startup"}]}, 2),
                row("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "check",
                    "input": 'text(await tools.exec_command({cmd:"pytest -q"}));'}, 3),
                row("response_item", {"type": "custom_tool_call_output", "call_id": "check",
                    "output": [{"type": "text", "text": 'Script completed\nWall time: 1 second\nOutput:\n{"exit_code":0,"output":"2 passed"}'}]}, 4),
                row("event_msg", {"type": "task_complete", "turn_id": "native-turn", "last_agent_message": "Done"}, 5)]
        source.write_text("".join(json.dumps(r) + "\n" for r in rows))
        env = dict(os.environ, CODEX_HOME=str(home), AGENTTRACE_CODEX_DIR=str(capture),
                   AGENTTRACE_STATE_DIR=str(self.base / "capture-state"), AGENTTRACE_CAPTURE="full")
        cmd = [sys.executable, str(ROOT / "cli/agenttrace.py"), "review", "--refresh", "--check", "--json",
               "--session", "native-work", "--agent", "codex", "--db", str(self.base / "fresh.db"), str(capture)]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        review = json.loads(result.stdout)
        self.assertEqual(review["state"], "passed")
        self.assertEqual(review["task"], "Fix startup")

    def test_http_auth_namespaced_review_and_export(self):
        self.add(code=1, output="<script>bad()</script>")
        self.add(agent="pi", code=0)
        server = make_server(self.store, Indexer(self.store, []), host="127.0.0.1", port=0, token="review-token", scan_interval=3600)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(path):
            try:
                with opener.open(f"http://127.0.0.1:{server.server_address[1]}{path}", timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.load(error)
        try:
            self.assertEqual(get("/api/review")[0], 401)
            self.assertEqual(get("/api/review?token=review-token&session=work")[0], 409)
            code, review = get("/api/review?token=review-token&session=work&agent=codex")
            self.assertEqual(code, 200)
            self.assertEqual(review["state"], "failed")
            self.assertIn("call-1", review["handoff_markdown"])
            self.assertEqual(get("/api/review?token=review-token&session=missing")[0], 404)
            self.assertEqual(get("/api/review?token=review-token&limit=1")[1]["total"], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
