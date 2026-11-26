#!/usr/bin/env python3
"""Run real Git and subprocesses to verify repository review semantics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "common")]
from core.repo_review import record_check, repository_review  # noqa: E402
from core.review_git import GitReviewError, file_diff, worktree  # noqa: E402
from core.review_tasks import active_tasks, record_task_event, save_task, worktree_task  # noqa: E402
from core.schema.model import CanonicalEvent  # noqa: E402
from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402
from server.main import make_server  # noqa: E402


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root = base / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.file = self.root / "profile.txt"
        self.file.write_text("base\n")
        self.git("add", "--", "profile.txt")
        self.git("commit", "-qm", "base")
        self.store = TraceStore(base / "archive.db")
        self.addCleanup(self.store.close)

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", self.root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args]
        )

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "cli/agenttrace.py"), *args],
                              cwd=self.root, env={**os.environ, "AGENTTRACE_DB": str(self.store.path)},
                              capture_output=True, text=True, timeout=20)

    def test_inbox_reads_active_generations_and_checkpoints_in_one_query(self):
        root, store = self.root, self.store
        main = "branch:main"
        other = "branch:other"

        def save(checkout, goal):
            save_task(store, root, checkout, goal, pane_id=None, terminal_id=None, agent=None, session_id=None)

        def event(checkout, kind, detail):
            task = worktree_task(store, root, checkout)
            assert task is not None
            return record_task_event(store, task, kind, detail)

        save(main, "Old task")
        event(main, "checkpoint", "Old checkpoint")
        generation = event(main, "archived", "")
        save(main, "First active task")
        save(other, "Other active task")
        event(other, "checkpoint", "Current checkpoint")
        event(other, "checkpoint", "Newest checkpoint")
        save("branch:inactive", "Inactive task")
        event("branch:inactive", "archived", "")
        with patch.object(store, "one", wraps=store.one) as one, \
             patch.object(store, "query", wraps=store.query) as query:
            items = active_tasks(store)
        self.assertEqual([(t.checkout, t.goal, t.checkpoint, t.generation) for t in items], [
            (other, "Other active task", "Newest checkpoint", 0),
            (main, "First active task", "", generation),
        ])
        one.assert_not_called()
        query.assert_called_once()

    def test_cli_gate_and_staged_unstaged_preview_use_current_snapshot(self):
        self.file.write_text("staged\n")
        self.git("add", "profile.txt")
        self.file.write_text("working\n")
        result = self.cli("review", "--require", "tests", "--check", "--json", "--diff", "profile.txt")
        self.assertEqual(result.returncode, 2, result.stderr)
        review = json.loads(result.stdout)
        self.assertEqual(review["state"], "missing")
        self.assertEqual(review["files"], [{"path": "profile.txt", "index": "M", "worktree": "M"}])
        self.assertIn("+staged", review["diff"]["staged"])
        self.assertIn("+working", review["diff"]["unstaged"])
        run = self.cli("check", "tests", "--", sys.executable, "-c", "print('completed')")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.cli("review", "--require", "tests", "--check").returncode, 0)
        self.assertEqual(self.cli("review", "--require", "tests", "--check", "--staged").returncode, 2)
        self.file.write_text("updated\n")
        stale = self.cli("review", "--require", "tests", "--check", "--json")
        self.assertEqual(stale.returncode, 2)
        self.assertEqual(json.loads(stale.stdout)["checks"][0]["state"], "stale")
        failed = self.cli("check", "tests", "--", sys.executable, "-c", "print('failed'); raise SystemExit(9)")
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(self.cli("review", "--require", "tests", "--check").returncode, 1)
        handoff = self.cli("review", "--require", "tests", "--markdown")
        self.assertIn("tests  failed", handoff.stdout)
        self.assertIn("profile.txt", handoff.stdout)
        self.assertEqual(self.git("show", ":profile.txt"), b"staged\n")

    def test_config_requires_all_commands_and_changed_configuration_stales_results(self):
        config = self.root / ".agenttrace-review.json"
        tests = [sys.executable, "-c", "print('tests')"]
        lint = [sys.executable, "-c", "print('lint')"]
        config.write_text(json.dumps({"checks": {"tests": tests, "lint": lint}}))
        self.assertEqual(self.cli("check", "tests").returncode, 0)
        self.assertEqual(repository_review(self.store, self.root)["state"], "missing")
        self.assertEqual(self.cli("check", "lint", "--", sys.executable, "-c", "pass").returncode, 2)
        self.assertEqual(self.cli("check", "lint").returncode, 0)
        self.assertEqual(repository_review(self.store, self.root)["state"], "passed")
        config.write_text(json.dumps({"checks": {"tests": tests, "lint": lint[:-1] + ["print('different')"]}}))
        self.assertEqual(repository_review(self.store, self.root)["state"], "stale")

    def test_process_that_changes_the_snapshot_is_unknown(self):
        script = "from pathlib import Path; Path('profile.txt').write_text('changed during validation')"
        run = record_check(self.store, self.root, "tests", [sys.executable, "-c", script])
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["state"], "unknown")
        self.assertEqual(repository_review(self.store, self.root, required=["tests"])["state"], "stale")
        self.assertIn("changed", run["detail"])
        with self.assertRaises(ValueError):
            record_check(self.store, self.root, "tests", ["pytest", "--version"])
        with self.assertRaises(ValueError):
            record_check(self.store, self.root, "tests", ["sh", "-c", "exit 0"])

    def test_invalid_policy_preserves_diff_and_blocks_the_gate(self):
        (self.root / ".agenttrace-review.json").write_text("{")
        self.file.write_text("updated\n")
        result = self.cli("review", "--check", "--json", "--diff", "profile.txt")
        self.assertEqual(result.returncode, 2)
        review = json.loads(result.stdout)
        self.assertEqual(review["state"], "unknown")
        self.assertIn("+updated", review["diff"]["unstaged"])
        self.assertIn("valid JSON", review["policy_error"])

    def test_untracked_files_ignored_outputs_and_index_only_changes(self):
        (self.root / ".gitignore").write_text("cache/\n")
        baseline = worktree(self.root).fingerprint
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "test-output").write_text("temporary")
        self.assertEqual(worktree(self.root).fingerprint, baseline)
        extra = self.root / "untracked.txt"
        extra.write_text("untracked\n")
        added = worktree(self.root).fingerprint
        self.assertNotEqual(added, baseline)
        self.assertIn("+untracked", file_diff(self.root, "untracked.txt", untracked=True)["untracked"])
        self.git("add", "untracked.txt")
        self.assertNotEqual(worktree(self.root).fingerprint, added)
        with self.assertRaises(GitReviewError):
            file_diff(self.root, "../archive.db", untracked=True)

    def test_staged_gate_compares_actual_bytes_even_when_git_status_hides_changes(self):
        self.git("update-index", "--assume-unchanged", "profile.txt")
        self.file.write_text("working copy differs\n")
        self.assertEqual(self.git("status", "--porcelain"), b"")
        self.assertFalse(worktree(self.root).staged_matches_worktree)
        run = self.cli("check", "tests", "--", sys.executable, "-c", "pass")
        self.assertEqual(run.returncode, 0, run.stderr)
        result = self.cli("review", "--require", "tests", "--check", "--staged")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("differs from the Git index", result.stderr)

    def _turn(self, sid, trace, minute, cwd=None, agent="codex"):
        if cwd is None:
            cwd = str(self.root)
        cmd = "pytest -q"
        stamp = f"2026-11-20T00:{minute:02}:00.000Z"
        span = CanonicalEvent(
            type="span_end", adapter_id=agent, session_id=sid, trace_id=trace, span_id="check-" + trace, ts=stamp,
            span={"kind": "tool", "name": "exec_command", "status": "ok",
                  "input": {"args": {"cmd": cmd}}, "output": {"result": "2 passed"},
                  "attributes": {"exit_code": 0, "cwd": cwd}},
            resource={"cwd": cwd},
        ).to_dict()
        prompt = CanonicalEvent(type="event", adapter_id=agent, session_id=sid, trace_id=trace, ts=stamp,
                                event={"name": "user_prompt", "attributes": {"text": trace}}).to_dict()
        for event in (span, prompt):
            self.assertTrue(self.store.ingest(event))
        self.store.refresh_projections()

    def test_history_scope_and_recorded_checks_survive_followup_turns(self):
        self._turn("same", "old", 0)
        self._turn("same", "start", 1)
        self._turn("same", "foreign", 2, cwd="/another/project", agent="pi")
        record_check(self.store, self.root, "tests", [sys.executable, "-c", "pass"],
                     session="same", agent="codex")
        self._turn("same", "followup", 3)
        review = repository_review(self.store, self.root, required=["tests"], agent="codex",
                                   session="same", from_turn="start")
        self.assertEqual(review["state"], "passed")
        self.assertEqual([turn["trace_id"] for turn in review["history"]["turns"]], ["followup", "start"])
        self.assertEqual(review["checks"][0]["trace_id"], "start")
        self.assertEqual([r["adapter_id"] for r in review["sessions"]], ["codex"])
        with self.assertRaises(ValueError):
            repository_review(self.store, self.root, agent="pi", session="same")
        with self.assertRaises(ValueError):
            repository_review(self.store, self.root, agent="codex", session="same", from_turn="foreign")
        self.store.clear_archive()
        self.assertEqual(repository_review(self.store, self.root, required=["tests"])["state"], "passed")

    def test_worktree_api_reads_only_the_configured_project(self):
        self.file.write_text("changed\n")
        srv = make_server(self.store, Indexer(self.store, []), host="127.0.0.1", port=0,
                          token="fixture", repository=self.root)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        opener = build_opener(ProxyHandler({}))
        host = f"http://127.0.0.1:{srv.server_port}"
        try:
            with self.assertRaises(HTTPError) as missing:
                opener.open(host + "/api/worktree", timeout=5)
            self.assertEqual(missing.exception.code, 401)
            missing.exception.close()
            with self.assertRaises(HTTPError) as invalid:
                opener.open(host + "/api/worktree?file=../archive.db&token=fixture", timeout=5)
            self.assertEqual(invalid.exception.code, 400)
            invalid.exception.close()
            with opener.open(host + "/api/worktree?file=profile.txt&token=fixture", timeout=5) as response:
                result = json.load(response)
            self.assertIn("+changed", result["diff"]["unstaged"])
            self.assertIn("# Repository review", result["handoff_markdown"])
            with opener.open(host, timeout=5) as response:
                self.assertIn('__AGENTTRACE_WORKTREE__ = "true"', response.read().decode())
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
