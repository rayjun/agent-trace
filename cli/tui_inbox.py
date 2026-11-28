"""Find active and archived tasks across worktrees."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Static, TextArea

from core.review_tasks import ArchivedTask, TaskItem
from panel.text import sane
from tui_data import LocalBackend

InboxItem = TaskItem | ArchivedTask


class ArchivedTaskDialog(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("c", "copy", "Copy handoff")]

    def __init__(self, handoff: str) -> None:
        super().__init__()
        self.handoff = sane(handoff)

    def compose(self) -> ComposeResult:
        with Vertical(id="archived-task-dialog"):
            yield Static("Archived task", id="archived-task-heading")
            yield TextArea(self.handoff, id="archive-handoff", read_only=True, soft_wrap=True, show_cursor=False)
            with Horizontal(id="archived-task-actions"):
                yield Button("Copy handoff", id="archive-copy", variant="primary")
                yield Button("Close", id="archive-close")
        yield Footer()

    @on(Button.Pressed, "#archive-copy")
    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.handoff)
        self.notify("Archived handoff copied")

    @on(Button.Pressed, "#archive-close")
    def close_dialog(self) -> None:
        self.dismiss()


class TaskInbox(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "back", "Close inbox"),
        Binding("/", "search", "Search"),
        Binding("r", "reload", "Refresh"),
        Binding("ctrl+t", "toggle_archive", "Active / archived"),
    ]

    def __init__(self, backend: LocalBackend, open_task: Callable[[TaskItem], None], *, standalone: bool = False) -> None:
        super().__init__()
        self.backend = backend
        self.open_task = open_task
        self.standalone = standalone
        self.tasks: list[InboxItem] = []
        self.visible_tasks: list[InboxItem] = []
        self.show_archived = False
        self._loading = True
        self._preview_item: InboxItem | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="inbox"):
            yield Static("Task inbox", id="inbox-heading")
            with Horizontal(id="inbox-tabs"):
                yield Button("Active", id="inbox-active", variant="primary")
                yield Button("Archived", id="inbox-archived")
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
        self.run_worker(self._load(self.show_archived), group="inbox-list", exclusive=True, exit_on_error=False)

    async def _load(self, archived: bool) -> None:
        load: Callable[[], Sequence[InboxItem]] = self.backend.archived_tasks if archived else self.backend.tasks
        try:
            items = list(await asyncio.to_thread(load))
        except Exception as exc:
            self._loading = False
            self._preview_selected()
            self.query_one("#inbox-next", Static).update(sane(exc))
            return
        if self.show_archived != archived:
            return
        self.tasks = items
        self._loading = False
        self._render_tasks()

    def _selected(self) -> InboxItem | None:
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
            location = f"{item.root.name} · {item.checkout_label}"
            if isinstance(item, ArchivedTask):
                location += f" · {item.archived_at[:10]}"
            table.add_row(Text(sane(item.goal), no_wrap=True, overflow="ellipsis"),
                          Text(sane(location)))
            if selected is not None and self._key(item) == self._key(selected):
                cursor = index
        table.move_cursor(row=cursor)
        self._preview_item = None
        self._preview_selected()

    @staticmethod
    def _key(item: InboxItem) -> tuple[str, int] | tuple[str, str, str, int]:
        if isinstance(item, ArchivedTask):
            return ("archive", item.id)
        return ("active", str(item.root), item.checkout, item.generation)

    @staticmethod
    def _search_text(task: InboxItem) -> str:
        summary = task.summary if isinstance(task, ArchivedTask) else ""
        return " ".join((task.goal, str(task.root), task.checkout_label, task.checkpoint, summary)).casefold()

    def _preview_selected(self) -> None:
        item = self._selected()
        context = self.query_one("#inbox-context", Static)
        status = self.query_one("#inbox-next", Static)
        button = self.query_one("#inbox-resume", Button)
        if not item:
            self.workers.cancel_group(self, "inbox-preview")
            if self._loading:
                message = "Loading archived tasks…" if self.show_archived else "Loading active tasks…"
            elif self.tasks:
                message = "No matching tasks."
            elif self.show_archived:
                message = "No archived tasks."
            else:
                message = "No active tasks. Start one with agenttrace work."
            context.update(message)
            status.update("")
            button.disabled = True
        elif item != self._preview_item:
            self._preview_item = item
            context.update(sane(f"{item.root} · {item.checkout_label}\n{item.checkpoint or 'No checkpoint saved'}"))
            if isinstance(item, ArchivedTask):
                stamp = item.archived_at.replace("T", " ")[:16]
                summary = item.summary.splitlines()[0] if item.summary else "Verification unavailable"
                status.update(sane(f"Archived {stamp} · {summary}"))
                button.disabled = False
            else:
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

    async def _open_selected(self) -> None:
        if not (item := self._selected()):
            return
        self.workers.cancel_group(self, "inbox-preview")
        try:
            if isinstance(item, ArchivedTask):
                handoff = await asyncio.to_thread(self.backend.archived_handoff, item)
            else:
                await asyncio.to_thread(self.backend.resume_task, item)
        except Exception as exc:
            self.query_one("#inbox-next", Static).update(sane(exc))
            self.query_one("#inbox-resume", Button).disabled = True
            return
        if self.app.screen is self and self._selected() == item:
            if isinstance(item, ArchivedTask):
                self.app.push_screen(ArchivedTaskDialog(handoff))
            else:
                self.open_task(item)

    def _set_archive(self, archived: bool) -> None:
        if self.show_archived == archived:
            return
        self.show_archived = archived
        self.query_one("#inbox-active", Button).variant = "default" if archived else "primary"
        self.query_one("#inbox-archived", Button).variant = "primary" if archived else "default"
        self.query_one("#inbox-resume", Button).label = "Open archive" if archived else "Resume task"
        self._loading = True
        self.tasks = []
        self._render_tasks()
        self.query_one(DataTable).focus()
        self.action_reload()

    def action_toggle_archive(self) -> None:
        self._set_archive(not self.show_archived)

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
    async def open_selected(self, event: DataTable.RowSelected | Button.Pressed) -> None:
        event.stop()
        await self._open_selected()

    @on(Button.Pressed, "#inbox-active")
    @on(Button.Pressed, "#inbox-archived")
    def select_list(self, event: Button.Pressed) -> None:
        event.stop()
        self._set_archive(event.button.id == "inbox-archived")

    def action_search(self) -> None:
        self.query_one(Input).focus()

    def action_back(self) -> None:
        if self.standalone:
            self.app.exit()
        else:
            self.dismiss()
