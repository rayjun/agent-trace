#!/usr/bin/env python3
"""Pilot-driven keyboard, resize and async-race tests; no live traces touched."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
from typing import Any
import unittest

if importlib.util.find_spec("textual") is None:
    print("SKIP Textual UI: install requirements-tui.txt to run the optional dashboard tests")
    raise SystemExit(0)

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "cli"), str(ROOT / "common")]
from agenttrace_tui import BODY_LIMIT, NodeRef, TraceApp, body_text, readable_body, span_forest  # noqa: E402
from textual.widgets import Button, DataTable, Input, Select, TabbedContent, TextArea, Tree  # noqa: E402


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
