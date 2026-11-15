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
import unittest

if importlib.util.find_spec("textual") is None:
    print("SKIP Textual UI: install requirements-tui.txt to run the optional dashboard tests")
    raise SystemExit(0)

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "cli"), str(ROOT / "common")]
from agenttrace_tui import BODY_LIMIT, NodeRef, TraceApp, body_text, readable_body, span_forest  # noqa: E402
from textual.widgets import DataTable, Input, TabbedContent, TextArea  # noqa: E402


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

    def dashboard(self, search="", agent=None, offset=0):
        if self.failure:
            raise OSError("fixture temporarily unavailable")
        rows = [r for r in self.rows if (agent is None or r["adapter_id"] == agent)
                and (not search or search.lower() in str(r).lower())]
        return dict(
            overview=dict(tokens=dict(input=12000, output=3000, cache_read=9000),
                          task_duration_ms=dict(p50=2400), errors=1, unclosed=2,
                          sessions=len(self.rows), traces=6, spans=20),
            sessions=dict(total=len(rows), has_more=False, sessions=deepcopy(rows)),
            recent=[dict(tokens=dict(input=100, output=4, cache_read=80), status="ok",
                         duration_ms=2400, unclosed=0)],
            agents=["a", "b"], health=dict(index=False, scans=1), usage_known=True)

    def session(self, key, offset=0):
        return dict(metrics={}, turns=dict(total=1, traces=[dict(
            trace_id="shared", name="Turn " + key[0], status="ok", duration_ms=2400,
            start="2026-11-15T10:00:00.000Z")]))

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
