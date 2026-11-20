"""The worktree and its check evidence, with the trace archive one key away."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shlex
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Static, TextArea
from textual.worker import Worker

from core.repo_review import repository_diff, repository_markdown
from panel.text import sane

Evidence = tuple[str, str, str | None, str | None]


class RepositoryScreen(ModalScreen[Evidence | None]):
    BINDINGS = [
        Binding("escape", "dismiss", "Sessions"),
        Binding("r", "refresh", "Refresh"),
        Binding("d", "view('files')", "Changes"),
        Binding("c", "view('checks')", "Checks"),
        Binding("t", "view('history')", "History"),
        Binding("b", "history_boundary", "Start here"),
        Binding("h", "handoff", "Copy handoff"),
        Binding("o", "source", "Open trace"),
        Binding("x", "copy_check", "Copy check command"),
    ]

    def __init__(self, root: Path, backend: Any) -> None:
        super().__init__()
        self.root = root
        self.backend = backend
        self.review: dict[str, Any] | None = None
        self.mode = "files"
        self.selection = 0
        self.evidence: Evidence | None = None
        self.revision = 0
        self._worker: Worker | None = None
        self.from_turn: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="repo-main"):
            yield Static("Repository review", id="repo-heading")
            yield Static(sane(str(self.root)), id="repo-location", markup=False)
            yield Static("Reading Git changes and check evidence…", id="repo-result", markup=False)
            with Horizontal(id="repo-controls"):
                yield Button("Changes", id="repo-files")
                yield Button("Checks", id="repo-checks")
                yield Button("History", id="repo-history")
                yield Button("Sessions", id="repo-sessions")
            with Vertical(id="repo-content"):
                yield DataTable(id="repo-items", cursor_type="row")
                yield TextArea(read_only=True, soft_wrap=False, show_line_numbers=False,
                               show_cursor=False, id="repo-detail", theme="css")
            yield Static("Enter opens detail · o opens source trace · h copies handoff",
                         id="repo-help", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_column("Changed files", key="item")
        self.query_one(DataTable).focus()
        self.action_refresh()
        self.set_interval(5, self._poll)

    def _poll(self) -> None:
        if self._worker is None or self._worker.is_finished:
            self.action_refresh()

    def action_refresh(self) -> None:
        self.revision += 1
        self._worker = self.run_worker(self._refresh(self.revision), group="repo-read",
                                       exclusive=True, exit_on_error=False)

    async def _refresh(self, revision: int, *, session: str | None = None,
                       agent: str | None = None) -> None:
        try:
            if self.review and session is None and self.review["session_selected"]:
                selected = self.review["selected"]
                session, agent = selected["session_id"], selected["adapter_id"]
            review = await asyncio.to_thread(self.backend.repository, self.root, session, agent, self.from_turn)
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
        state = review["state"]
        color = {"failed": "#fda4af", "passed": "#86efac"}.get(state, "#ffd78a")
        text = f"{review['label']} · {len(review['files'])} changed files"
        if not review["staged_matches_worktree"]:
            text += " · unstaged or untracked changes"
        if review["unavailable"] or review["policy_error"]:
            text += " · " + (review["unavailable"] or review["policy_error"])
        self.query_one("#repo-result", Static).update(Text(sane(text), style=color))
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
            "history": "Select a turn from the current session, or select another session below." if items else
                       "No indexed sessions match this worktree. Refresh capture with agenttrace review --refresh.",
        }
        self._detail(messages[self.mode])
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
            self._detail(f"{item['name']} · {item['state']}\n"
                         f"Run #{item.get('id') or '—'} · exit {item.get('exit_code')}\n"
                         f"Snapshot {(item.get('fingerprint') or 'unavailable')[:12]}\n"
                         f"{shlex.join(item.get('argv') or [])}\n\n"
                         f"{item.get('output') or 'No recorded output.'}")
            if item.get("adapter_id") and item.get("session_id"):
                self.evidence = (item["adapter_id"], item["session_id"], item.get("trace_id"), None)
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
            self.revision += 1
            self._worker = self.run_worker(
                self._refresh(self.revision, session=session["session_id"], agent=session["adapter_id"]),
                group="repo-read", exclusive=True, exit_on_error=False)

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
        command = ["agenttrace", "check", "--repo", str(self.root), check["name"]]
        # Only copy the application command for configured checks. Captured
        # arguments can be redacted, incomplete or no longer appropriate.
        if not check["configured"]:
            self.notify("Supply the intended command after -- in a shell")
            return
        self.app.copy_to_clipboard(shlex.join(command))
        self.notify("Check command copied")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        mode = (event.button.id or "").removeprefix("repo-")
        if mode == "sessions":
            self.dismiss()
        elif mode in {"files", "checks", "history"}:
            self.action_view(mode)
