#!/usr/bin/env python3
"""Drive agenttrace watch inside a pty, feed it records, assert what it renders.

The TUI only runs against a real terminal (raw mode, alt screen), so this
harness allocates a pty, runs the panel in it, appends synthetic records to a
trace file, and reads back the rendered screen.
"""
import datetime
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH = os.path.join(ROOT, "cli", "agenttrace_watch.py")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\r")


def rec(agent, event, **kw):
    d = {
        "v": 1,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z",
        "agent": agent,
        "event": event,
        "session_id": "sess-live",
    }
    d.update(kw)
    return d


def run(timeout=6.0):
    tmp = tempfile.mkdtemp(prefix="agenttrace-watch-")
    traces = os.path.join(tmp, "traces")
    os.makedirs(traces)

    primary, secondary = pty.openpty()
    # Size the pty explicitly; an unsized pty reports 0 columns and the panel
    # would silently fall back to its minimum width.
    fcntl.ioctl(secondary, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", COLUMNS="100", LINES="30")
    proc = subprocess.Popen(
        [sys.executable, WATCH, traces],
        stdin=secondary, stdout=secondary, stderr=secondary, env=env, close_fds=True,
    )
    os.close(secondary)
    buf = b""


    def drain(seconds):
        nonlocal buf
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([primary], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(primary, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
        return buf

    drain(1.0)
    assert proc.poll() is None, "watch exited immediately"

    # 1. A normal request/response pair must appear.
    path = os.path.join(traces, "live.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(rec("hermes", "llm_request", model="space-bunny-free",
                               api_mode="chat_completions", request_chars=18804,
                               request=[{"role": "system", "content": "You are Ray's partner. " * 50},
                                        {"role": "user", "content": "What is 17*23?"}])) + "\n")
        f.flush()
    drain(1.0)
    f = open(path, "a")
    f.write(json.dumps(rec("hermes", "llm_response", model="space-bunny-free",
                           duration_ms=1500,
                           response={"content": "391", "finish_reason": "stop",
                                     "usage": {"input_tokens": 13318,
                                               "output_tokens": 2,
                                               "cache_read_tokens": 13298}})) + "\n")
    f.flush()
    drain(1.2)

    # 0. An idle panel must be silent. The old loop cleared the whole screen
    #    (`\x1b[2J`) four times a second regardless of change, which is what
    #    made the window flash; a frame is now written only when it differs
    #    from the one on screen. Repaint on real change, silence otherwise.
    idle_mark = len(buf)
    drain(1.5)
    assert len(buf) == idle_mark, (
        f"idle panel wrote {len(buf) - idle_mark} bytes (flicker regression); "
        f"tail: {buf[idle_mark:][200:600]!r}")
    assert b"\x1b[2J" not in buf, "panel used a full-screen clear (2J)"

    text = ANSI.sub("", buf.decode("utf-8", "replace"))
    assert "llm_request" in text, f"request event not rendered; screen was:\n{text[-3000:]}"
    assert "call #1" in text, \
        f"call banner missing — a complete LLM call must open with its own divider; got:\n{text[-3000:]}"
    assert "space-bunny-free" in text, "model not rendered"
    assert "17*23" in text, "user prompt not rendered"
    assert "391" in text, "response content not rendered"
    assert "cache 13298" in text or "13298" in text, \
        f"cache usage not rendered; screen was:\n{text[-3000:]}"
    # The stats row above the footer: session Σ tokens + KV-cache hit rate,
    # recomputed from the records currently in scope.
    assert "Σ tok" in text, \
        f"stats row (Σ tokens/cache) missing; screen was:\n{text[-3000:]}"
    assert "cache" in text, "cache rate missing from stats row"
    # Fold stub presence, not its wording — the stub text is a style choice and
    # has changed; what matters is that a 20k-char system prompt is collapsed.
    assert "e to expand" in text, \
        f"long system prompt not folded; screen was:\n{text[-3000:]}"
    # The Hermes adapter puts the system prompt inside `messages`, not in a
    # dedicated field. A renderer that only reads `request.system_prompt` drops
    # it entirely — assert the prompt body is actually on screen.
    assert "Ray's partner" in text, \
        f"system prompt missing from screen; screen was:\n{text[-3000:]}"

    # 2. A second agent's records must render alongside.
    f.write(json.dumps(rec("codex", "llm_request", model="gpt-5.5", api_mode="responses",
                           system_prompt="You are Codex, a coding agent based on GPT-5.")) + "\n")
    f.flush()
    drain(1.0)
    text = ANSI.sub("", buf.decode("utf-8", "replace"))
    assert "codex" in text, "second agent not rendered"
    assert "gpt-5.5" in text, "second agent model not rendered"

    # 3. 'e' toggles expansion; the folded stub must give way to real text.
    #    Judged on the hermes record, which is on screen and has the 20k-char
    #    system prompt that folding hides.
    os.write(primary, b"e")
    mark = len(buf)
    drain(1.2)
    text = ANSI.sub("", buf[mark:].decode("utf-8", "replace"))
    assert "expanded" in text, "expand toggle not reflected in header"
    # Assert the prompt BODY appeared, not merely that a stub vanished: a check
    # on the stub alone passes while nothing actually expanded.
    assert "Ray's partner" in text, \
        f"expand mode did not reveal the system prompt; got:\n{text[-2000:]}"
    os.write(primary, b"e")   # collapse again
    drain(0.6)

    # 3b. Session hysteresis — the "content appears, then is retracted" bug.
    #     Another session appending into the SAME trace file (a cron job, an
    #     e2e run, a second agent) used to call recs.clear() on every foreign
    #     record, wiping whatever you were watching. Now a foreign record is
    #     ignored inside the quiet window, the panel follows only after the
    #     scoped session has been silent for SCOPE_QUIET_SECONDS, and
    #     switching back RESTORES the old conversation from the buffer.
    #     Record ts drives the quiet window, so the test needs no real sleep.
    def iso(delta_sec):
        t = (datetime.datetime.now(datetime.timezone.utc)
             + datetime.timedelta(seconds=delta_sec))
        return t.strftime("%Y-%m-%dT%H:%M:%S") + ".000000Z"

    def last_frame():
        # Every repaint starts with \x1b[H (cursor home); the segment after
        # the last one is the complete screen currently on the terminal.
        seg = buf.rsplit(b"\x1b[H", 1)[-1]
        return ANSI.sub("", seg.decode("utf-8", "replace"))

    def write(record):
        f.write(json.dumps(record) + "\n")
        f.flush()

    # Our own record pins the scope clock at a known instant.
    write(rec("hermes", "note", ts=iso(0), note="scope-clock"))
    drain(0.6)

    # (a) foreign record INSIDE the quiet window: it must not retract view.
    write(rec("hermes", "tool_call", session_id="other-session", ts=iso(1),
              tool={"name": "terminal", "status": "ok",
                    "args": {"command": "cron-jobs"}}))
    write(rec("hermes", "note", ts=iso(2), note="own-after-foreign"))
    drain(1.2)
    frame = last_frame()
    assert "ess-live" in frame, \
        f"scope flipped by a quiet-window record; frame:\n{frame}"
    assert "17*23" in frame, \
        f"conversation retracted by a foreign record; frame:\n{frame}"
    assert "cron-jobs" not in frame, \
        f"foreign session leaked into the scoped view; frame:\n{frame}"

    # (b) foreign record BEYOND the quiet window: panel follows it, old
    #     content is hidden (not deleted).
    write(rec("hermes", "note", session_id="other-session", ts=iso(60),
              note="moved-out"))
    drain(1.2)
    frame = last_frame()
    assert "session=-session" in frame, \
        f"scope did not follow after the quiet window; frame:\n{frame}"
    assert "17*23" not in frame, \
        f"old session still rendered after the switch; frame:\n{frame}"

    # (c) our session writes again (foreign one now quiet): scope returns
    #     AND the conversation comes back — nothing was cleared.
    write(rec("hermes", "note", ts=iso(120), note="scope-returns"))
    drain(1.2)
    frame = last_frame()
    assert "ess-live" in frame, f"scope did not return; frame:\n{frame}"
    assert "17*23" in frame, \
        f"old conversation not restored from the buffer; frame:\n{frame}"

    # 4. Pager / unknown escape sequences must NOT kill the panel.
    #    Regression: PgUp (`\x1b[5~`) decoded to its final byte `~`, matched
    #    nothing, fell back to "ESC" — and the loop treats ESC as quit, so
    #    the keys the footer advertises closed the panel. PgUp twice scrolls
    #    up (header shows `scroll -N`), PgDn twice returns to the bottom;
    #    Delete/Home and unknown sequences are ignored, never quit.
    #    Scroll only exists when content overflows the viewport (visible =
    #    30 rows − header/stats/footer = 27): add filler pairs until it does.
    #    With the filter applied later, content shrinks below one screen and
    #    scroll clamps to 0 BY DESIGN — so this runs before the filter.
    for i in range(6):
        f.write(json.dumps(rec("filler", "llm_request", model=f"filler-{i}",
                               api_mode="chat_completions",
                               system_prompt=f"system filler {i} " * 5,
                               request=[{"role": "user",
                                         "content": f"filler prompt {i}"}])) + "\n")
        f.write(json.dumps(rec("filler", "llm_response", model=f"filler-{i}",
                               duration_ms=10,
                               response={"content": f"filler reply {i}",
                                         "finish_reason": "stop"})) + "\n")
    f.flush()
    drain(1.5)
    os.write(primary, b"\x1b[5~")
    os.write(primary, b"\x1b[5~")
    mark = len(buf)
    drain(1.2)
    text = ANSI.sub("", buf[mark:].decode("utf-8", "replace"))
    assert "scroll -" in text, \
        f"PgUp did not scroll the view; got:\n{text[-800:]}"
    os.write(primary, b"\x1b[6~")
    os.write(primary, b"\x1b[6~")
    os.write(primary, b"\x1b[3~")     # Delete — ignored
    os.write(primary, b"\x1b[1~")     # some terminals' Home — HOME, no-op
    os.write(primary, b"\x1b[99~")    # unknown — must be ignored, never quit
    drain(1.2)
    assert proc.poll() is None, \
        f"escape sequence exited the panel (rc={proc.poll()})"

    # 5. Filter must hide non-matching records.
    f.write(json.dumps(rec("pi", "llm_request", model="pi-model", api_mode="chat_completions",
                           request=[{"role": "user", "content": "zebra-unique-token"}])) + "\n")
    f.flush()
    drain(1.0)
    text = ANSI.sub("", buf.decode("utf-8", "replace"))
    assert "zebra-unique-token" in text, "pi record not rendered before filtering"

    os.write(primary, b"/zebra-unique-token\n")
    mark = len(buf)
    drain(1.2)
    # Only judge frames drawn after the filter was accepted; the buffer
    # legitimately contains pre-filter screens.
    text = ANSI.sub("", buf[mark:].decode("utf-8", "replace"))
    assert "zebra-unique-token" in text, "filter removed the matching record"
    assert "17*23" not in text, f"filter failed to hide non-matching record; got:\n{text[-2000:]}"

    # 6. 'q' must quit cleanly and restore the terminal.
    os.write(primary, b"q")
    deadline = time.time() + 3
    while time.time() < deadline and proc.poll() is None:
        drain(0.2)
    drain(0.5)
    assert proc.poll() is not None, "q did not exit"
    assert proc.returncode == 0, f"non-zero exit: {proc.returncode}"
    f.close()

    shutil.rmtree(tmp, ignore_errors=True)
    print("PASS — pty harness: live render, multi-agent, filter, expand, clean quit")


if __name__ == "__main__":
    run()
