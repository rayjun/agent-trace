"""Textual observability console. Launched by ``agenttrace tui``."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.driver import Driver
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Input, LoadingIndicator,
                             Select, Sparkline, Static, TabbedContent, TabPane,
                             TextArea, Tree)
from textual.widgets.tree import TreeNode

from panel.text import sane
from agenttrace_common import flatten_content
from herdr_review import PaneReview
from tui_data import PAGE_SIZE, LocalBackend, SessionKey
from core.review_tasks import TaskItem
from tui_inbox import TaskInbox
from tui_repository import Evidence, RepositoryScreen

CYAN = "#7dd3fc"
VIOLET = "#b6a0fa"
MUTED = "#94a3bd"
GREEN = "#86efac"
RED = "#fda4af"
BODY_LIMIT = 100_000


def compact(value: int | float | None) -> str:
    if value is None:
        return "—"
    for scale, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(value) >= scale:
            return f"{value / scale:.1f}{suffix}"
    return f"{value:g}"


def duration(ms: int | float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:g}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60_000:.1f}m"


def body_text(value: Any) -> tuple[str, str | None]:
    if value is None:
        return "Not captured in this span.", None
    structured = not isinstance(value, str)
    text = json.dumps(value, ensure_ascii=False, indent=2) if structured else value
    text = sane(text)
    if len(text) > BODY_LIMIT:
        text = text[:BODY_LIMIT] + "\n\n… Preview limited to 100,000 characters."
    return text, "json" if structured else None


def readable_body(value: Any, section: str) -> Any:
    """Show recorded conversation text naturally; the JSON tab retains structure."""
    if section == "events" and isinstance(value, list):
        return "\n\n".join(
            f"{e.get('ts', '')} · {e.get('name', 'event')}\n"
            + body_text(readable_body(e.get("attributes"), "output"))[0] for e in value)
    if section == "input":
        messages = value.get("messages") if isinstance(value, dict) else value
        if isinstance(messages, list) and messages and all(isinstance(m, dict) and "role" in m for m in messages):
            blocks = []
            if isinstance(value, dict):
                system = value.get("system_prompt") or value.get("instructions")
                if system:
                    blocks.append("SYSTEM\n" + flatten_content(system))
            for message in messages:
                content = flatten_content(message.get("content"))
                if not content and message.get("content") is not None:
                    content = body_text(message["content"])[0]
                blocks.append(str(message["role"]).upper() + "\n" + content)
                if message.get("tool_calls"):
                    blocks.append("TOOL CALLS\n" + body_text(message["tool_calls"])[0])
            if isinstance(value, dict) and value.get("tools"):
                blocks.append("TOOLS\n" + body_text(value["tools"])[0])
            return "\n\n".join(blocks)
    if section == "output" and isinstance(value, dict):
        parts = []
        if value.get("reasoning"):
            parts.append("REASONING\n" + flatten_content(value["reasoning"]))
        for field in ("content", "result", "text"):
            if value.get(field) is not None:
                content = flatten_content(value[field])
                parts.append(content if content else body_text(value[field])[0])
                break
        if value.get("tool_calls"):
            parts.append("TOOL CALLS\n" + body_text(value["tool_calls"])[0])
        if parts:
            return "\n\n".join(parts)
    return value


@dataclass(frozen=True)
class NodeRef:
    trace_id: str
    span_id: str | None = None


def span_forest(spans: list[dict]) -> list[tuple[dict, list]]:
    """Keep orphan/cyclic spans inspectable without recursive parent loops."""
    by_id = {s["span_id"]: s for s in spans}
    children: dict[str, list[dict]] = {}
    roots = []
    for span in spans:
        parent = span.get("parent_span_id")
        if isinstance(parent, str) and parent in by_id and parent != span["span_id"]:
            children.setdefault(parent, []).append(span)
        else:
            roots.append(span)
    visited: set[str] = set()

    def branch(span: dict, depth: int = 0) -> tuple[dict, list]:
        visited.add(span["span_id"])
        # Extremely deep adapter trees are flattened rather than exhausting the
        # Python stack (or generating unusable indentation in the terminal).
        nested = [] if depth >= 40 else [branch(s, depth + 1)
                 for s in children.get(span["span_id"], []) if s["span_id"] not in visited]
        return span, nested

    forest = [branch(s) for s in roots if s["span_id"] not in visited]
    for span in spans:
        if span["span_id"] not in visited:
            forest.append(branch(span))
    return forest


class Metric(Vertical):
    def __init__(self, label: str, name: str) -> None:
        super().__init__(id=name, classes="metric")
        self.label = label

    def compose(self) -> ComposeResult:
        yield Static(self.label, classes="metric-label")
        yield Static("—", classes="metric-value")
        yield Static("Waiting for local data", classes="metric-caption")
        yield Sparkline([], classes="metric-chart", min_color="#334155", max_color=CYAN)

    def set_value(self, value: str, caption: str, data: list[float]) -> None:
        self.query_one(".metric-value", Static).update(Text(value))
        self.query_one(".metric-caption", Static).update(Text(caption))
        self.query_one(Sparkline).data = data


class HandoffScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = sane(text)

    def compose(self) -> ComposeResult:
        with Vertical(id="handoff-dialog"):
            yield Static("HANDOFF · recorded evidence, not an instruction to execute", id="handoff-title")
            yield TextArea(self.text, read_only=True, show_line_numbers=False,
                           show_cursor=False, soft_wrap=True, id="handoff-text")
            with Horizontal(id="handoff-actions"):
                yield Button("Copy brief", id="handoff-copy")
                yield Button("Close", id="handoff-close")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "handoff-copy":
            self.app.copy_to_clipboard(self.text)
            self.app.notify("Brief sent to the terminal clipboard")
        else:
            self.dismiss()


class TraceApp(App):
    TITLE = "agent-trace"
    SUB_TITLE = "Local observability"
    AUTO_FOCUS = "#sessions"
    CSS_PATH = "agenttrace_tui.tcss"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("q", "quit", "Quit", show=False),
        Binding("space", "pause", "Pause"),
        Binding("r", "refresh", "Refresh"),
        Binding("/", "search", "Search"),
        Binding("ctrl+l", "clear_filters", "Show all", priority=True),
        Binding("e", "review_evidence", "Evidence"),
        Binding("h", "handoff", "Handoff"),
        Binding("m", "metrics", "Metrics", show=False),
        Binding("w", "repository", "Worktree"),
        Binding("i", "task_inbox", "Tasks"),
        Binding("1", "panel('sessions')", "Sessions"),
        Binding("2", "panel('traces')", "Trace"),
        Binding("3", "panel('detail')", "Details"),
        Binding("escape", "navigate_back", "Back", show=False),
    ]

    def get_driver_class(self) -> type[Driver]:
        driver = super().get_driver_class()
        if self.herdr_target or os.environ.get("HERDR_ENV") == "1":
            from textual.drivers.linux_driver import LinuxDriver
            if driver is LinuxDriver:
                from tui_terminal import HerdrDriver
                return HerdrDriver
        return driver

    def __init__(self, backend: Any, *, agent: str | None = None,
                 session: str | None = None, interval: float = 2.5,
                 repository: Path | None = None, open_repository: bool = False,
                 herdr_target: PaneReview | None = None, continue_task: bool = False,
                 open_inbox: bool = False) -> None:
        self.herdr_target = herdr_target
        super().__init__()
        self.backend = backend
        self.interval = interval
        self.repository_root = repository
        self.open_repository = open_repository
        self.open_inbox = open_inbox
        self.continue_task = continue_task
        self.agent_filter = agent
        self.initial_session = session
        self.live = True
        self.ready = False
        self._starting = False
        self.closed = False
        self._refreshing = False
        self._filter_revision = 0
        self._display_epoch = 0
        self._session_epoch = 0
        self._span_epoch = 0
        self.session_key: SessionKey | None = None
        self.span_ref: NodeRef | None = None
        self.session_offset = 0
        self.turn_offset = 0
        self._session_keys: list[str] = []
        self._session_rows: dict[str, dict] = {}
        self._agent_options: tuple[str, ...] = ()
        self._turn_nodes: dict[str, TreeNode] = {}
        self._trace_cache: dict[str, list[dict]] = {}
        self._trace_requests: dict[tuple[SessionKey, int, str], asyncio.Task] = {}
        self._search_timer: Any = None
        self._active_panel = "sessions"
        self._last_dashboard: dict = {}
        self._last_success: float | None = None
        self._request_started = 0.0
        self._review: dict | None = None
        self._metrics_visible = False
        self._review_primary = True
        self._review_navigation = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            yield Static("◈ AGENT / TRACE", id="brand")
            yield Static("WORK REVIEW · evidence before acceptance", id="tagline")
            yield LoadingIndicator(id="opening")
            yield Static("◌ OPENING", id="live")
        with Horizontal(id="controls"):
            yield Input(value=self.initial_session or "", placeholder="Search sessions, models, folders…  /", id="search")
            options = [("All agents", "*")]
            if self.agent_filter:
                options.append((self.agent_filter, self.agent_filter))
            yield Select(options, allow_blank=False, value=self.agent_filter or "*", id="agent")
            yield Button("Ⅱ Pause", id="pause", variant="default")
            yield Button("↻", id="refresh")
        with Horizontal(id="filters"):
            yield Static("All sessions", id="filter-summary")
            yield Button("× Show all", id="clear-filters")
        with Vertical(id="work-summary"):
            with Horizontal(id="review-actions"):
                yield Static("Select work to review", id="review-state")
                yield Button("e  Evidence", id="review-evidence", disabled=True)
                yield Button("h  Handoff", id="handoff", disabled=True)
            yield Static("Recent sessions across your agents. Select one to see task, conclusion and check evidence.", id="review-task")
            yield Static("No result inferred from missing data.", id="review-conclusion")
            yield Static("m: archive metrics · Enter: drill into calls", id="review-next")
        with Horizontal(id="metrics"):
            yield Metric("TOKENS", "tokens")
            yield Metric("TURN P50", "latency")
            yield Metric("CACHE HIT", "cache")
            yield Metric("SPAN ERRORS", "errors")
        with Horizontal(id="body"):
            with Vertical(id="sessions-panel", classes="panel"):
                yield Static("01  RECENT WORK", classes="panel-title")
                yield DataTable(id="sessions", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="pager"):
                    yield Button("‹", id="sessions-prev")
                    yield Static("—", id="sessions-page")
                    yield Button("›", id="sessions-next")
            with Vertical(id="traces-panel", classes="panel"):
                yield Static("02  EXECUTION TREE", classes="panel-title")
                yield Static("Select a session", id="scope", classes="muted")
                yield Tree("Turns", id="traces")
                with Horizontal(classes="pager"):
                    yield Button("‹", id="turns-prev")
                    yield Static("—", id="turns-page")
                    yield Button("›", id="turns-next")
            with Vertical(id="detail-panel", classes="panel"):
                yield Static("03  REVIEW / CALL DETAILS", classes="panel-title")
                yield Static("Select a call to inspect", id="span-summary")
                with TabbedContent(initial="tab-review", id="details"):
                    for label, name in (("Review", "review"), ("Input", "input"), ("Output", "output"),
                                        ("Error", "error"), ("JSON", "json"),
                                        ("Events", "events"), ("Info", "info")):
                        with TabPane(label, id="tab-" + name):
                            yield TextArea("Select a session, expand a turn, then choose a call.",
                                           id="field-" + name, read_only=True, soft_wrap=True,
                                           show_line_numbers=False, show_cursor=False,
                                           theme="css")
        yield Static("Opening your local archive…", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#sessions", DataTable)
        table.add_column("AGENT / SESSION", key="identity", width=25)
        table.add_column("TURNS", key="turns", width=5)
        self._render_filters()
        self.query_one("#metrics").display = self._metrics_visible
        self.query_one("#traces", Tree).show_root = False
        self._layout()
        self.set_interval(self.interval, self._tick)
        self.run_worker(self._initialize(), group="startup", exit_on_error=False)

    async def _initialize(self) -> None:
        if self._starting:
            return
        self._starting = True
        try:
            await asyncio.to_thread(self.backend.start)
            if self.closed:
                self.backend.close()
                return
            self.ready = True
            self.query_one("#opening").display = False
            if self.open_inbox:
                self.open_inbox = False
                self.call_after_refresh(self.action_task_inbox, True)
                return
            await self._refresh()
            if self.open_repository:
                self.open_repository = False
                self.action_repository()
        except Exception as exc:
            self.query_one("#opening").display = False
            self._error(exc)
        finally:
            self._starting = False

    def on_unmount(self) -> None:
        self.closed = True
        self.backend.close()

    def on_resize(self, event: Resize) -> None:
        if self.screen_stack:
            self._layout(event.size.width, event.size.height)

    def _layout(self, width: int | None = None, height: int | None = None) -> None:
        width = self.size.width if width is None else width
        height = self.size.height if height is None else height
        self.screen.set_class(width < 125, "medium")
        self.screen.set_class(width < 85, "narrow")
        self.screen.set_class(height < 35, "short")
        for panel in ("sessions", "traces", "detail"):
            self.screen.set_class(self._active_panel == panel, "view-" + panel)

    def _tick(self) -> None:
        if isinstance(self.screen, TaskInbox):
            return
        if self.ready and self.live and not self._refreshing:
            self.run_worker(self._refresh(), group="refresh", exit_on_error=False)
        elif self.ready and self.live and self._last_success and (
                time.monotonic() - self._request_started > max(10, self.interval * 3)):
            self._error(TimeoutError("Refresh delayed; showing the previous snapshot"))

    def _error(self, exc: Exception) -> None:
        if self.closed:
            return
        age = f" · last good snapshot {int(time.time() - self._last_success)}s ago" if self._last_success else ""
        self.query_one("#status", Static).update(Text("STALE · " + sane(exc) + age, style=RED))
        self.query_one("#live", Static).update(Text("● STALE", style=RED))

    async def _refresh(self) -> None:
        if self._refreshing or not self.ready:
            return
        self._refreshing = True
        self._request_started = time.monotonic()
        revision = self._filter_revision
        display_epoch = self._display_epoch
        initial = self.initial_session
        session_epoch = self._session_epoch
        try:
            search = self.query_one("#search", Input).value
            data = await asyncio.to_thread(self.backend.dashboard, search,
                                           self.agent_filter, self.session_offset)
            resolved = None
            resolution_error = None
            if initial is not None:
                # Explicit IDs are exact selectors, independent of search pages.
                try:
                    resolved = await asyncio.to_thread(self.backend.resolve_session, initial, self.agent_filter)
                except ValueError:
                    resolution_error = "This session ID belongs to multiple agents. Select an agent or choose a row."
                except LookupError:
                    resolution_error = "The requested session ID was not found. Choose a row or change the search."
            if self.closed or revision != self._filter_revision or display_epoch != self._display_epoch:
                return
            selection_changed = initial != self.initial_session or session_epoch != self._session_epoch
            if resolved is not None and not selection_changed:
                self._select_session(resolved)
            self._last_dashboard = data
            if not data["sessions"]["sessions"] and self.session_offset > 0:
                # Deletions can remove the last page while we are viewing it.
                total = data["sessions"]["total"]
                self.session_offset = max(0, ((max(1, total) - 1) // PAGE_SIZE) * PAGE_SIZE)
                self._filter_revision += 1
                return
            self._render_dashboard(data)
            self._render_filters(data["sessions"]["total"], data["overview"]["sessions"])
            self._render_sessions(data["sessions"], allow_auto_select=not selection_changed)
            if resolution_error and not selection_changed:
                self._clear_session()
                self._show_info(resolution_error)
            if self.session_key is not None:
                await self._load_session(self.session_key, self._session_epoch)
            self._last_success = time.time()
        except Exception as exc:
            self._error(exc)
        finally:
            self._refreshing = False
            if not self.closed and revision != self._filter_revision:
                self.call_later(self.action_refresh)

    def _render_dashboard(self, data: dict) -> None:
        overview, health = data["overview"], data["health"]
        recent = list(reversed(data.get("recent", [])))
        tokens = overview["tokens"]
        known = data.get("usage_known", True)
        total_tokens = (tokens.get("input") or 0) + (tokens.get("output") or 0)
        self.query_one("#tokens", Metric).set_value(
            compact(total_tokens) if known else "—",
            f"{compact(tokens.get('input'))} in · {compact(tokens.get('output'))} out · all agents" if known else "Token usage not captured",
            [(r["tokens"].get("input") or 0) + (r["tokens"].get("output") or 0) for r in recent])
        durations = [r["duration_ms"] for r in recent
                     if r.get("duration_ms") is not None and r.get("unclosed") == 0]
        self.query_one("#latency", Metric).set_value(
            duration(overview["task_duration_ms"]["p50"]), "Stored turn duration · all agents", durations)
        cache = tokens.get("cache_read")
        input_tokens = tokens.get("input")
        ratio = min(100, 100 * cache / input_tokens) if known and data.get("cache_known", True) and input_tokens and cache is not None else None
        self.query_one("#cache", Metric).set_value(
            f"{ratio:.1f}%" if ratio is not None else "—", "Cache read / input · all agents",
            [min(100, 100 * (r["tokens"].get("cache_read") or 0) / r["tokens"]["input"])
             for r in recent if r["tokens"].get("input") and r["tokens"].get("cache_read") is not None])
        self.query_one("#errors", Metric).set_value(
            compact(overview["errors"]), f"{compact(overview['unclosed'])} open spans · all agents",
            [r.get("status") == "error" for r in recent])
        agents = tuple(sorted(set(data.get("agents", [])) | ({self.agent_filter} if self.agent_filter else set())))
        if agents != self._agent_options:
            self._agent_options = agents
            with self.prevent(Select.Changed):
                select = self.query_one("#agent", Select)
                select.set_options([("All agents", "*")] + [(a, a) for a in agents])
                select.value = self.agent_filter or "*"
        indexing = health.get("index") and not health.get("scans")
        label = "Ⅱ PAUSED" if not self.live else "◌ INDEXING" if indexing else "● LIVE"
        self.query_one("#live", Static).update(Text(label, style=VIOLET if not self.live else CYAN))
        notes = f"{compact(overview['sessions'])} sessions · {compact(overview['traces'])} turns · {compact(overview['spans'])} spans"
        if not health.get("index", True):
            notes += " · index refresh disabled"
        elif health.get("scanning"):
            notes += " · indexing new activity"
        if health.get("errors"):
            notes += f" · {len(health['errors'])} source issues (last snapshot retained)"
        capture = health.get("capture")
        if capture and capture["enabled"] and capture["available"]:
            notes += " · Codex syncing" if capture["last_success"] is None else " · Codex connected"
        self.query_one("#status", Static).update(Text(notes, style=MUTED))

    def _render_filters(self, matching: int | None = None,
                         total: int | None = None) -> None:
        search = self.query_one("#search", Input).value
        filtered = bool(search or self.agent_filter)
        self.query_one("#filters").display = filtered
        parts = [f"Filtered: {matching} / {total} sessions" if matching is not None
                 else "Filters active"]
        if self.agent_filter:
            parts.append("agent=" + sane(self.agent_filter))
        if search:
            parts.append("search=" + sane(search))
        self.query_one("#filter-summary", Static).update(Text(" · ".join(parts), style=VIOLET))

    @staticmethod
    def _session_label(row: dict) -> Text:
        cwd = (row.get("cwd") or "").rstrip("/").rsplit("/", 1)[-1]
        sid = sane(row["session_id"]).rsplit("/", 1)[-1]
        label = Text(sane(row["adapter_id"]), style=CYAN)
        label.append(" · " + (sane(cwd) or sid[-18:]), style="#e2e8f0")
        label.truncate(25, overflow="ellipsis")
        state = row.get("review_state") or ""
        icon = {"failed": "! ", "passed": "✓ ", "open": "◌ ", "unknown": "? "}.get(state, "")
        preview = Text(icon + sane(row.get("task") or sid[-25:]).replace("\n", " "),
                       style=RED if state == "failed" else GREEN if state == "passed" else MUTED)
        preview.truncate(25, overflow="ellipsis")
        label.append("\n")
        label.append_text(preview)
        return label

    def _render_sessions(self, data: dict, *, allow_auto_select: bool = True) -> None:
        table = self.query_one("#sessions", DataTable)
        rows = data["sessions"]
        row_map = {json.dumps([r["adapter_id"], r["session_id"]]): r for r in rows}
        keys = list(row_map)
        selected = json.dumps(list(self.session_key)) if self.session_key else None
        with self.prevent(DataTable.RowHighlighted, DataTable.RowSelected):
            if keys != self._session_keys:
                table.clear()
                for key, row in row_map.items():
                    table.add_row(self._session_label(row), compact(row["trace_count"]), height=2, key=key)
                self._session_keys = keys
                if selected in keys:
                    table.move_cursor(row=keys.index(selected), scroll=False)
            else:
                for key, row in row_map.items():
                    table.update_cell(key, "identity", self._session_label(row))
                    table.update_cell(key, "turns", compact(row["trace_count"]))
        self._session_rows = row_map
        total = data["total"]
        start = self.session_offset + 1 if rows else 0
        self.query_one("#sessions-page", Static).update(f"{start}–{self.session_offset + len(rows)} / {total}")
        self.query_one("#sessions-prev", Button).disabled = self.session_offset == 0
        self.query_one("#sessions-next", Button).disabled = not data["has_more"]
        if selected not in keys:
            if self.initial_session is not None or not allow_auto_select:
                return
            if rows:
                chosen = rows[0]
                self._select_session((chosen["adapter_id"], chosen["session_id"]))
                with self.prevent(DataTable.RowHighlighted, DataTable.RowSelected):
                    table.move_cursor(row=rows.index(chosen))
            else:
                self._clear_session()
                filtered = self.agent_filter or self.query_one("#search", Input).value
                message = ("No sessions match these filters. Click Show all or press Ctrl+L to restore all sessions."
                           if filtered else "No sessions yet. New agent activity will appear here.")
                self._show_info(message)

    def _clear_session(self) -> None:
        self._review_primary = True
        self._review_navigation = False
        self._render_review(None)
        self.session_key = None
        self.span_ref = None
        self._session_epoch += 1
        self._span_epoch += 1
        self._turn_nodes.clear()
        self._trace_cache.clear()
        self.query_one("#traces", Tree).clear()
        self.query_one("#scope", Static).update("Select a session")
        self.query_one("#span-summary", Static).update("Select a call to inspect")
        for name in ("input", "output", "error", "json", "events", "info"):
            self._set_body(name, None)

    def _select_session(self, key: SessionKey) -> None:
        if key == self.session_key:
            return
        self._clear_session()
        self.session_key = key
        self.turn_offset = 0
        self.query_one("#scope", Static).update(Text(sane(key[0]) + " / " + sane(key[1])[-28:], style=MUTED))
        self._show_info("Loading this session…")

    @on(DataTable.RowHighlighted, "#sessions")
    @on(DataTable.RowSelected, "#sessions")
    def session_selected(self, event: Any) -> None:
        row = self._session_rows.get(event.row_key.value)
        if row:
            self.initial_session = None
            key = (row["adapter_id"], row["session_id"])
            self._select_session(key)
            if isinstance(event, DataTable.RowSelected):
                self._review_navigation = True
                self._active_panel = "traces"
                self._layout()
                self.query_one("#traces", Tree).focus()
            self.run_worker(self._load_session(key, self._session_epoch),
                            group="session", exclusive=True, exit_on_error=False)

    async def _load_session(self, key: SessionKey, epoch: int) -> None:
        offset = self.turn_offset
        try:
            data = await asyncio.to_thread(self.backend.session, key, offset)
            if self.closed or key != self.session_key or epoch != self._session_epoch or offset != self.turn_offset:
                return
            self._render_review(data.get("review"))
            if self._review_navigation and self._review:
                self._active_panel = "detail"
                self._layout()
            self._review_navigation = False
            turns = data["turns"]
            if not turns["traces"] and offset > 0:
                self.turn_offset = max(0, ((max(1, turns["total"]) - 1) // PAGE_SIZE) * PAGE_SIZE)
                self._session_epoch += 1
                self.run_worker(self._load_session(key, self._session_epoch), group="session-repage",
                                exclusive=True, exit_on_error=False)
                return
            tree = self.query_one("#traces", Tree)
            cursor_ref = tree.cursor_node.data if tree.cursor_node else None
            cursor_selection = self._span_epoch
            ids = {r["trace_id"] for r in turns["traces"]}
            with self.prevent(Tree.NodeSelected, Tree.NodeExpanded):
                for trace_id in list(self._turn_nodes):
                    if trace_id not in ids:
                        self._turn_nodes.pop(trace_id).remove()
                        self._trace_cache.pop(trace_id, None)
                for row in reversed(turns["traces"]):
                    label = self._turn_label(row)
                    trace_id = row["trace_id"]
                    if trace_id in self._turn_nodes:
                        self._turn_nodes[trace_id].set_label(label)
                    else:
                        self._turn_nodes[trace_id] = tree.root.add(
                            label, NodeRef(trace_id), before=0, allow_expand=True)
            n = len(turns["traces"])
            start = offset + 1 if n else 0
            self.query_one("#turns-page", Static).update(f"{start}–{offset + n} / {turns['total']}")
            self.query_one("#turns-prev", Button).disabled = offset == 0
            self.query_one("#turns-next", Button).disabled = offset + n >= turns["total"]
            if self.span_ref is not None and self.span_ref.trace_id not in ids:
                self.span_ref = None
                self._span_epoch += 1
                self._show_info("The selected turn left this page. Select another turn.")
            ref, selection = self.span_ref, self._span_epoch
            visible = {trace_id for trace_id, node in self._turn_nodes.items() if node.is_expanded}
            auto_trace = turns["traces"][0]["trace_id"] if n and ref is None else None
            if auto_trace is not None:
                visible.add(auto_trace)
            if ref is not None:
                visible.add(ref.trace_id)
            await asyncio.gather(*(self._load_trace(key, epoch, trace_id,
                                                   choose=trace_id == auto_trace, selection=selection)
                                   for trace_id in visible))
            self.call_after_refresh(self._restore_tree_cursor, key, epoch, cursor_selection, cursor_ref)
            if (ref is not None and key == self.session_key and epoch == self._session_epoch
                    and ref == self.span_ref and selection == self._span_epoch and ref.span_id):
                await self._load_span(key, ref, selection)
            if not n:
                if self._review:
                    self.query_one("#details", TabbedContent).active = "tab-review"
                else:
                    self._show_info("This session has no indexed turns yet.")
        except Exception as exc:
            if epoch == self._session_epoch and key == self.session_key:
                self._error(exc)

    @staticmethod
    def _turn_label(row: dict) -> Text:
        status = row.get("status")
        icon, color = ("!", RED) if status == "error" else ("◌", CYAN) if status == "incomplete" else ("✓", GREEN)
        label = Text(icon + " ", style=color)
        label.append((row.get("start") or "")[11:19] + "  ", style=MUTED)
        label.append(sane(row.get("name") or "Turn")[:28])
        label.append("  " + duration(row.get("duration_ms")), style=VIOLET)
        return label

    def _restore_tree_cursor(self, key: SessionKey, epoch: int, selection: int,
                             ref: NodeRef | None) -> None:
        if (self.closed or key != self.session_key or epoch != self._session_epoch
                or selection != self._span_epoch or ref is None):
            return
        pending = list(self._turn_nodes.values())
        tree = self.query_one("#traces", Tree)
        while pending:
            node = pending.pop()
            if node.data == ref:
                tree.move_cursor(node)
                return
            pending.extend(node.children)

    async def _load_trace(self, key: SessionKey, epoch: int, trace_id: str,
                          *, choose: bool = False, selection: int | None = None) -> None:
        node = self._turn_nodes.get(trace_id)
        if self.closed or epoch != self._session_epoch or key != self.session_key or node is None:
            return
        request_key = (key, epoch, trace_id)
        task = self._trace_requests.get(request_key)
        if task is None:
            task = asyncio.create_task(asyncio.to_thread(self.backend.trace, key, trace_id))
            self._trace_requests[request_key] = task

            def finished(done: asyncio.Task) -> None:
                if self._trace_requests.get(request_key) is done:
                    self._trace_requests.pop(request_key, None)
                # A cancelled UI waiter must not leave an unobserved exception.
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        try:
            data = await asyncio.shield(task)
        except Exception:
            if self.closed or epoch != self._session_epoch or key != self.session_key:
                return
            raise
        if (self.closed or epoch != self._session_epoch or key != self.session_key
                or self._turn_nodes.get(trace_id) is not node):
            return
        if choose and selection is not None and selection != self._span_epoch:
            return
        if data is None:
            self._show_info("This turn is no longer in the index. Refresh to update.")
            return
        spans = data["spans"]
        visible = [s for s in spans if not (s.get("synthetic") and s["span_id"] == trace_id)] or spans
        if self._trace_cache.get(trace_id) != visible:
            tree = self.query_one("#traces", Tree)
            cursor_ref = tree.cursor_node.data if tree.cursor_node else None
            descendants = []
            pending = list(node.children)
            while pending:
                child = pending.pop()
                descendants.append(child)
                pending.extend(child.children)
            expanded = {n.data.span_id for n in descendants if n.is_expanded and n.data}
            with self.prevent(Tree.NodeSelected, Tree.NodeExpanded):
                node.remove_children()

                def add(parent: TreeNode, forest: list) -> None:
                    for span, children in forest:
                        status = span.get("status")
                        color = RED if status == "error" else CYAN if status == "incomplete" else GREEN
                        label = Text("◌ " if status == "incomplete" else "! " if status == "error" else "✓ ", style=color)
                        label.append(sane(span.get("kind", "span")), style=VIOLET)
                        label.append(" · " + sane(span.get("name", ""))[:32])
                        label.append("  " + duration(span.get("duration_ms")), style=MUTED)
                        child = parent.add(label, NodeRef(trace_id, span["span_id"]),
                                           expand=span["span_id"] in expanded,
                                           allow_expand=bool(children))
                        add(child, children)

                add(node, span_forest(visible))
                self.call_after_refresh(self._restore_tree_cursor, key, epoch, self._span_epoch, cursor_ref)
                self._trace_cache[trace_id] = visible
                if choose:
                    node.expand()
            if self.span_ref and self.span_ref.trace_id == trace_id and self.span_ref.span_id:
                if not any(s["span_id"] == self.span_ref.span_id for s in visible):
                    self.span_ref = None
                    self._span_epoch += 1
                    self._show_info("This call is no longer in the index. Select another call.")
        if choose and visible:
            first = next((s for s in visible if s.get("kind") in ("llm", "tool")), visible[0])
            self.span_ref = NodeRef(trace_id, first["span_id"])
            self._span_epoch += 1
            await self._load_span(key, self.span_ref, self._span_epoch, new=True)

    @on(Tree.NodeExpanded, "#traces")
    def trace_expanded(self, event: Tree.NodeExpanded) -> None:
        if event.node.data and event.node.data.span_id is None and self.session_key:
            self.run_worker(self._expand(self.session_key, self._session_epoch, event.node.data), group="expand",
                            exit_on_error=False)

    async def _expand(self, key: SessionKey, epoch: int, ref: NodeRef) -> None:
        try:
            await self._load_trace(key, epoch, ref.trace_id)
        except Exception as exc:
            if epoch == self._session_epoch and key == self.session_key:
                self._error(exc)

    @on(Tree.NodeSelected, "#traces")
    def call_selected(self, event: Tree.NodeSelected) -> None:
        ref = event.node.data
        if not ref or not self.session_key:
            return
        self._review_primary = False
        self._active_panel = "detail"
        self._layout()
        self.span_ref = ref
        self._span_epoch += 1
        self._show_info("Loading this call…")
        if ref.span_id is None:
            self.run_worker(self._choose_trace(self.session_key, self._session_epoch, ref.trace_id, self._span_epoch),
                            group="detail", exclusive=True, exit_on_error=False)
        else:
            self.run_worker(self._load_span(self.session_key, ref, self._span_epoch, new=True),
                            group="detail", exclusive=True, exit_on_error=False)

    async def _choose_trace(self, key: SessionKey, epoch: int, trace_id: str,
                            selection: int) -> None:
        try:
            await self._load_trace(key, epoch, trace_id, choose=True, selection=selection)
        except Exception as exc:
            self._error(exc)

    async def _load_span(self, key: SessionKey, ref: NodeRef, epoch: int,
                         *, new: bool = False) -> None:
        try:
            span = await asyncio.to_thread(self.backend.span, key, ref.trace_id, ref.span_id)
            if self.closed or key != self.session_key or ref != self.span_ref or epoch != self._span_epoch:
                return
            if span is None:
                self._show_info("This call is no longer in the index. Select another call.")
                return
            summary = Text(sane(span.get("kind", "span")) + " · " + sane(span.get("name", "")), style=CYAN)
            summary.append("\n" + sane(span.get("status", "")) + " · " + duration(span.get("duration_ms"))
                           + " · " + sane(span.get("model") or "model not recorded"), style=MUTED)
            self.query_one("#span-summary", Static).update(summary)
            for name in ("input", "output", "error", "events"):
                self._set_body(name, span.get(name), reset=new)
            self._set_body("json", span, reset=new)
            info = {k: v for k, v in span.items() if k not in ("input", "output", "error")}
            self._set_body("info", info, reset=new)
            if new:
                self.query_one("#details", TabbedContent).active = (
                    "tab-review" if self._review_primary and self._review else
                    "tab-error" if span.get("error") else "tab-output" if span.get("output") is not None
                    else "tab-input" if span.get("input") is not None
                    else "tab-events" if span.get("events") else "tab-info")
        except Exception as exc:
            if epoch == self._span_epoch and key == self.session_key:
                self._error(exc)

    def _set_body(self, name: str, value: Any, *, reset: bool = False) -> None:
        field = self.query_one("#field-" + name, TextArea)
        text, language = body_text(readable_body(value, name))
        if field.text != text:
            offset = field.scroll_offset
            field.language = language
            field.load_text(text)
            if not reset:
                field.scroll_to(offset.x, offset.y, animate=False, immediate=True)
        if reset:
            field.scroll_home(animate=False)

    def _show_info(self, message: str) -> None:
        self._set_body("info", message, reset=True)
        self.query_one("#details", TabbedContent).active = "tab-info"

    @on(Input.Changed, "#search")
    def search_changed(self) -> None:
        if self.query_one("#search", Input).value != self.initial_session:
            self.initial_session = None
        self._filter_revision += 1
        self.session_offset = 0
        self._render_filters()
        if self._search_timer:
            self._search_timer.stop()
        self._search_timer = self.set_timer(.3, self.action_refresh)

    @on(Select.Changed, "#agent")
    def agent_changed(self, event: Select.Changed) -> None:
        self.agent_filter = None if event.value == "*" else str(event.value)
        self.session_offset = 0
        self._filter_revision += 1
        self._render_filters()
        self.action_refresh()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id
        if name == "pause":
            self.action_pause()
        elif name == "refresh":
            self.action_refresh()
        elif name == "clear-filters":
            self.action_clear_filters()
        elif name == "review-evidence":
            self.action_review_evidence()
        elif name == "handoff":
            self.action_handoff()
        elif name in ("sessions-prev", "sessions-next"):
            self.session_offset = max(0, self.session_offset + (PAGE_SIZE if name.endswith("next") else -PAGE_SIZE))
            self._filter_revision += 1
            self.action_refresh()
        elif name in ("turns-prev", "turns-next") and self.session_key:
            self.turn_offset = max(0, self.turn_offset + (PAGE_SIZE if name.endswith("next") else -PAGE_SIZE))
            self._session_epoch += 1
            self.span_ref = None
            self._span_epoch += 1
            self._turn_nodes.clear()
            self._trace_cache.clear()
            self.query_one("#traces", Tree).clear()
            self.run_worker(self._load_session(self.session_key, self._session_epoch),
                            group="session", exclusive=True, exit_on_error=False)

    def action_pause(self) -> None:
        self.live = not self.live
        self._display_epoch += 1
        self._session_epoch += 1
        self._span_epoch += 1
        self.query_one("#pause", Button).label = "Ⅱ Pause" if self.live else "▶ Resume"
        self.query_one("#live", Static).update(Text("● LIVE" if self.live else "Ⅱ PAUSED", style=CYAN if self.live else VIOLET))
        if self.live:
            self.action_refresh()

    def action_refresh(self) -> None:
        if self.ready:
            self.run_worker(self._refresh(), group="refresh", exit_on_error=False)
        elif self._starting:
            self.notify("The local archive is still opening", severity="information")
        else:
            self.query_one("#opening").display = True
            self.run_worker(self._initialize(), group="startup", exit_on_error=False)

    def _render_review(self, review: dict | None) -> None:
        self._review = review
        self.query_one("#handoff", Button).disabled = review is None
        self.query_one("#review-evidence", Button).disabled = not review or not (review["checks"] or review.get("failures"))
        if review is None:
            self._set_body("review", "Select a session to review its task, conclusion and check evidence.", reset=True)
            for name, text in (("state", "Select work to review"), ("task", "Task text not available yet."),
                               ("conclusion", "Conclusion and verification are reported separately."),
                               ("next", "Select a session · e: evidence · h: handoff · m: metrics")):
                self.query_one("#review-" + name, Static).update(Text(text, style=MUTED))
            return
        from core.review import markdown
        self._set_body("review", markdown(review))
        color = RED if review["state"] == "failed" else GREEN if review["state"] == "passed" else VIOLET
        failures = len(review.get("failures", []))
        extra = f" · {failures} tool failures" if failures else ""
        self.query_one("#review-state", Static).update(Text(review["label"] + " · latest turn" + extra, style=color))
        for name, text in (("task", "TASK  " + (review.get("task") or "Task text not captured.")),
                           ("conclusion", "AGENT SAYS  " + (review.get("conclusion") or "No conclusion captured.")),
                           ("next", review["next_action"])):
            self.query_one("#review-" + name, Static).update(Text(sane(text).replace("\n", " "), style=MUTED if name == "next" else "#e2e8f0"))

    def action_metrics(self) -> None:
        self._metrics_visible = not self._metrics_visible
        self.query_one("#metrics").display = self._metrics_visible

    def action_handoff(self) -> None:
        if self._review:
            from core.review import markdown
            self.push_screen(HandoffScreen(markdown(self._review)))

    def action_repository(self) -> None:
        if self.repository_root is None or not self.ready:
            return
        self.push_screen(RepositoryScreen(self.repository_root, self.backend, pane=self.herdr_target,
                                          continue_task=self.continue_task),
                         self._repository_source)

    def action_task_inbox(self, standalone: bool = False) -> None:
        if not self.ready or isinstance(self.screen, TaskInbox):
            return
        if isinstance(self.screen, RepositoryScreen) and self.screen.inbox_parent:
            self.screen.dismiss()
            return
        self.push_screen(TaskInbox(self.backend, self._resume_inbox_task, standalone=standalone))

    def _resume_inbox_task(self, item: TaskItem) -> None:
        pane = self.herdr_target
        self.push_screen(RepositoryScreen(item.root, self.backend, pane=pane if pane and pane.root == item.root else None,
                                          continue_task=True, inbox_parent=True, expected_task=item),
                         self._repository_source)

    def _repository_source(self, source: Evidence | None) -> None:
        if source is None:
            return
        agent, session, trace, span = source
        with self.prevent(Input.Changed, Select.Changed):
            self.query_one("#search", Input).value = session
        self.session_offset = 0
        self.initial_session = session
        self.agent_filter = agent
        self._filter_revision += 1
        key = (agent, session)
        self._select_session(key)
        if trace and span:
            self.run_worker(self._open_review_evidence(key, self._session_epoch,
                                                       {"trace_id": trace, "span_id": span}),
                            group="review-evidence", exclusive=True, exit_on_error=False)
        self.action_refresh()

    def action_review_evidence(self) -> None:
        if self._review and self.session_key and (self._review["checks"] or self._review.get("failures")):
            self._review_primary = False
            checks = self._review["checks"]
            failures = self._review.get("failures", [])
            check = next((c for c in reversed(checks) if c["state"] == "failed"),
                         failures[-1] if failures else checks[-1])
            self.run_worker(self._open_review_evidence(self.session_key, self._session_epoch, check),
                            group="review-evidence", exclusive=True, exit_on_error=False)

    async def _open_review_evidence(self, key: SessionKey, epoch: int, check: dict) -> None:
        try:
            if self.turn_offset:
                self.turn_offset = 0
                await self._load_session(key, epoch)
            if key != self.session_key or epoch != self._session_epoch:
                return
            self.span_ref = NodeRef(check["trace_id"], check["span_id"])
            self._span_epoch += 1
            self._active_panel = "detail"
            self._layout()
            await self._load_span(key, self.span_ref, self._span_epoch, new=True)
        except Exception as exc:
            if key == self.session_key and epoch == self._session_epoch:
                self._error(exc)

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_clear_filters(self) -> None:
        if self._search_timer:
            self._search_timer.stop()
        with self.prevent(Input.Changed, Select.Changed):
            self.query_one("#search", Input).value = ""
            self.query_one("#agent", Select).value = "*"
        self.agent_filter = None
        self.initial_session = None
        self.session_offset = 0
        self._filter_revision += 1
        self._render_filters()
        self._active_panel = "sessions"
        self._layout()
        self.query_one("#sessions", DataTable).focus()
        self.action_refresh()

    def action_panel(self, panel: str) -> None:
        self._active_panel = panel
        self._layout()
        active = self.query_one("#details", TabbedContent).active.removeprefix("tab-")
        self.query_one({"sessions": "#sessions", "traces": "#traces", "detail": "#field-" + active}[panel]).focus()

    def action_navigate_back(self) -> None:
        if isinstance(self.focused, Input):
            self.query_one("#sessions", DataTable).focus()
        else:
            reviewing = self.query_one("#details", TabbedContent).active == "tab-review"
            self.action_panel("traces" if self._active_panel == "detail" and not reviewing else "sessions")


def run(args: Any) -> int:
    from agenttrace_common import platform_dir
    from core.review_git import repository_root

    db = args.db or Path(os.environ.get("AGENTTRACE_DB") or platform_dir() / "agenttrace.db")
    backend = LocalBackend(db.expanduser(), args.dirs, index=not args.no_index,
                           interval=args.interval, capture=True)
    open_inbox = bool(getattr(args, "all", False) and getattr(args, "cmd", None) == "work")
    root = None if open_inbox else repository_root((args.repo or Path.cwd()).expanduser())
    task_view = getattr(args, "cmd", None) == "work" or getattr(args, "herdr_cmd", None) == "continue"
    if (args.repo or (task_view and not open_inbox)) and root is None:
        print("A Git worktree is required")
        return 2
    open_repo = not open_inbox and (
        task_view or (not args.archive and (args.repo is not None or not any((args.session, args.agent, args.no_index))))
    )
    TraceApp(backend, agent=args.agent, session=args.session, interval=args.interval,
             repository=root, open_repository=open_repo, herdr_target=getattr(args, "herdr_target", None),
             continue_task=task_view, open_inbox=open_inbox).run()
    return 0
