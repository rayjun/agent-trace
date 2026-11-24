"""Browse active tasks across the local archive."""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Static

from core.review_tasks import TaskItem
from panel.text import sane
from tui_data import LocalBackend


class TaskInbox(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "back", "Close inbox"),
        Binding("/", "search", "Search"),
        Binding("r", "reload", "Refresh"),
    ]

    def __init__(self, backend: LocalBackend, open_task: Callable[[TaskItem], None], *, standalone: bool = False) -> None:
        super().__init__()
        self.backend = backend
        self.open_task = open_task
        self.standalone = standalone
        self.tasks: list[TaskItem] = []
        self.visible_tasks: list[TaskItem] = []
        self._preview_item: TaskItem | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="inbox"):
            yield Static("Task inbox", id="inbox-heading")
            yield Input(placeholder="Filter by goal, worktree, branch or checkpoint", id="inbox-search")
            yield DataTable(id="inbox-items", cursor_type="row", zebra_stripes=True)
            yield Static("Loading saved tasks…", id="inbox-context", markup=False)
            yield Static("", id="inbox-next", markup=False)
            with Horizontal(id="inbox-actions"):
                yield Button("Resume task", id="inbox-resume", variant="primary", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Task", width=max(24, min(54, self.size.width // 2)))
        table.add_column("Worktree · branch")
        table.focus()
        self.action_reload()

    def on_screen_resume(self) -> None:
        if self.is_mounted:
            self.action_reload()

    def action_reload(self) -> None:
        self.run_worker(self._load(), group="inbox-list", exclusive=True, exit_on_error=False)

    async def _load(self) -> None:
        try:
            self.tasks = await asyncio.to_thread(self.backend.tasks)
        except Exception as exc:
            self.query_one("#inbox-next", Static).update(sane(exc))
            return
        self._render_tasks()

    def _selected(self) -> TaskItem | None:
        index = self.query_one(DataTable).cursor_row
        return self.visible_tasks[index] if 0 <= index < len(self.visible_tasks) else None

    def _render_tasks(self) -> None:
        selected = self._selected()
        query = self.query_one(Input).value.casefold().split()
        table = self.query_one(DataTable)
        self.visible_tasks = []
        for task in self.tasks:
            text = self._search_text(task)
            if all(part in text for part in query):
                self.visible_tasks.append(task)
        table.clear()
        cursor = 0
        for index, item in enumerate(self.visible_tasks):
            table.add_row(Text(sane(item.goal), no_wrap=True, overflow="ellipsis"),
                          Text(sane(f"{item.root.name} · {item.checkout_label}")))
            if selected and (item.root, item.checkout, item.generation) == (
                selected.root, selected.checkout, selected.generation
            ):
                cursor = index
        table.move_cursor(row=cursor)
        self._preview_item = None
        self._preview_selected()

    @staticmethod
    def _search_text(task: TaskItem) -> str:
        return " ".join((task.goal, str(task.root), task.checkout_label, task.checkpoint)).casefold()

    def _preview_selected(self) -> None:
        item = self._selected()
        context = self.query_one("#inbox-context", Static)
        status = self.query_one("#inbox-next", Static)
        button = self.query_one("#inbox-resume", Button)
        if not item:
            self.workers.cancel_group(self, "inbox-preview")
            context.update("No matching tasks." if self.tasks else "No active tasks. Start one with agenttrace work.")
            status.update("")
            button.disabled = True
        elif item != self._preview_item:
            self._preview_item = item
            context.update(sane(f"{item.root} · {item.checkout_label}\n{item.checkpoint or 'No checkpoint saved'}"))
            status.update("Reading current checkout…")
            button.disabled = True
            self.run_worker(self._preview(item), group="inbox-preview", exclusive=True, exit_on_error=False)

    async def _preview(self, item: TaskItem) -> None:
        try:
            status = await asyncio.to_thread(self.backend.task_next_step, item)
        except Exception as exc:
            status = str(exc)
        else:
            if self._selected() == item:
                self.query_one("#inbox-resume", Button).disabled = False
        if self._selected() == item:
            self.query_one("#inbox-next", Static).update(sane(status))

    async def _resume(self) -> None:
        if not (item := self._selected()):
            return
        self.workers.cancel_group(self, "inbox-preview")
        try:
            await asyncio.to_thread(self.backend.resume_task, item)
        except Exception as exc:
            self.query_one("#inbox-next", Static).update(sane(exc))
            self.query_one("#inbox-resume", Button).disabled = True
            return
        if self.app.screen is self and self._selected() == item:
            self.open_task(item)

    @on(Input.Changed, "#inbox-search")
    def filter_tasks(self) -> None:
        self._render_tasks()

    @on(Input.Submitted, "#inbox-search")
    def focus_results(self) -> None:
        self.query_one(DataTable).focus()

    @on(DataTable.RowHighlighted, "#inbox-items")
    def highlight(self) -> None:
        self._preview_selected()

    @on(DataTable.RowSelected, "#inbox-items")
    @on(Button.Pressed, "#inbox-resume")
    async def resume(self, event: DataTable.RowSelected | Button.Pressed) -> None:
        event.stop()
        await self._resume()

    def action_search(self) -> None:
        self.query_one(Input).focus()

    def action_back(self) -> None:
        if self.standalone:
            self.app.exit()
        else:
            self.dismiss()
