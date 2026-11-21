"""The worktree and its check evidence, with the trace archive one key away."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Static, TextArea
from textual.worker import Worker

from core.repo_review import repository_diff, repository_markdown
from core.review_tasks import NEXT_STEP, task_review_state
from herdr_review import PaneReview, herdr_request, task_source
from panel.text import sane

Evidence = tuple[str, str, str | None, str | None]


class TaskEditor(ModalScreen[tuple[str, bool] | None]):
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    def __init__(self, goal: str) -> None:
        super().__init__()
        self.goal = goal

    def compose(self) -> ComposeResult:
        with Vertical(id="task-editor"):
            yield Static("Worktree task goal")
            yield Input(self.goal, id="task-goal", max_length=240, placeholder="What should this worktree deliver?")
            with Horizontal(id="task-editor-actions"):
                yield Button("Save", id="task-save", variant="primary")
                if self.goal:
                    yield Button("Save and bind here", id="task-bind")
                yield Button("Cancel", id="task-cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed)
    def submit(self, event: Input.Submitted | Button.Pressed) -> None:
        event.stop()
        button = event.button.id if isinstance(event, Button.Pressed) else None
        if button == "task-cancel":
            self.dismiss()
        elif goal := self.query_one(Input).value.strip():
            self.dismiss((goal, button == "task-bind"))


class RepositoryScreen(ModalScreen[Evidence | None]):
    BINDINGS = [
        Binding("escape", "back", "Close review"),
        Binding("r", "refresh", "Refresh"),
        Binding("d", "view('files')", "Changes"),
        Binding("c", "view('checks')", "Checks"),
        Binding("t", "view('history')", "History"),
        Binding("b", "history_boundary", "Start here"),
        Binding("h", "handoff", "Copy handoff"),
        Binding("o", "source", "Open trace"),
        Binding("x", "copy_check", "Copy check command"),
        Binding("n", "edit_task", "Task"),
        Binding("v", "run_check", "Run check"),
        Binding("a", "task_agent", "Return to agent"),
        Binding("f", "copy_repair", "Copy repair request"),
    ]

    def __init__(self, root: Path, backend: Any, *, pane: PaneReview | None = None,
                 continue_task: bool = False) -> None:
        super().__init__()
        self.root = root
        self.backend = backend
        self.pane = pane
        self.continue_task = continue_task
        self.resume_pending = continue_task
        self._check_process: subprocess.Popen[bytes] | None = None
        self.chosen_session: tuple[str, str] | None = None
        self.review: dict[str, Any] | None = None
        self.mode = "files"
        self.selection = 0
        self.evidence: Evidence | None = None
        self.revision = 0
        self._worker: Worker | None = None
        self.from_turn: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="repo-main"):
            yield Static("Continue task" if self.continue_task else "Repository review", id="repo-heading")
            yield Static(sane(str(self.root)), id="repo-location", markup=False)
            if self.pane:
                yield Static(sane(self.pane.pane_id), id="repo-context", markup=False)
            yield Static("Reading Git changes and check evidence…", id="repo-result", markup=False)
            if self.continue_task:
                yield Static("Reading saved task…", id="repo-task", markup=False)
            with Horizontal(id="repo-controls"):
                yield Button("Changes", id="repo-files")
                yield Button("Checks", id="repo-checks")
                yield Button("History", id="repo-history")
                if self.continue_task:
                    yield Button("Task", id="repo-edit-task")
                else:
                    yield Button("Sessions", id="repo-sessions")
            with Vertical(id="repo-content"):
                yield DataTable(id="repo-items", cursor_type="row")
                yield TextArea(read_only=True, soft_wrap=False, show_line_numbers=False,
                               show_cursor=False, id="repo-detail", theme="css")
            help_text = ("n edits task · v runs check · f copies repair request · a returns to agent"
                         if self.continue_task else "Enter opens detail · o opens source trace · h copies handoff")
            yield Static(help_text,
                         id="repo-help", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_column("Changed files", key="item")
        self.query_one(DataTable).focus()
        self.action_refresh()
        self.set_interval(5, self._poll)

    def _poll(self) -> None:
        if self._check_process and self._check_process.poll() is not None:
            if self._check_process.returncode == 2:
                self.notify("Check could not verify this snapshot. See Checks for details.", severity="warning")
            self._check_process = None
        if self._worker is None or self._worker.is_finished:
            self.action_refresh()

    def action_refresh(self) -> None:
        self.revision += 1
        self._worker = self.run_worker(self._refresh(self.revision), group="repo-read",
                                       exclusive=True, exit_on_error=False)

    async def _refresh(self, revision: int, *, session: str | None = None,
                       agent: str | None = None) -> None:
        try:
            if self.continue_task and session is None:
                session, agent = self.chosen_session or (None, None)
            elif self.pane and session is None:
                session, agent = self.chosen_session or (self.pane.session_id, self.pane.agent)
                if session is None:
                    agent = None
            elif self.review and session is None and self.review["session_selected"]:
                selected = self.review["selected"]
                session, agent = selected["session_id"], selected["adapter_id"]
            options = {"auto_select_session": False, "allow_missing_session": True} if self.pane else {}
            if self.continue_task:
                options["include_task"] = True
            review = await asyncio.to_thread(self.backend.repository, self.root, session, agent, self.from_turn,
                                             **options)
        except Exception as exc:
            if revision == self.revision and self.is_mounted:
                self.query_one("#repo-result", Static).update(sane(f"Review unavailable · {exc}"))
            return
        if revision != self.revision or not self.is_mounted:
            return
        unchanged = self.review == review
        self.review = review
        identity = " · ".join([self.root.name, review["branch"] or "detached", (review["head"] or "unborn")[:12]])
        self.query_one("#repo-location", Static).update(sane(identity))
        if self.pane:
            task = review.get("task")
            source_pane, source_agent, source_session = (
                (task["pane_id"], task["agent"], task["session_id"]) if task
                else (self.pane.pane_id, self.pane.agent, self.pane.session_id))
            source_label = " · ".join(part for part in (source_pane, source_agent) if part)
            selected = review["selected"]
            if selected:
                binding = "Selected session" if self.chosen_session else ("Task session" if task else "Pane session")
                message = f"{binding} · {selected['adapter_id']} · {selected['session_id'][-22:]}"
            elif self.chosen_session or (source_session and (task or not self.continue_task)):
                message = "Session unavailable in this worktree · choose in History (t)"
            elif source_session:
                message = "Pane session will be saved with the task"
            else:
                message = "No native session ID · choose in History (t)"
            self.query_one("#repo-context", Static).update(sane(" · ".join(filter(None, (source_label, message)))))
        state = review["state"]
        color = {"failed": "#fda4af", "passed": "#86efac"}.get(state, "#ffd78a")
        text = f"{review['label']} · {len(review['files'])} changed files"
        if not review["staged_matches_worktree"]:
            text += " · unstaged or untracked changes"
        if review["unavailable"] or review["policy_error"]:
            text += " · " + (review["unavailable"] or review["policy_error"])
        self.query_one("#repo-result", Static).update(Text(sane(text), style=color))
        if self.continue_task:
            task = review["task"]
            step = task_review_state(review)
            message = f"{task['goal']} · {NEXT_STEP[step]}" if task else "No saved task · press n to set a goal"
            if task:
                live_source = await asyncio.to_thread(task_source, self.root, task)
                if revision != self.revision or not self.is_mounted:
                    return
                if live_source and live_source.status == "blocked":
                    message = f"{task['goal']} · Agent is waiting · press a to return"
            self.query_one("#repo-task", Static).update(sane(message))
            if task and self.resume_pending:
                self.resume_pending = False
                if review["checks"]:
                    self.mode = "checks"
                    self.selection = next((i for i, check in enumerate(review["checks"])
                                           if check["state"] == step), 0)
                    self._populate()
                    return
        if not unchanged:
            self._populate()

    def action_view(self, mode: str) -> None:
        self.mode = mode
        self.selection = 0
        self._populate()

    def _items(self) -> list[tuple[str, dict[str, Any]]]:
        if not self.review:
            return []
        if self.mode == "files":
            return [(f"{f['index']}{f['worktree']}  {f['path']}", f) for f in self.review["files"]]
        if self.mode == "checks":
            return [(f"{c['name']}  ·  {c['state']}" + ("  · required" if c["required"] else ""), c)
                    for c in self.review["checks"]]
        history = self.review["history"]
        sessions = [(f"Session · {s['adapter_id']} · {s['session_id'][-22:]}", {"session": s})
                    for s in self.review["sessions"]]
        turns = []
        for turn in history["turns"]:
            task = " ".join((turn["task"] or "Task text unavailable").split())
            turns.append((f"Turn · {task[:80]}", {"turn": turn}))
        return turns + sessions

    def _populate(self) -> None:
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.add_column({"files": "Changed files", "checks": "Checks on this snapshot",
                          "history": "Related session history"}[self.mode], key="item")
        self.evidence = None
        items = self._items()
        for label, _ in items:
            table.add_row(Text(sane(label)))
        if items:
            self.selection = min(self.selection, len(items) - 1)
            table.move_cursor(row=self.selection)
        messages = {
            "files": "Select a file and press Enter to see its diff." if items else "The worktree has no changes.",
            "checks": "Select a check and press Enter to see its recorded output." if items else
                      "Run agenttrace check NAME -- PROGRAM [ARG ...] to verify this code snapshot.",
            "history": ("Select a turn from the current session, or select another session below."
                        if self.review and self.review["selected"] else "Select a session to see its history.")
                       if items else "No indexed sessions match this worktree.",
        }
        self._detail(messages[self.mode])
        if self.continue_task and self.mode == "checks" and items:
            self._show_check(items[self.selection][1])
        table.focus()

    def _detail(self, value: str) -> None:
        widget = self.query_one(TextArea)
        widget.load_text(sane(value))
        widget.scroll_home(animate=False)

    @on(DataTable.RowSelected, "#repo-items")
    def selected(self, event: DataTable.RowSelected) -> None:
        items = self._items()
        if event.cursor_row >= len(items):
            return
        self.selection = event.cursor_row
        self.evidence = None
        _, item = items[self.selection]
        if self.mode == "files":
            self._detail("Reading diff…")
            self.run_worker(self._load_diff(item["path"]), group="repo-diff", exclusive=True, exit_on_error=False)
        elif self.mode == "checks":
            self._show_check(item)
        elif "turn" in item:
            turn = item["turn"]
            checks = turn["checks"]
            check = next((check for check in checks if check["state"] == "failed"), checks[0] if checks else None)
            self.evidence = (turn["adapter_id"], turn["session_id"], turn["trace_id"],
                             check["span_id"] if check else None)
            details = [turn["task"] or "Task text unavailable",
                       "\nAgent reported\n" + (turn["conclusion"] or "No conclusion recorded"),
                       "\nHistorical checks · code snapshot unavailable"]
            for check in checks:
                details.append(f"{check['state']} · {check['command']}\n{check.get('output_excerpt') or ''}")
            self._detail("\n".join(details))
        else:
            session = item["session"]
            self.from_turn = None
            if self.pane:
                self.chosen_session = (session["session_id"], session["adapter_id"])
                self.action_refresh()
            else:
                self.revision += 1
                self._worker = self.run_worker(
                    self._refresh(self.revision, session=session["session_id"], agent=session["adapter_id"]),
                    group="repo-read", exclusive=True, exit_on_error=False)

    def _show_check(self, check: dict[str, Any]) -> None:
        self._detail(f"{check['name']} · {check['state']}\n"
                     f"Run #{check.get('id') or '—'} · exit {check.get('exit_code')}\n"
                     f"Snapshot {(check.get('fingerprint') or 'unavailable')[:12]}\n"
                     f"{shlex.join(check.get('argv') or [])}\n\n"
                     f"{check.get('output') or 'No recorded output.'}")
        if check.get("adapter_id") and check.get("session_id"):
            self.evidence = (check["adapter_id"], check["session_id"], check.get("trace_id"), None)

    async def _load_diff(self, path: str) -> None:
        version, row = self.revision, self.selection
        try:
            parts = await asyncio.to_thread(repository_diff, self.root, path)
            text = "\n\n".join(f"{label.upper()}\n{patch}" for label, patch in parts.items() if patch)
            text = text or "This file has no textual diff."
        except (OSError, ValueError, RuntimeError) as exc:
            text = f"Diff unavailable · {exc}"
        if self.is_mounted and self.mode == "files" and self.selection == row and self.revision == version:
            self._detail(text)

    def action_source(self) -> None:
        if self.evidence:
            self.dismiss(self.evidence)

    def action_back(self) -> None:
        if self.pane:
            self.app.exit()
        else:
            self.dismiss()

    def action_history_boundary(self) -> None:
        if self.mode != "history" or not self.evidence or not self.evidence[2]:
            return
        agent, session, trace, _ = self.evidence
        self.from_turn = None if self.from_turn == trace else trace
        self.revision += 1
        self._worker = self.run_worker(self._refresh(self.revision, session=session, agent=agent),
                                       group="repo-read", exclusive=True, exit_on_error=False)

    def action_handoff(self) -> None:
        if self.review:
            self.app.copy_to_clipboard(sane(repository_markdown(self.review)))
            self.notify("Repository handoff copied")

    def action_copy_check(self) -> None:
        table = self.query_one(DataTable)
        items = self._items()
        if self.mode != "checks" or not 0 <= table.cursor_row < len(items):
            return
        check = items[table.cursor_row][1]
        command = ["agenttrace", "check", "--repo", str(self.root)]
        if self.pane and self.review and (selected := self.review["selected"]):
            command += ["--agent", selected["adapter_id"], "--session", selected["session_id"]]
        command.append(check["name"])
        # Only copy the application command for configured checks. Captured
        # arguments can be redacted, incomplete or no longer appropriate.
        if not check["configured"]:
            self.notify("Supply the intended command after -- in a shell")
            return
        self.app.copy_to_clipboard(shlex.join(command))
        self.notify("Check command copied")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("edit_task", "run_check", "task_agent", "copy_repair") and not self.continue_task:
            return False
        return super().check_action(action, parameters)

    def action_edit_task(self) -> None:
        if self.continue_task and self.review:
            task = self.review["task"]
            self.app.push_screen(TaskEditor(task["goal"] if task else ""), self._save_task)

    async def _save_task(self, answer: tuple[str, bool] | None) -> None:
        if not answer or not self.review or not self.pane:
            return
        goal, rebind = answer
        task = self.review["task"]
        if task and not rebind:
            pane_id, terminal_id = task["pane_id"], task["terminal_id"]
            agent, session = task["agent"], task["session_id"]
        else:
            session, agent = self.chosen_session or (self.pane.session_id, self.pane.agent)
            pane_id, terminal_id = self.pane.pane_id, self.pane.terminal_id
        try:
            await asyncio.to_thread(self.backend.save_worktree_task, self.root, self.review["checkout"], goal,
                                    pane_id=pane_id, terminal_id=terminal_id, agent=agent, session_id=session)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            self.notify(sane(str(exc)), severity="error")
            return
        self.notify("Task saved for this checkout")
        self.resume_pending = True
        self.action_refresh()

    async def action_task_agent(self) -> None:
        task = self.review.get("task") if self.review else None
        if task and (source := await asyncio.to_thread(task_source, self.root, task)):
            try:
                await asyncio.to_thread(herdr_request, "agent", "focus", source.pane_id)
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
            else:
                self.app.exit()
        else:
            self.notify("Source agent unavailable. Use History or bind this task to the current pane.",
                        severity="warning")

    def _selected_check(self) -> dict[str, Any] | None:
        row = self.query_one(DataTable).cursor_row
        checks = self.review["checks"] if self.review and self.mode == "checks" else []
        return checks[row] if 0 <= row < len(checks) else None

    def action_run_check(self) -> None:
        check = self._selected_check()
        if not self.continue_task or not self.review or not check or not check["configured"]:
            self.notify("Select a check defined in .agenttrace-review.json", severity="warning")
            return
        if self._check_process and self._check_process.poll() is None:
            self.notify("A check is already running", severity="warning")
            return
        command = [sys.executable, str(Path(__file__).with_name("agenttrace.py")), "check",
                   "--db", str(self.backend.db.resolve()), "--repo", str(self.root)]
        if selected := self.review["selected"]:
            command += ["--agent", selected["adapter_id"], "--session", selected["session_id"]]
        command.append(check["name"])
        try:
            self._check_process = subprocess.Popen(command, cwd=self.root, stdin=subprocess.DEVNULL,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                    start_new_session=True)
        except OSError as exc:
            self.notify(sane(str(exc)), severity="error")
            return
        self.notify(f"Running {check['name']} · safe to close this popup")

    def action_copy_repair(self) -> None:
        check = self._selected_check()
        task = self.review.get("task") if self.review else None
        if task and check and check["state"] == "failed":
            message = sane(
                f"Continue this task: {task['goal']}\n"
                f"Investigate the failed check {check['name']} (exit {check['exit_code']}).\n"
                f"Command: {shlex.join(check.get('argv') or [])}\n"
                f"Snapshot: {check['fingerprint']}\n\n{check.get('output') or 'No recorded output.'}")
            self._detail(message)
            self.app.copy_to_clipboard(message)
            self.notify("Repair request copied")
        else:
            self.notify("Select a current failed check for this task", severity="warning")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        mode = (event.button.id or "").removeprefix("repo-")
        if mode == "sessions":
            self.dismiss()
        elif mode == "edit-task":
            self.action_edit_task()
        elif mode in {"files", "checks", "history"}:
            self.action_view(mode)
