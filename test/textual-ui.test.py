#!/usr/bin/env python3
"""Pilot-driven keyboard, resize and async-race tests; no live traces touched."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
from typing import Any
import unittest
from unittest.mock import patch

if importlib.util.find_spec("textual") is None:
    print("SKIP Textual UI: install requirements-tui.txt to run the optional dashboard tests")
    raise SystemExit(0)

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "cli"), str(ROOT / "common")]
from agenttrace_tui import BODY_LIMIT, NodeRef, TraceApp, body_text, readable_body, span_forest  # noqa: E402
from herdr_review import PaneReview, pane_review  # noqa: E402
from textual.widgets import Button, DataTable, Input, Select, Static, TabbedContent, TextArea, Tree  # noqa: E402


class RepositoryUITests(unittest.IsolatedAsyncioTestCase):
    async def test_archived_inbox_opens_and_copies_handoff_after_worktree_moves(self):
        from core.review_tasks import record_task_event, resolve_repair_event, save_task, worktree_task
        from core.storage.sqlite_store import TraceStore
        from tui_data import LocalBackend
        from tui_inbox import ArchivedTaskDialog, TaskInbox

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "removed-worktree"
            subprocess.run(["git", "init", "-q", "-b", "main", root], check=True)
            db = parent / "archive.db"
            store = TraceStore(db)

            def save(goal):
                save_task(store, root, "branch:main", goal, pane_id=None, terminal_id=None,
                          agent=None, session_id=None)

            def event(kind, detail):
                task = worktree_task(store, root, "branch:main")
                return record_task_event(store, task, kind, detail)

            try:
                save("First review")
                event("checkpoint", "Keep the earlier reproduction notes")
                repair = event("repair_pending", "test · codex · w9:p1")
                resolve_repair_event(store, repair, sent=True)
                event("archived", "A current check failed\nHEAD 123 · snapshot abc")
                save("Second review")
                event("archived", "Required checks passed · 0 changed files")
                save("Active review")
                event("checkpoint", "Do not leak this note into earlier tasks")
            finally:
                store.close()
            root.rename(parent / "moved")

            app = TraceApp(LocalBackend(db, [], index=False), open_inbox=True)
            with patch.object(app, "copy_to_clipboard") as copy:
                async with app.run_test(size=(92, 30)) as pilot:
                    async def wait_for(condition):
                        for _ in range(60):
                            if condition():
                                return
                            await pilot.pause(.1)
                        self.fail("The archived task view did not reach its expected state")

                    await wait_for(lambda: isinstance(app.screen, TaskInbox) and len(app.screen.visible_tasks) == 1)
                    inbox = app.screen
                    self.assertEqual(inbox.visible_tasks[0].goal, "Active review")
                    await pilot.press("ctrl+t")
                    await wait_for(lambda: len(inbox.visible_tasks) == 2)
                    self.assertEqual([(item.goal, item.checkpoint) for item in inbox.visible_tasks], [
                        ("Second review", ""), ("First review", "Keep the earlier reproduction notes"),
                    ])
                    inbox.query_one(Input).value = "earlier reproduction"
                    await wait_for(lambda: len(inbox.visible_tasks) == 1)
                    self.assertIn("A current check failed", str(inbox.query_one("#inbox-next", Static).render()))
                    await pilot.click("#inbox-resume")
                    await wait_for(lambda: isinstance(app.screen, ArchivedTaskDialog))
                    text = app.screen.query_one("#archive-handoff", TextArea).text
                    self.assertIn("HEAD 123 · snapshot abc", text)
                    self.assertIn("Keep the earlier reproduction notes", text)
                    self.assertIn("Repair request sent", text)
                    self.assertNotIn("Do not leak", text)
                    await pilot.click("#archive-copy")
                    copy.assert_called_once_with(text)
                    await pilot.press("escape")
                    self.assertIs(app.screen, inbox)
                    await pilot.press("ctrl+t")
                    inbox.query_one(Input).value = ""
                    await wait_for(lambda: len(inbox.visible_tasks) == 1
                                   and inbox.visible_tasks[0].goal == "Active review")

    async def test_inbox_filters_resumes_and_rechecks_checkout_before_opening(self):
        from core.repo_review import record_check
        from core.review_git import checkout_identity
        from core.review_tasks import checkout_key, record_task_event, save_task, worktree_task
        from core.storage.sqlite_store import TraceStore
        from tui_data import LocalBackend
        from tui_inbox import TaskInbox
        from tui_repository import RepositoryScreen

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            first, second = parent / "first", parent / "second"
            store = TraceStore(parent / "inbox.db")
            try:
                for root, goal in ((first, "Update documentation"), (second, "Fix validation")):
                    root.mkdir()
                    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True)
                    checkout = checkout_key(*checkout_identity(root))
                    save_task(store, root, checkout, goal, pane_id=None, terminal_id=None, agent=None, session_id=None)
                task = worktree_task(store, second, "branch:main")
                record_task_event(store, task, "checkpoint", "Investigate the schema error")
                record_check(store, second, "validation", [sys.executable, "-c", "raise SystemExit(1)"])
            finally:
                store.close()

            app = TraceApp(LocalBackend(store.path, [], index=False), open_inbox=True)
            async with app.run_test(size=(92, 30)) as pilot:
                async def wait_for(condition):
                    for _ in range(60):
                        if condition():
                            return
                        await pilot.pause(.1)
                    self.fail("Task inbox did not reach its expected state")

                await wait_for(lambda: isinstance(app.screen, TaskInbox) and len(app.screen.visible_tasks) == 2)
                inbox = app.screen
                await pilot.press("/")
                inbox.query_one(Input).value = "schema error"
                await wait_for(lambda: len(inbox.visible_tasks) == 1
                               and not inbox.query_one("#inbox-resume", Button).disabled)
                self.assertEqual(inbox.visible_tasks[0].root, second)
                self.assertIn("Investigate failure", str(inbox.query_one("#inbox-next", Static).render()))
                await pilot.press("enter", "enter")
                await wait_for(lambda: isinstance(app.screen, RepositoryScreen) and app.screen.review is not None)
                self.assertEqual(app.screen.root, second)
                self.assertIn("schema error", app.screen.query_one(TextArea).text)

                await pilot.press("i")
                await wait_for(lambda: app.screen is inbox and len(inbox.visible_tasks) == 1)
                subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/other"], cwd=second, check=True)
                await pilot.press("enter")
                await wait_for(lambda: "Check out main" in str(inbox.query_one("#inbox-next", Static).render()))
                self.assertIs(app.screen, inbox)
                self.assertTrue(inbox.query_one("#inbox-resume", Button).disabled)
                self.assertEqual(checkout_identity(second)[0], "other")

                subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=second, check=True)
                task = worktree_task(store, second, "branch:main")
                try:
                    record_task_event(store, task, "archived", "Checkpoint archived")
                    save_task(store, second, "branch:main", "An unrelated task",
                              pane_id=None, terminal_id=None, agent=None, session_id=None)
                finally:
                    store.close()
                await pilot.press("enter")
                await wait_for(lambda: "was archived" in str(inbox.query_one("#inbox-next", Static).render()))
                self.assertIs(app.screen, inbox)
                await pilot.press("escape")

    async def test_repair_preview_sends_once_and_rechecks_snapshot_and_agent(self):
        from dataclasses import replace
        from core.repo_review import record_check
        from core.review_git import checkout_identity
        from core.review_tasks import checkout_key, save_task
        from core.storage.sqlite_store import TraceStore
        from tui_data import LocalBackend
        from tui_repository import RepairDialog, RepositoryScreen

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "example"
            root.mkdir()
            subprocess.run(["git", "init", "-q", root], check=True)
            file = root / "example.txt"
            file.write_text("broken")
            source = PaneReview(root, "w99:p1", "codex", "source-session", "fixture", "idle")
            store = TraceStore(parent / "review.db")
            try:
                save_task(store, root, checkout_key(*checkout_identity(root)), "Repair the preview harness",
                          pane_id=source.pane_id, terminal_id=source.terminal_id,
                          agent=source.agent, session_id=source.session_id)
                check = record_check(store, root, "test", [sys.executable, "-c", "raise SystemExit(1)"])
                self.assertEqual(check["state"], "failed")
            finally:
                store.close()
            opener = replace(source, pane_id="w99:p2", session_id="different-session")
            app = TraceApp(LocalBackend(store.path, [], index=False), repository=root, open_repository=True,
                           herdr_target=opener, continue_task=True)
            with (
                patch("tui_repository.task_source", return_value=source) as find_source,
                patch("tui_repository.herdr_request") as api,
            ):
                async with app.run_test(size=(100, 32)) as pilot:
                    for _ in range(60):
                        if isinstance(app.screen, RepositoryScreen) and not app.screen.query_one("#repo-next", Button).disabled:
                            break
                        await pilot.pause(.1)
                    await pilot.click("#repo-next")

                    async def preview():
                        await pilot.press("f")
                        self.assertIsInstance(dialog := app.screen, RepairDialog)
                        return dialog

                    dialog = await preview()
                    self.assertIn("w99:p1", str(dialog.query_one("#repair-status", Static).render()))
                    message = dialog.query_one("#repair-message", TextArea).text
                    self.assertIn("Repair the preview harness", message)
                    await pilot.press("escape")
                    api.assert_not_called()

                    dialog = await preview()
                    file.write_text("changed")
                    await pilot.click("#repair-send")
                    self.assertIn("failed check changed", str(dialog.query_one("#repair-status", Static).render()))
                    api.assert_not_called()
                    file.write_text("broken")
                    await pilot.press("escape")

                    dialog = await preview()
                    find_source.return_value = replace(source, session_id="new-session")
                    await pilot.click("#repair-send")
                    self.assertIn("source agent changed", str(dialog.query_one("#repair-status", Static).render()))
                    api.assert_not_called()
                    await pilot.press("escape")

                    find_source.return_value = replace(source, status="blocked")
                    dialog = await preview()
                    self.assertTrue(dialog.query_one("#repair-send", Button).disabled)
                    self.assertFalse(dialog.query_one("#repair-copy", Button).disabled)
                    await pilot.press("escape")

                    find_source.return_value = source
                    dialog = await preview()
                    await pilot.click("#repair-send")
                    api.assert_called_once_with("agent", "prompt", "w99:p1", message)
                    self.assertTrue(dialog.query_one("#repair-send", Button).disabled)
                    self.assertIn("Sent to w99:p1", str(dialog.query_one("#repair-status", Static).render()))
                    await pilot.press("escape", "g")
                    for _ in range(60):
                        if "Repair request sent" in app.screen.query_one("#repo-detail", TextArea).text:
                            break
                        await pilot.pause(.1)
                    self.assertIn("Repair request sent", app.screen.query_one("#repo-detail", TextArea).text)
                    self.assertEqual(str(app.screen.query_one("#repo-next", Button).label), "Follow up on repair")

    async def test_waiting_agent_does_not_close_task_popup_until_requested(self):
        from core.review_git import checkout_identity
        from core.review_tasks import checkout_key, save_task
        from core.storage.sqlite_store import TraceStore
        from tui_data import LocalBackend
        from tui_repository import RepositoryScreen

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "example"
            root.mkdir()
            subprocess.run(["git", "init", "-q", root], check=True)
            source = PaneReview(root, "w99:p1", "codex", None, "fixture", "blocked")
            store = TraceStore(parent / "review.db")
            try:
                save_task(store, root, checkout_key(*checkout_identity(root)), "Answer the agent",
                          pane_id=source.pane_id, terminal_id=source.terminal_id,
                          agent=source.agent, session_id=source.session_id)
            finally:
                store.close()
            app = TraceApp(LocalBackend(store.path, [], index=False), repository=root, open_repository=True,
                           herdr_target=source, continue_task=True)
            with patch("tui_repository.task_source", return_value=source), patch("tui_repository.herdr_request") as api:
                async with app.run_test(size=(92, 30)) as pilot:
                    for _ in range(60):
                        if isinstance(app.screen, RepositoryScreen) and "Answer agent" in str(
                                app.screen.query_one("#repo-task", Static).render()):
                            break
                        await pilot.pause(.1)
                    self.assertTrue(app.is_running)
                    self.assertIn("Answer agent", str(app.screen.query_one("#repo-task", Static).render()))
                    api.assert_not_called()
                    await pilot.press("a")
                    api.assert_called_once_with("agent", "focus", "w99:p1")
                    self.assertFalse(app.is_running)

    async def test_task_resumes_failed_check_then_reruns_after_worktree_change(self):
        from core.repo_review import record_check, repository_review
        from core.storage.sqlite_store import TraceStore
        from tui_data import LocalBackend
        from tui_repository import ArchiveTaskDialog, RepositoryScreen

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "example"
            root.mkdir()
            subprocess.run(["git", "init", "-q", root], check=True)
            source = root / "example.txt"
            source.write_text("broken")
            command = [sys.executable, "-c",
                       "from pathlib import Path; assert Path('example.txt').read_text() == 'fixed', 'repair needed'"]
            (root / ".agenttrace-review.json").write_text(json.dumps({"checks": {"test": command}}))
            store = TraceStore(parent / "review.db")
            self.addCleanup(store.close)

            async def wait_for(pilot, condition):
                for _ in range(60):
                    if condition():
                        return
                    await pilot.pause(.1)
                self.fail("The task did not reach its expected state")

            def make_app(pane):
                return TraceApp(LocalBackend(store.path, [], index=False), repository=root, open_repository=True,
                                herdr_target=PaneReview(root, pane, "codex", None, "fixture-terminal") if pane else None,
                                continue_task=True)

            with patch("tui_repository.task_source", return_value=None):
                app = make_app("w99:p1")
                async with app.run_test(size=(92, 30)) as pilot:
                    await wait_for(pilot, lambda: isinstance(app.screen, RepositoryScreen) and app.screen.review)
                    await pilot.press("n")
                    app.screen.query_one("#task-goal", Input).value = "Repair example validation"
                    await pilot.press("enter")
                    await wait_for(pilot, lambda: isinstance(app.screen, RepositoryScreen) and app.screen.review["task"])
                    self.assertEqual(app.screen.review["task"]["pane_id"], "w99:p1")
                    await pilot.press("j")
                    app.screen.query_one("#task-goal", Input).value = "Fix the example and then rerun the test"
                    await pilot.press("enter")
                    await wait_for(pilot, lambda: isinstance(app.screen, RepositoryScreen)
                                   and "rerun the test" in app.screen.query_one(TextArea).text)
                    await pilot.press("escape")
                self.assertEqual(record_check(store, root, "test", [])["state"], "failed")

                app = make_app("w99:p2")
                async with app.run_test(size=(92, 30)) as pilot:
                    await wait_for(pilot, lambda: isinstance(app.screen, RepositoryScreen)
                                   and "rerun the test" in app.screen.query_one(TextArea).text)
                    await pilot.click("#repo-next")
                    await wait_for(pilot, lambda: isinstance(app.screen, RepositoryScreen)
                                   and "repair needed" in app.screen.query_one(TextArea).text)
                    screen = app.screen
                    self.assertEqual(screen.review["task"]["pane_id"], "w99:p1")
                    self.assertIn("Repair example validation", str(screen.query_one("#repo-task", Static).render()))
                    source.write_text("fixed")
                    await pilot.press("r")
                    await wait_for(pilot, lambda: screen.review["state"] == "stale")
                    self.assertIn("Rerun checks", str(screen.query_one("#repo-task", Static).render()))
                    await pilot.press("v", "escape")
                    self.assertFalse(app.is_running)
                for _ in range(60):
                    if repository_review(store, root)["state"] == "passed":
                        break
                    await asyncio.sleep(.1)
                self.assertEqual(repository_review(store, root)["state"], "passed")
                ref = subprocess.check_output(["git", "symbolic-ref", "HEAD"], cwd=root, text=True).strip()
                subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/other"], cwd=root, check=True)
                self.assertIsNone(repository_review(store, root, include_task=True)["task"])
                subprocess.run(["git", "symbolic-ref", "HEAD", ref], cwd=root, check=True)
                self.assertEqual(repository_review(store, root, include_task=True)["task"]["goal"],
                                 "Repair example validation")
                app = make_app(None)
                async with app.run_test(size=(92, 30)) as pilot:
                    await wait_for(pilot, lambda: isinstance(app.screen, RepositoryScreen)
                                   and "rerun the test" in app.screen.query_one(TextArea).text)
                    await pilot.press("c", "z")
                    self.assertIsInstance(app.screen, ArchiveTaskDialog)
                    self.assertIn("Required checks passed", app.screen.summary)
                    await pilot.click("#archive-confirm")
                    await wait_for(pilot, lambda: isinstance(app.screen, RepositoryScreen)
                                   and "Task archived" in app.screen.query_one(TextArea).text)
                    self.assertIsNone(app.screen.review["task"])
                    self.assertIn("Required checks passed", app.screen.query_one(TextArea).text)
                    await pilot.click("#repo-next")
                    app.screen.query_one("#task-goal", Input).value = "Next task"
                    await pilot.press("enter")
                    await wait_for(pilot, lambda: isinstance(app.screen, RepositoryScreen)
                                   and app.screen.review["task"] and app.screen.review["task"]["goal"] == "Next task")
                    self.assertEqual(app.screen.review["activity"]["checkpoint"], "")

    async def test_herdr_uses_only_the_pane_session_until_a_session_is_selected(self):
        from core.storage.sqlite_store import TraceStore
        from tui_data import LocalBackend
        from tui_repository import RepositoryScreen

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "example"
            root.mkdir()
            subprocess.run(["git", "init", "-q", root], check=True)
            (root / "example.txt").write_text("working copy\n")
            store = TraceStore(parent / "review.db")
            try:
                with store.transaction():
                    store._conn().executemany(
                        "INSERT INTO sessions(session_id, adapter_id, cwd, first_ts, last_ts) VALUES (?, ?, ?, ?, ?)",
                        [
                            ("newer", "codex", str(root), "2026-01-02", "2026-01-02"),
                            ("exact", "codex", str(root), "2026-01-01", "2026-01-01"),
                            ("other", "codex", str(parent), "2026-01-03", "2026-01-03"),
                        ],
                    )
            finally:
                store.close()

            for native in ("exact", None, "syncing", "other"):
                with self.subTest(native=native):
                    target = PaneReview(root, "w6:p1", "codex", native)
                    app = TraceApp(LocalBackend(store.path, [], index=False), repository=root,
                                   open_repository=True, herdr_target=target)
                    async with app.run_test(size=(82, 24)) as pilot:
                        for _ in range(60):
                            if isinstance(app.screen, RepositoryScreen) and app.screen.review:
                                break
                            await pilot.pause(.1)
                        self.assertIsInstance(screen := app.screen, RepositoryScreen)
                        self.assertIsNotNone(screen.review)
                        selected = screen.review["selected"]
                        self.assertEqual(selected["session_id"] if selected else None,
                                         native if native == "exact" else None)
                        self.assertEqual(screen.review["files"][0]["path"], "example.txt")
                        self.assertIn("Pane session" if native == "exact" else "choose in History",
                                      str(screen.query_one("#repo-context", Static).render()))
                        await pilot.press("t", "enter")
                        for _ in range(60):
                            if screen.review["selected"] and screen.review["selected"]["session_id"] == "newer":
                                break
                            await pilot.pause(.1)
                        self.assertEqual(screen.review["selected"]["session_id"], "newer")
                        await pilot.press("escape")
                        self.assertFalse(app.is_running)

    async def test_open_diff_and_refresh_check_after_worktree_edit(self):
        from core.repo_review import record_check
        from core.storage.sqlite_store import TraceStore
        from tui_data import LocalBackend
        from tui_repository import RepositoryScreen

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "example"
            root.mkdir()
            subprocess.run(["git", "init", "-q", root], check=True)
            source = root / "example.txt"
            source.write_text("expected content\n")
            command = [sys.executable, "-c",
                       "from pathlib import Path; assert 'expected content' in Path('example.txt').read_text()"]
            (root / ".agenttrace-review.json").write_text(json.dumps({"checks": {"test": command}}))
            store = TraceStore(parent / "review.db")
            try:
                self.assertEqual(record_check(store, root, "test", [])["state"], "passed")
            finally:
                store.close()
            app = TraceApp(LocalBackend(store.path, [], index=False), repository=root, open_repository=True)
            async with app.run_test(size=(90, 30)) as pilot:
                async def wait_for(condition):
                    for _ in range(60):
                        await pilot.pause(.1)
                        if condition():
                            return
                    self.fail("Repository review did not reach the expected state")

                await wait_for(lambda: isinstance(app.screen, RepositoryScreen) and app.screen.review is not None)
                screen = app.screen
                self.assertIn("Required checks passed", str(screen.query_one("#repo-result", Static).render()))
                await pilot.press("down", "enter")
                await wait_for(lambda: "+expected content" in screen.query_one(TextArea).text)
                await pilot.press("c", "enter")
                self.assertIn("exit 0", screen.query_one(TextArea).text)
                source.write_text("edited content\n")
                await pilot.press("r")
                await wait_for(lambda: "out of date" in str(screen.query_one("#repo-result", Static).render()))
                await pilot.press("escape", "w")
                self.assertIsInstance(app.screen, RepositoryScreen)


class HerdrPaneTests(unittest.TestCase):
    def test_popup_prefers_source_pane_and_foreground_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(["git", "init", "-q", root], check=True)
            child = root / "src"
            child.mkdir()
            pane = {"pane_id": "w6:p1", "foreground_cwd": str(child), "cwd": "/", "agent": "pi"}
            expected = str(root / "session.jsonl")
            env = {"HERDR_ACTIVE_PANE_ID": "w6:p1", "HERDR_PANE_ID": "w3:p2", "HERDR_BIN_PATH": "/bin/herdr"}
            real_run = subprocess.run
            for ref, session in [
                ({"agent": "pi", "kind": "path", "value": expected}, expected),
                ({"agent": "pi", "kind": "id", "value": "guessed"}, None),
                ({"agent": "codex", "kind": "id", "value": "stale"}, None),
                (None, None),
            ]:
                with self.subTest(ref=ref):
                    pane["agent_session"] = ref

                    def run(command, **kwargs):
                        if command[0] == "/bin/herdr":
                            self.assertEqual(command, ["/bin/herdr", "pane", "get", "w6:p1"])
                            return subprocess.CompletedProcess(command, 0, json.dumps({"result": {"pane": pane}}))
                        return real_run(command, **kwargs)

                    with patch.dict(os.environ, env), patch("herdr_review.subprocess.run", side_effect=run):
                        self.assertEqual(pane_review(), PaneReview(root, "w6:p1", "pi", session))


class PaginatedBackend:
    """Delegate payloads to FakeBackend, but honor the real session page size."""
    def __init__(self, rows):
        self.base = FakeBackend()
        self.base.rows = rows
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.resolution_error: Exception | None = None

    def __getattr__(self, name) -> Any:
        return getattr(self.base, name)

    def dashboard(self, search="", agent=None, offset=0):
        return self.base.dashboard(search, agent, offset)

    def resolve_session(self, session_id, agent=None):
        if self.block:
            self.entered.set()
            self.release.wait(5)
        if self.resolution_error is not None:
            raise self.resolution_error
        return self.base.resolve_session(session_id, agent)

    def close(self):
        self.release.set()
        self.base.close()


class MultiTurnBackend:
    def __init__(self):
        self.base = FakeBackend()
        self.version = "old"
        self.calls = []
        self.block_trace: str | None = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_delayed = False
        self.remove_selected_call = False

    def __getattr__(self, name) -> Any:
        return getattr(self.base, name)

    def session(self, key, offset=0):
        data = self.base.session(key, offset)
        row = data["turns"]["traces"][0]
        data["turns"].update(total=3, traces=[dict(row, trace_id=t)
                                            for t in ("shared", "second", "third")])
        return data

    def trace(self, key, trace_id):
        self.calls.append((key, trace_id))
        version = self.version
        if key[0] == "a" and trace_id == self.block_trace:
            self.entered.set()
            self.release.wait(5)
            if self.fail_delayed:
                raise OSError("old context query failed")
        if self.remove_selected_call and trace_id == "shared":
            return dict(spans=[dict(span_id="replacement", parent_span_id=None, kind="llm",
                                    name=key[0] + " " + version, status="ok", duration_ms=1)])
        return dict(spans=[dict(span_id="call", parent_span_id=None, kind="llm",
                               name=key[0] + " " + version, status="ok", duration_ms=1),
                           dict(span_id="child", parent_span_id="call", kind="tool",
                                name=version, status="ok", duration_ms=1)])

    def close(self):
        self.release.set()
        self.base.close()


class FakeBackend:
    def __init__(self):
        self.rows = [dict(adapter_id=agent, session_id="same", cwd="/work/" + agent,
                          trace_count=3) for agent in ("a", "b")]
        self.output = "\n".join(f"line {i}: reply from a" for i in range(180))
        self.failure = False
        self.block_agent = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = False

    def start(self):
        pass

    def close(self):
        self.closed = True
        self.release.set()

    def dashboard(self, search="", agent=None, offset=0) -> dict:
        if self.failure:
            raise OSError("fixture temporarily unavailable")
        rows = [r for r in self.rows if (agent is None or r["adapter_id"] == agent)
                and (not search or search.lower() in str(r).lower())]
        return dict(
            overview=dict(tokens=dict(input=12000, output=3000, cache_read=9000),
                          task_duration_ms=dict(p50=2400), errors=1, unclosed=2,
                          sessions=len(self.rows), traces=6, spans=20),
            sessions=dict(total=len(rows), has_more=offset + 30 < len(rows),
                          sessions=deepcopy(rows[offset:offset + 30])),
            recent=[dict(tokens=dict(input=100, output=4, cache_read=80), status="ok",
                         duration_ms=2400, unclosed=0)],
            agents=["a", "b"], health=dict(index=False, scans=1), usage_known=True)

    def session(self, key, offset=0) -> dict:
        return dict(metrics={}, turns=dict(total=1, traces=[dict(
            trace_id="shared", name="Turn " + key[0], status="ok", duration_ms=2400,
            start="2026-11-15T10:00:00.000Z")]))

    def resolve_session(self, session_id, agent=None):
        matches = {(r["adapter_id"], r["session_id"]) for r in self.rows
                   if r["session_id"] == session_id and (agent is None or r["adapter_id"] == agent)}
        if not matches:
            raise LookupError("session not found")
        if len(matches) > 1:
            raise ValueError("session belongs to multiple agents")
        return next(iter(matches))

    def trace(self, key, trace_id):
        return dict(spans=[dict(span_id="call", parent_span_id=None, kind="llm",
                               name="Model " + key[0], status="ok", duration_ms=1200)])

    def span(self, key, trace_id, span_id):
        if key[0] == self.block_agent:
            self.entered.set()
            self.release.wait(5)
        return dict(span_id=span_id, trace_id=trace_id, adapter_id=key[0], session_id=key[1],
                    kind="llm", name="Model " + key[0], model="model-" + key[0],
                    status="ok", duration_ms=1200, input={"owner": key[0]},
                    output=self.output if key[0] == "a" else "reply from b", error=None)


async def settled(app, pilot):
    await pilot.pause(.05)
    await app.workers.wait_for_complete()
    await pilot.pause(.05)


@asynccontextmanager
async def running(size=(176, 40), **kwargs):
    backend = FakeBackend()
    app = TraceApp(backend, interval=3600, **kwargs)
    async with app.run_test(size=size) as pilot:
        await settled(app, pilot)
        yield app, pilot, backend


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_work_review_is_primary_and_opens_evidence_and_portable_handoff(self):
        backend = FakeBackend()
        original = backend.session
        def session(key, offset=0):
            data = original(key, offset)
            data["review"] = dict(adapter_id=key[0], session_id=key[1], cwd="/work/project",
                trace_id="shared", task="Fix the failing task 中文", conclusion="Agent says done",
                state="failed", label="Checks failed", scope="Latest turn only; not current worktree approval",
                next_action="Open the failed output and rerun the check.", limited=False, files=[], failures=[],
                checks=[dict(command="pytest -q", state="failed", span_id="call", trace_id="shared",
                             output_excerpt="1 failed")])
            return data
        backend.session = session
        app = TraceApp(backend, interval=3600)
        async with app.run_test(size=(176, 40)) as pilot:
            await settled(app, pilot)
            self.assertFalse(app.query_one("#metrics").display)
            self.assertEqual(app.query_one("#details", TabbedContent).active, "tab-review")
            self.assertIn("pytest -q", app.query_one("#field-review", TextArea).text)
            await pilot.resize_terminal(80, 30)
            await pilot.pause(.1)
            app.action_panel("detail")
            await pilot.press("escape")
            self.assertEqual(app._active_panel, "sessions")
            await pilot.resize_terminal(176, 40)
            await pilot.pause(.1)
            self.assertIn("Checks failed", str(app.query_one("#review-state").render()))
            self.assertIn("Fix the failing task 中文", str(app.query_one("#review-task").render()))
            await pilot.click("#review-evidence")
            await settled(app, pilot)
            self.assertEqual(app.span_ref, NodeRef("shared", "call"))
            await pilot.click("#handoff")
            await pilot.pause(.1)
            text = app.screen.query_one("#handoff-text", TextArea).text
            self.assertIn("unverified self-report", text)
            self.assertIn("pytest -q", text)
            self.assertIn("a / same", text)
            app.action_refresh()
            await settled(app, pilot)
            await pilot.press("escape")
            await pilot.pause(.1)
            self.assertIs(app.screen, app.screen_stack[0])
            app.action_metrics()
            self.assertTrue(app.query_one("#metrics").display)

    async def test_mouse_selects_sessions_calls_and_detail_tabs(self):
        async with running() as (app, pilot, backend):
            await pilot.click("#sessions", offset=(3, 3))
            await settled(app, pilot)
            self.assertEqual(app.session_key, ("b", "same"))
            await pilot.click("#traces", offset=(10, 1))
            await settled(app, pilot)
            self.assertEqual(app.span_ref, NodeRef("shared", "call"))
            details = app.query_one("#details", TabbedContent)
            await pilot.click(details.get_tab("tab-output"))
            await pilot.pause(.1)
            self.assertEqual(details.active, "tab-output")
            self.assertEqual(app.query_one("#field-output", TextArea).text, "reply from b")

    async def test_keyboard_namespace_selection_and_tabs(self):
        async with running() as (app, pilot, backend):
            self.assertEqual(app.session_key, ("a", "same"))
            self.assertEqual(app.span_ref, NodeRef("shared", "call"))
            app.query_one("#sessions", DataTable).focus()
            await pilot.press("down", "enter")
            await settled(app, pilot)
            self.assertEqual(app.session_key, ("b", "same"))
            self.assertIn('"b"', app.query_one("#field-input", TextArea).text)
            self.assertEqual(app.query_one("#field-output", TextArea).text, "reply from b")
            app.action_panel("detail")
            await pilot.press("ctrl+q")
        self.assertTrue(backend.closed)

    async def test_refresh_preserves_selection_tab_and_scroll(self):
        async with running() as (app, pilot, backend):
            area = app.query_one("#field-output", TextArea)
            app.query_one("#details", TabbedContent).active = "tab-output"
            await pilot.pause(.1)
            area.scroll_to(y=40, animate=False, immediate=True)
            previous = area.scroll_offset.y
            self.assertGreater(previous, 0)
            backend.rows.insert(0, dict(adapter_id="new", session_id="new", cwd="/new", trace_count=1))
            backend.output += "\nnew tail"
            app.action_refresh()
            await settled(app, pilot)
            self.assertEqual(app.session_key, ("a", "same"))
            self.assertEqual(app.query_one("#details", TabbedContent).active, "tab-output")
            self.assertEqual(area.scroll_offset.y, previous)

    async def test_delayed_foreign_payload_cannot_replace_new_selection(self):
        async with running() as (app, pilot, backend):
            backend.block_agent = "a"
            app.action_refresh()
            await asyncio.to_thread(backend.entered.wait, 2)
            app.query_one("#sessions", DataTable).focus()
            await pilot.press("down")
            await pilot.pause(.2)
            self.assertEqual(app.session_key, ("b", "same"))
            backend.release.set()
            await settled(app, pilot)
            self.assertEqual(app.query_one("#field-output", TextArea).text, "reply from b")

    async def test_search_and_pause_then_resume(self):
        async with running() as (app, pilot, backend):
            app.action_pause()
            self.assertFalse(app.live)
            app.action_search()
            field = app.query_one("#search", Input)
            field.value = "b"
            await pilot.pause(.4)
            await settled(app, pilot)
            self.assertEqual(app.session_key, ("b", "same"))
            self.assertEqual(len(app._session_rows), 1)
            app.action_pause()
            await settled(app, pilot)
            self.assertTrue(app.live)

    async def test_clear_search_and_agent_restores_all_sessions_and_page(self):
        async with running() as (app, pilot, backend):
            backend.rows = [dict(adapter_id=agent, session_id=f"s{i}", cwd=f"/work/project-{i}",
                                 trace_count=i + 1) for i, agent in enumerate(["a", "b"] * 36)]
            app.action_refresh()
            await settled(app, pilot)
            app.query_one("#sessions-next", Button).press()
            await settled(app, pilot)
            self.assertEqual(app.session_offset, 30)
            self.assertEqual(len(app._session_rows), 30)
            app.query_one("#agent", Select).value = "a"
            app.query_one("#search", Input).value = "project-2"
            await pilot.pause(.4)
            await settled(app, pilot)
            self.assertTrue(app.query_one("#filters").display)
            self.assertIn("agent=a", str(app.query_one("#filter-summary").render()))
            self.assertEqual(app.session_offset, 0)
            self.assertTrue(all(row["adapter_id"] == "a" and "project-2" in row["cwd"]
                                for row in app._session_rows.values()))
            await pilot.press("ctrl+l")
            await settled(app, pilot)
            self.assertIsNone(app.agent_filter)
            self.assertEqual(app.query_one("#search", Input).value, "")
            self.assertFalse(app.query_one("#filters").display)
            self.assertEqual(app.session_offset, 0)
            self.assertIn("/ 72", str(app.query_one("#sessions-page").render()))
            seen = set(app._session_rows)
            for _ in range(2):
                app.query_one("#sessions-next", Button).press()
                await settled(app, pilot)
                seen.update(app._session_rows)
            self.assertEqual(len(seen), 72)
            self.assertTrue(app.query_one("#sessions-next", Button).disabled)

    async def test_empty_agent_filter_shows_recovery_and_clear_button_works(self):
        async with running() as (app, pilot, backend):
            backend.rows = [r for r in backend.rows if r["adapter_id"] == "a"]
            app.query_one("#agent", Select).value = "b"
            await settled(app, pilot)
            self.assertEqual(app._session_rows, {})
            self.assertIn("Ctrl+L", app.query_one("#field-info", TextArea).text)
            self.assertIn("0 / 1", str(app.query_one("#filter-summary").render()))
            app.query_one("#clear-filters", Button).press()
            await settled(app, pilot)
            self.assertEqual(app.session_key, ("a", "same"))
            self.assertEqual(len(app._session_rows), 1)

    async def test_session_label_updates_when_metadata_arrives(self):
        async with running() as (app, pilot, backend):
            backend.rows[0]["cwd"] = "/work/new-project-name"
            app.action_refresh()
            await settled(app, pilot)
            row_key = app._session_keys[0]
            self.assertIn("new-project-name", str(app.query_one("#sessions", DataTable).get_cell(row_key, "identity")))

    async def test_deleted_last_page_recovers_without_hiding_remaining_sessions(self):
        async with running() as (app, pilot, backend):
            backend.rows = [dict(adapter_id="a", session_id=f"s{i}", cwd="/work", trace_count=1)
                            for i in range(35)]
            app.session_offset = 30
            app.action_refresh()
            await settled(app, pilot)
            self.assertEqual(len(app._session_rows), 5)
            backend.rows = backend.rows[:20]
            app.action_refresh()
            await settled(app, pilot)
            self.assertEqual(app.session_offset, 0)
            self.assertEqual(len(app._session_rows), 20)
            self.assertIn("/ 20", str(app.query_one("#sessions-page").render()))

    async def test_failures_keep_last_good_payload_and_recover(self):
        async with running() as (app, pilot, backend):
            previous = app.query_one("#field-output", TextArea).text
            backend.failure = True
            app.action_refresh()
            await settled(app, pilot)
            self.assertIn("STALE", str(app.query_one("#status").render()))
            self.assertEqual(app.query_one("#field-output", TextArea).text, previous)
            backend.failure = False
            app.action_refresh()
            await settled(app, pilot)
            self.assertNotIn("STALE", str(app.query_one("#status").render()))

    async def test_responsive_split_and_single_column_navigation(self):
        async with running(size=(176, 27)) as (app, pilot, backend):
            for size in ((176, 27), (110, 28), (76, 24), (60, 20)):
                await pilot.resize_terminal(*size)
                await pilot.pause(.1)
                self.assertEqual(app.screen.has_class("narrow"), size[0] < 85)
                for panel in ("sessions", "traces", "detail"):
                    app.action_panel(panel)
                    await pilot.pause(.05)
                    widget = app.query_one("#" + panel + "-panel")
                    self.assertTrue(widget.display)
                    self.assertGreater(widget.size.height, 0)
                    self.assertLessEqual(widget.region.right, size[0])
            await pilot.resize_terminal(176, 27)
            self.assertEqual(app.session_key, ("a", "same"))

    async def test_ambiguous_direct_session_requires_a_namespace(self):
        async with running(session="same") as (app, pilot, backend):
            self.assertIsNone(app.session_key)
            self.assertIn("multiple agents", app.query_one("#field-info", TextArea).text)
        async with running(session="same", agent="b") as (app, pilot, backend):
            self.assertEqual(app.session_key, ("b", "same"))

    async def test_initialization_error_can_retry(self):
        backend = FakeBackend()
        original = backend.start
        backend.start = lambda: (_ for _ in ()).throw(OSError("archive unavailable"))
        app = TraceApp(backend, interval=3600)
        async with app.run_test(size=(110, 28)) as pilot:
            await settled(app, pilot)
            self.assertFalse(app.ready)
            self.assertIn("STALE", str(app.query_one("#live").render()))
            backend.start = original
            app.action_refresh()
            await settled(app, pilot)
            self.assertTrue(app.ready)
            self.assertEqual(app.session_key, ("a", "same"))

    async def test_pausing_discards_inflight_payload(self):
        async with running() as (app, pilot, backend):
            original = app.query_one("#field-output", TextArea).text
            backend.block_agent = "a"
            backend.output = "newer response"
            app.action_refresh()
            await asyncio.to_thread(backend.entered.wait, 2)
            app.action_pause()
            backend.release.set()
            await settled(app, pilot)
            self.assertEqual(app.query_one("#field-output", TextArea).text, original)
            app.action_pause()
            await settled(app, pilot)
            self.assertEqual(app.query_one("#field-output", TextArea).text, "newer response")


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def session_rows():
        return [dict(adapter_id="a", session_id=f"target-copy-{i}", trace_count=1)
                for i in range(30, 0, -1)] + [dict(adapter_id="a", session_id="target", trace_count=1)]

    async def test_explicit_session_resolves_page_two_exactly(self):
        backend = PaginatedBackend(self.session_rows())
        app = TraceApp(backend, agent="a", session="target", interval=3600)
        async with app.run_test() as pilot:
            await settled(app, pilot)
            self.assertEqual(app.session_key, ("a", "target"))
            self.assertEqual(app.session_offset, 0)
            self.assertFalse(any(r["session_id"] == "target" for r in app._session_rows.values()))
            app.action_refresh()
            await settled(app, pilot)
            self.assertEqual(app.session_key, ("a", "target"))

    async def test_missing_explicit_session_never_substitutes_a_partial_match(self):
        backend = PaginatedBackend(self.session_rows()[:-1])
        app = TraceApp(backend, agent="a", session="target", interval=3600)
        async with app.run_test() as pilot:
            await settled(app, pilot)
            self.assertIsNone(app.session_key)
            self.assertIn("not found", app.query_one("#field-info", TextArea).text)
            app.action_refresh()
            await settled(app, pilot)
            self.assertIsNone(app.session_key)

    async def test_ambiguity_is_detected_across_session_pages(self):
        rows = self.session_rows()
        rows[0] = dict(adapter_id="b", session_id="target", trace_count=1)
        app = TraceApp(PaginatedBackend(rows), session="target", interval=3600)
        async with app.run_test() as pilot:
            await settled(app, pilot)
            self.assertIsNone(app.session_key)
            self.assertIn("multiple agents", app.query_one("#field-info", TextArea).text)

    async def test_delayed_exact_lookup_cannot_override_changed_filter(self):
        backend = PaginatedBackend(self.session_rows())
        backend.block = True
        app = TraceApp(backend, session="target", interval=3600)
        async with app.run_test() as pilot:
            try:
                self.assertTrue(await asyncio.to_thread(backend.entered.wait, 2))
                app.query_one("#search", Input).value = "target-copy-29"
                await pilot.pause(.4)
            finally:
                backend.release.set()
            await settled(app, pilot)
            self.assertEqual(app.session_key, ("a", "target-copy-29"))

    async def test_pending_exact_lookup_preserves_newer_row_selection(self):
        for outcome in ("resolved", "missing", "ambiguous", "row_left_page"):
            with self.subTest(outcome=outcome):
                backend = PaginatedBackend(self.session_rows())
                app = TraceApp(backend, agent="a", session="target", interval=3600)
                async with app.run_test() as pilot:
                    await settled(app, pilot)
                    await app.workers.wait_for_complete()
                    self.assertFalse(app._refreshing)
                    backend.block = True
                    if outcome == "row_left_page":
                        backend.base.rows = [r for r in backend.base.rows
                                             if r["session_id"] != "target-copy-29"]
                    app.action_refresh()
                    try:
                        self.assertTrue(await asyncio.to_thread(backend.entered.wait, 2))
                        table = app.query_one("#sessions", DataTable)
                        table.move_cursor(row=1)
                        await pilot.pause()
                        self.assertEqual(app.session_key, ("a", "target-copy-29"))
                        self.assertIsNone(app.initial_session)
                        if outcome == "missing":
                            backend.base.rows.pop()
                        elif outcome == "ambiguous":
                            backend.resolution_error = ValueError("ambiguous")
                    finally:
                        backend.release.set()
                    await settled(app, pilot)
                    self.assertEqual(app.session_key, ("a", "target-copy-29"))
                    self.assertEqual(app.span_ref, NodeRef("shared", "call"))
                    self.assertNotIn("not found", app.query_one("#field-info", TextArea).text)
                    self.assertNotIn("multiple agents", app.query_one("#field-info", TextArea).text)

    async def test_refresh_without_selected_span_updates_other_expanded_turns(self):
        backend = MultiTurnBackend()
        app = TraceApp(backend, interval=3600)
        async with app.run_test() as pilot:
            await settled(app, pilot)
            app._turn_nodes["second"].expand()
            await settled(app, pilot)
            backend.remove_selected_call = True
            app.action_refresh()
            await settled(app, pilot)
            self.assertIsNone(app.span_ref)
            backend.calls.clear()
            backend.version = "after removal"
            app.action_refresh()
            await settled(app, pilot)
            self.assertIn("after removal", str(app._turn_nodes["second"].children[0].label))
            self.assertEqual(app.span_ref, NodeRef("shared", "replacement"))
            self.assertTrue(app._turn_nodes["second"].is_expanded)
            self.assertFalse(app._turn_nodes["third"].is_expanded)
            self.assertCountEqual(backend.calls, [(("a", "same"), "shared"), (("a", "same"), "second")])

    async def test_independent_expansions_finish_when_another_turn_expands(self):
        backend = MultiTurnBackend()
        app = TraceApp(backend, interval=3600)
        async with app.run_test() as pilot:
            await settled(app, pilot)
            backend.block_trace = "second"
            app._turn_nodes["second"].expand()
            try:
                self.assertTrue(await asyncio.to_thread(backend.entered.wait, 2))
                app._turn_nodes["third"].expand()
                await pilot.pause(.15)
                self.assertTrue(app._turn_nodes["third"].children)
            finally:
                backend.release.set()
            await settled(app, pilot)
            self.assertTrue(app._turn_nodes["second"].children)
            self.assertTrue(app._turn_nodes["second"].is_expanded)

    async def test_refresh_updates_all_expanded_turns_preserving_tree_state(self):
        backend = MultiTurnBackend()
        app = TraceApp(backend, interval=3600)
        async with app.run_test() as pilot:
            await settled(app, pilot)
            app._turn_nodes["second"].expand()
            await settled(app, pilot)
            child = app._turn_nodes["second"].children[0]
            child.expand()
            await pilot.pause(.1)
            tree = app.query_one("#traces", Tree)
            with app.prevent(Tree.NodeSelected):
                tree.select_node(child.children[0])
            await pilot.pause(.1)
            self.assertEqual(tree.cursor_node.data, NodeRef("second", "child"))
            selected = app.span_ref
            backend.calls.clear()
            backend.version = "new"
            app.action_refresh()
            await settled(app, pilot)
            self.assertIn("new", str(app._turn_nodes["second"].children[0].label))
            self.assertTrue(app._turn_nodes["second"].children[0].is_expanded)
            self.assertEqual(tree.cursor_node.data, NodeRef("second", "child"))
            self.assertEqual(app.span_ref, selected)
            self.assertFalse(app._turn_nodes["third"].is_expanded)
            self.assertCountEqual(backend.calls, [(("a", "same"), "shared"), (("a", "same"), "second")])
            backend.calls.clear()
            backend.version = "timer update"
            app._tick()
            await settled(app, pilot)
            self.assertIn("timer update", str(app._turn_nodes["second"].children[0].label))
            self.assertEqual(tree.cursor_node.data, NodeRef("second", "child"))
            self.assertCountEqual(backend.calls, [(("a", "same"), "shared"), (("a", "same"), "second")])
            # The timer path uses the same refresh, without reopening collapsed turns.
            app._turn_nodes["second"].collapse()
            backend.calls.clear()
            backend.version = "newest"
            app._tick()
            await settled(app, pilot)
            self.assertEqual(backend.calls, [(("a", "same"), "shared")])
            self.assertFalse(app._turn_nodes["second"].is_expanded)
            app._turn_nodes["second"].expand()
            await settled(app, pilot)
            self.assertIn("newest", str(app._turn_nodes["second"].children[0].label))

    async def test_refresh_and_expand_share_delayed_query_and_discard_old_context(self):
        for failure in (False, True):
            with self.subTest(delayed_failure=failure):
                backend = MultiTurnBackend()
                app = TraceApp(backend, interval=3600)
                async with app.run_test() as pilot:
                    await settled(app, pilot)
                    backend.calls.clear()
                    backend.block_trace = "second"
                    app._turn_nodes["second"].expand()
                    try:
                        self.assertTrue(await asyncio.to_thread(backend.entered.wait, 2))
                        app.action_refresh()
                        await pilot.pause(.2)
                        self.assertEqual(backend.calls.count((("a", "same"), "second")), 1)
                        # Both an expansion and refresh await the old context.
                        app._select_session(("b", "same"))
                        await app._load_session(app.session_key, app._session_epoch)
                        backend.fail_delayed = failure
                    finally:
                        backend.release.set()
                    await settled(app, pilot)
                    self.assertEqual(app.session_key, ("b", "same"))
                    self.assertFalse(app._turn_nodes["second"].children)
                    self.assertIn("b old", str(app._turn_nodes["shared"].children[0].label))
                    self.assertNotIn("STALE", str(app.query_one("#status").render()))


class RenderingTests(unittest.TestCase):
    def test_long_project_and_task_fit_the_two_line_session_row(self):
        from rich.console import Console
        console = Console(width=25, record=True, file=open(os.devnull, "w"))
        try:
            console.print(TraceApp._session_label(dict(adapter_id="codex", session_id="long-session-id",
                cwd="/work/an-extremely-long-project-name", task="Fix 缓存 invalidation and add tests", review_state="failed")))
            lines = console.export_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("codex", lines[0])
            self.assertIn("Fix 缓存", lines[1])
        finally:
            console.file.close()

    def test_messages_and_provider_results_render_as_readable_text(self):
        request = {"messages": [{"role": "user", "content": "第一行\n第二行"}]}
        self.assertEqual(readable_body(request, "input"), "USER\n第一行\n第二行")
        response = {"content": "first\nsecond", "usage": {"input_tokens": 10}}
        self.assertEqual(readable_body(response, "output"), "first\nsecond")
        self.assertIn('"usage"', body_text(response)[0])
        self.assertEqual(readable_body({"result": "tool output"}, "output"), "tool output")
    def test_control_sequences_cjk_and_payload_limit(self):
        text, language = body_text("你好\x1b[2J\x1b]52;c;clipboard\x07🙂")
        self.assertEqual(text, "你好🙂")
        self.assertIsNone(language)
        self.assertEqual(body_text({"雪": [1]})[1], "json")
        self.assertLess(len(body_text("x" * (BODY_LIMIT + 200))[0]), BODY_LIMIT + 100)

    def test_cyclic_and_orphan_spans_remain_inspectable(self):
        spans = [dict(span_id="a", parent_span_id="b"), dict(span_id="b", parent_span_id="a"),
                 dict(span_id="orphan", parent_span_id="missing")]
        def ids(forest):
            return [span["span_id"] for span, _ in forest] + [n for _, nested in forest for n in ids(nested)]
        result = ids(span_forest(spans))
        self.assertEqual(sorted(result), ["a", "b", "orphan"])


@unittest.skipUnless(os.name == "posix", "PTY smoke test requires POSIX")
class TerminalTests(unittest.TestCase):
    def test_herdr_cell_mouse_clicks_work_without_an_initial_pixel_resize(self):
        import fcntl
        import json
        import pty
        import select
        import signal
        import struct
        import termios
        import time

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            launcher = Path(directory) / "probe.py"
            launcher.write_text(f'''
import json, runpy
from pathlib import Path
fixture = runpy.run_path({str(Path(__file__).resolve())!r})
class Probe(fixture["TraceApp"]):
    CSS_PATH = {str(ROOT / "cli/agenttrace_tui.tcss")!r}
    async def on_mount(self):
        self.set_interval(.05, self.report)
    def report(self):
        if not self.ready or not self.session_key:
            return
        regions = {{name: list(self.query_one("#" + name).region)
                   for name in ("pause", "sessions")}}
        Path({str(state_path)!r}).write_text(json.dumps(dict(
            live=self.live, session=self.session_key, regions=regions)))
Probe(fixture["FakeBackend"](), interval=3600).run()
''')
            primary, secondary = pty.openpty()
            fcntl.ioctl(secondary, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 176, 0, 0))
            process = subprocess.Popen(
                [sys.executable, str(launcher)], stdin=secondary, stdout=secondary,
                stderr=secondary, env={**os.environ, "TERM": "xterm-256color", "HERDR_ENV": "1"})
            os.close(secondary)
            output = bytearray()

            def wait_for(predicate):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if select.select([primary], [], [], .03)[0]:
                        output.extend(os.read(primary, 65536))
                    try:
                        state = json.loads(state_path.read_text())
                    except (OSError, ValueError):
                        continue
                    if predicate(state):
                        return state
                self.fail("terminal click did not reach the expected widget: " + bytes(output[-6000:]).decode(errors="replace"))

            def click(x, y):
                os.write(primary, f"\x1b[<0;{x+1};{y+1}M\x1b[<0;{x+1};{y+1}m".encode())

            try:
                state = wait_for(lambda s: s["session"] == ["a", "same"])
                self.assertIn(b"\x1b[?1006h", output)
                self.assertIn(b"\x1b[?1016l", output)
                self.assertNotIn(b"\x1b[?1016h", output)
                self.assertNotIn(b"\x1b[?2048$p", output)
                x, y, width, height = state["regions"]["pause"]
                click(x + width // 2, y + height // 2)
                wait_for(lambda s: not s["live"])
                time.sleep(.35)  # Let Textual's button activation debounce expire.
                click(x + width // 2, y + height // 2)
                state = wait_for(lambda s: s["live"])
                x, y, _, _ = state["regions"]["sessions"]
                click(x + 3, y + 3)
                wait_for(lambda s: s["session"] == ["b", "same"])
                # SIGWINCH remains the resize source; mouse clicks still work.
                fcntl.ioctl(primary, termios.TIOCSWINSZ, struct.pack("HHHH", 38, 160, 0, 0))
                os.kill(process.pid, signal.SIGWINCH)
                state = wait_for(lambda s: s["regions"]["pause"][0] != state["regions"]["pause"][0])
                x, y, width, height = state["regions"]["pause"]
                time.sleep(.35)  # Finish the resize paint and previous activation.
                click(x + width // 2, y + height // 2)
                wait_for(lambda s: not s["live"])
            finally:
                try:
                    os.write(primary, b"\x11")
                except OSError:
                    pass
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    if select.select([primary], [], [], .05)[0]:
                        try:
                            os.read(primary, 65536)
                        except OSError:
                            break
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                os.close(primary)

    def test_real_cli_starts_in_a_pty_and_restores_terminal_on_quit(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        import time

        with tempfile.TemporaryDirectory() as directory:
            primary, secondary = pty.openpty()
            fcntl.ioctl(secondary, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "cli/agenttrace.py"), "tui", "--no-index",
                 "--db", str(Path(directory) / "test.db")], stdin=secondary,
                stdout=secondary, stderr=secondary, env={**os.environ, "TERM": "xterm-256color"})
            os.close(secondary)
            output = b""
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and b"AGENT / TRACE" not in output:
                    if select.select([primary], [], [], .1)[0]:
                        output += os.read(primary, 65536)
                self.assertIn(b"AGENT / TRACE", output)
                os.write(primary, b"\x11")
                # Drain while it exits: a full terminal pipe must not block teardown.
                while process.poll() is None and time.monotonic() < deadline:
                    if select.select([primary], [], [], .1)[0]:
                        try:
                            output += os.read(primary, 65536)
                        except OSError:
                            break
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertIn(b"\x1b[?1049l", output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(primary)


if __name__ == "__main__":
    unittest.main()
