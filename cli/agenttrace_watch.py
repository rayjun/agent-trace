#!/usr/bin/env python3
"""agenttrace watch — a full-terminal live view of LLM traffic across agents.

Reads the same trace jsonl the adapters write and renders a scrolling panel.
Stdlib only: the TUI is raw-termios + ANSI, not curses, so it drops into any
terminal without a ncurses terminfo dependency.

    agenttrace watch                     # follow everything, newest at bottom
    agenttrace watch --agent hermes      # one agent
    agenttrace watch --history 200      # preload more lines
    agenttrace watch --no-follow         # dump and exit (scriptable)

Keys:
    q / Ctrl-C   quit
    e             expand/collapse the focused long field
    f             toggle follow (pause = stop auto-scroll)
    PgUp/PgDn     scroll history
    /             filter by substring

This module is the MAIN LOOP: scope the conversation, tail the files, render a
frame, read a key, repeat. Everything it draws with or reads from lives under
`panel/`; the names below are re-exported so `import agenttrace_watch as w;
w.Formatter` still resolves.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from agenttrace import default_trace_dirs, iter_records  # noqa: E402,F401
from agenttrace_common import AGENTS  # noqa: E402

from panel.formatter import Formatter, _args_preview  # noqa: E402,F401
from panel.scoring import grade_for, score_prompt  # noqa: E402,F401
from panel.stats import session_stats  # noqa: E402,F401
from panel.tailer import Tailer, latest_session, load_history  # noqa: E402,F401
from panel.terminal import (  # noqa: E402,F401
    _CSI_KEYS, _HAVE_TERMIOS, _TILDE_KEYS, Screen, _split_key, _termios,
    decode_esc,
)
from panel.text import (  # noqa: E402,F401
    C_AI, C_BLUE, C_BOLD, C_DIM, C_MAGENTA, C_RED, C_RESET, C_REV, C_USER,
    C_WHITE, CALL_HUES, EXPAND_LIMIT, FOLD_THRESHOLD, FIELD_W,
    NORMAL_BODY_LINES, _as_text, _bar, _fit, _fmt_tok, _wide, avail_for,
    call_hue, clip, clip_plain, re_sub, sane, strip_ansi, vlen,
)

# -------------------------------------------------------------------------
# session scope — which conversation the panel follows
# -------------------------------------------------------------------------

# How long the scoped session must go quiet before another session is allowed
# to take the panel over. Trace files are shared: a cron job, an e2e test run
# or a second agent appends into the SAME jsonl while you watch your own
# conversation, so a single foreign record must never trigger a switch.
SCOPE_QUIET_SECONDS = 5.0


def _ts_seconds(ts: str) -> float | None:
    """ISO-8601 timestamp → epoch seconds; None when unparseable.

    Diffs only — two values parsed the same way cancel any timezone offset,
    so naive datetimes are fine here."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def watch(args) -> int:
    # Restore the default SIGPIPE disposition — but only when stdout is NOT
    # the TUI. Python sets SIGPIPE to SIG_IGN at startup, which turns
    # `agenttrace watch --no-follow | grep -q` into a BrokenPipeError
    # traceback the moment the reader exits early; Unix tools are expected
    # to die quietly on a closed pipe. In the live panel, though, SIG_DFL
    # means a closed stdout pipe KILLS the process outright, skipping
    # Screen.__exit__ — the terminal is left in raw mode with the alt screen
    # active and the cursor hidden (reproduced: returncode -13). The TUI
    # keeps SIG_IGN so a broken pipe surfaces as an exception we unwind from.
    if getattr(args, "no_follow", False) or not sys.stdout.isatty():
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        except (AttributeError, ValueError, OSError):
            pass

    dirs = [d.expanduser().resolve() for d in args.dirs] if args.dirs else default_trace_dirs()
    if not dirs:
        print(f"no trace directories found (looked for {default_trace_dirs()})", file=sys.stderr)

    # `--agent` arrives as a plain str from the top-level agenttrace parser and as
    # a list from this module's standalone parser. Normalise once, here, so the
    # filter and the header label never disagree about the value's shape.
    raw_agent = getattr(args, "agent", None)
    agents = list(raw_agent) if isinstance(raw_agent, (list, tuple)) else (
        [raw_agent] if raw_agent else [])

    # Keep RECORDS, not rendered lines. Rendering is a pure function of
    # (record, expand flag), so toggling `e` re-renders everything correctly
    # instead of trying to patch a line buffer that may be stale.
    fmt = Formatter()
    max_lines = 4000

    # Scope to the current conversation by default. A day of Hermes turns is
    # hundreds of sessions and megabytes of JSONL; showing all of them means the
    # panel is dominated by this morning's work while you are trying to watch
    # the turn in front of you. `--all-sessions` restores the old behaviour.
    all_sessions = getattr(args, "all_sessions", False)
    scope = args.session or (None if all_sessions else latest_session(dirs))
    # Follow the newest session unless the scope was pinned by hand. This must
    # be true even when `scope` is still None: the panel is often started
    # before the agent has written anything, and a `bool(scope)` guard made the
    # first-ever session bypass scoping entirely.
    follow_scope = not args.session and not all_sessions

    def admit(r: dict) -> bool:
        """All filters EXCEPT the session scope.

        The live buffer keeps other sessions' records: switching scope away
        and back must restore what you were looking at, and a record dropped
        at append time can never come back. Scope filtering happens at render
        time instead (see `shown`)."""
        if agents and r.get("agent") not in agents:
            return False
        if args.model and args.model not in (r.get("model") or ""):
            return False
        if args.event and r.get("event") != args.event:
            return False
        if args.since and (r.get("ts") or "") < args.since:
            return False
        return True

    def keep(r: dict) -> bool:
        """History preload path: only the scoped session's records are read
        off disk — there is nothing to restore when loading."""
        return admit(r) and (not scope or r.get("session_id") == scope)

    history = getattr(args, "history", 0)
    following = getattr(args, "follow", True)
    if not history and scope and following:
        # A live panel starts empty and follows. Loading every record of the
        # current session just to show the last 4000 lines is wasted I/O on a
        # long session. `--no-follow` still has to print what is on disk, so it
        # is excluded from this shortcut.
        recs: list[dict] = []
    else:
        recs = load_history(dirs, keep, history)

    if not following:
        for l in render_view(recs, fmt, max_lines, 10_000, 0):
            print(strip_ansi(l))
        return 0

    tailer = Tailer(dirs)   # seeds existing files to EOF, reads new files from 0

    scroll = 0          # 0 = pinned to bottom (follow)
    # Newest ts seen for the current scope; the quiet-window clock compares
    # against it. Empty until the scoped session writes its first record.
    scope_last = ""
    started = time.monotonic()

    def scope_is_quiet(ts: str) -> bool:
        """True when the scoped session has been silent long enough that
        another session may take the panel over."""
        if not scope_last:
            # The scope was adopted from the file tail at startup and its
            # session has not written since — fall back to wall-clock so a
            # dead scope cannot pin the panel to an empty screen forever.
            return time.monotonic() - started >= SCOPE_QUIET_SECONDS
        a, b = _ts_seconds(ts), _ts_seconds(scope_last)
        return a is not None and b is not None and (a - b) >= SCOPE_QUIET_SECONDS
    filter_text = ""
    # Keyed by (ts, event, session) rather than id(): records are dicts that
    # live and die with the buffer, so id() is reusable and a stale entry would
    # silently match a different record.
    search_cache: dict[tuple, str] = {}

    def matches_filter(r: dict) -> bool:
        # Re-checked every frame: the filter is typed interactively, so records
        # already buffered must be re-evaluated, not just newly arrived ones.
        # The searchable text is computed once per record and memoised: doing
        # json.dumps over the whole buffer on every frame (4 times a second,
        # thousands of records, each carrying a full conversation) was the
        # single most expensive thing the panel did.
        if not filter_text:
            return True
        key = (r.get("ts"), r.get("event"), r.get("session_id"))
        hay = search_cache.get(key)
        if hay is None:
            hay = json.dumps(r, ensure_ascii=False).lower()
            search_cache[key] = hay
        return filter_text in hay

    with Screen() as scr:
        last_frame: str | None = None
        last_size = (0, 0)
        while True:
            for rec in tailer.poll():
                # Session following with hysteresis, and never a clear.
                # The old rule — any record from another session called
                # recs.clear() — meant a cron job, an e2e test run or a
                # second agent appending into the SAME trace file wiped the
                # conversation you were watching (that was the "content
                # shows up, then gets retracted" bug: three session ids
                # interleave in hermes-*.jsonl within seconds).
                # Now another session takes over only after the scoped one
                # has been quiet for SCOPE_QUIET_SECONDS, and switching
                # hides (never deletes) the old conversation — if its
                # session writes again, the content comes back as it was.
                #
                # admit() runs FIRST: a session whose records are all
                # filtered out (--agent/--model/--event/--since) must not
                # steal the scope. It could take over anyway (the scope
                # update used to run before the filter), its records never
                # entered the buffer, and it kept refreshing scope_last —
                # so the panel stayed blank and could never recover while
                # your own session's content sat hidden in the buffer.
                if not admit(rec):
                    continue
                sid = rec.get("session_id")
                ts = rec.get("ts") or ""
                if sid and sid == scope:
                    if ts > scope_last:
                        scope_last = ts
                elif follow_scope and sid:
                    if scope is None:
                        scope, scope_last = sid, ts
                        scroll = 0
                    elif ts > scope_last and scope_is_quiet(ts):
                        scope, scope_last = sid, ts
                        scroll = 0
                recs.append(rec)
            if len(recs) > max_lines:
                recs = recs[-max_lines:]

            scr.resize()
            visible = scr.height - 3   # header + stats row + footer
            # Render at the real panel width so wrapping matches the terminal.
            fmt.width = max(40, scr.width - 1)
            # Scope filtering lives HERE (render time), not in the buffer:
            # records from other sessions stay cached so a switch away and
            # back restores the conversation instead of leaving a hole.
            shown = [r for r in recs
                     if (not scope or r.get("session_id") == scope)
                     and matches_filter(r)]
            view = render_view(shown, fmt, max_lines, visible, scroll)

            head = f"{C_REV}{C_BOLD} agenttrace watch {C_RESET} {C_DIM}agents={','.join(agents) if agents else 'all'}  " \
                   f"records={len(shown)}{'/' + str(len(recs)) if filter_text else ''}  "
            if scope:
                # Show which conversation is on screen — with auto-scoping this
                # changes when the agent restarts, and a blank panel is
                # otherwise indistinguishable from a broken one.
                head += f"session={scope[-8:]}  "
            if filter_text:
                head += f"{C_RESET}{C_BOLD}/{filter_text}{C_RESET} {C_DIM}"
            flags = []
            if fmt.expand:
                flags.append("expanded")
            flags.append("following" if not scroll else f"scroll -{scroll}")
            head += " · ".join(flags) + C_RESET
            foot = (f" {C_DIM}{C_BOLD}q{C_DIM} quit · {C_BOLD}e{C_DIM} expand · "
                    f"{C_BOLD}PgUp/PgDn{C_DIM} scroll · {C_BOLD}/{C_DIM} filter{C_RESET}")

            # Body is padded to the full height so the footer always sits on the
            # bottom row: without it the help line floats up as content arrives,
            # which reads as the layout jumping on every new record.
            # Above the footer, the stats row: Σ tokens, cache rate, prompt
            # average for the session currently on screen — recomputed from
            # `shown` each frame, so it follows every scope/filter switch.
            stats = session_stats(shown, fmt.width) or f"  {C_DIM}Σ —{C_RESET}"
            rows = [head] + list(view)
            rows += [""] * max(0, visible - len(view))
            rows.append(stats)
            rows.append(foot)

            def _row(r: str) -> str:
                # Erase-to-EOL is what makes a partial redraw safe: a shorter
                # line would otherwise leave the tail of the previous frame
                # behind. Rows already filling the screen need no erase.
                r = clip_plain(r, scr.width)
                if vlen(strip_ansi(r)) >= scr.width:
                    return r
                return r + "\x1b[K"

            frame = "\r\n".join(_row(r) for r in rows)
            # Repaint ONLY when something actually changed. The old loop wrote
            # `\x1b[H\x1b[2J` (home + full-screen clear) four times a second no
            # matter what, so the terminal blanked and redrew continuously —
            # that, not the content, was the flicker.
            if frame != last_frame or (scr.width, scr.height) != last_size:
                scr.out.write("\x1b[H" + frame + "\x1b[J")
                scr.out.flush()
                last_frame = frame
                last_size = (scr.width, scr.height)

            key = scr.poll_key(0.25)
            if key is None:
                continue
            if key in ("q", "ESC"):
                break
            if key == "e":
                fmt.expand = not fmt.expand      # next frame re-renders from recs
            elif key in ("PGUP", "UP"):
                scroll += max(1, visible - 2)
            elif key in ("PGDN", "DOWN"):
                scroll = max(0, scroll - max(1, visible - 2))
            elif key == "/":
                scr.out.write("\x1b[2J\x1b[H" + C_BOLD + "filter: " + C_RESET)
                scr.out.flush()
                if scr.tty_in and _HAVE_TERMIOS:
                    termios, _ = _termios()
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, scr._saved)
                filter_text = sys.stdin.readline().strip()
                if scr.tty_in and _HAVE_TERMIOS:
                    _, tty = _termios()
                    tty.setraw(sys.stdin.fileno())
                scroll = 0
                # The filter prompt overwrote the screen directly, bypassing the
                # frame cache — force a repaint so the panel does not stay
                # blank until the next content change.
                last_frame = None
    return 0


def render_view(recs: list[dict], fmt: Formatter, max_lines: int,
                visible: int, scroll: int) -> list[str]:
    """Render records, keep the last `max_lines` display lines, apply scroll.

    Call numbers are assigned here, not inside the Formatter: the panel
    re-renders every visible record on every frame, so an instance counter
    would keep climbing. Counting while walking the (chronological) list
    makes the number a pure function of the record stream — same input,
    same `call #N` — no matter how often the frame is rebuilt.
    """
    lines: list[str] = []
    call = 0
    # Adapters write BOTH llm_response and assistant_message carrying the
    # same reply (13 duplicate pairs in a real trace) — both render through
    # the same reply renderer, so every answer appeared TWICE on screen.
    # Data stays in the trace file (the CLI can diff them); the panel just
    # refuses to show a reply it already showed in this session. Only an
    # exact text match in the same session is skipped, so an error-path
    # assistant_message with different wording still renders.
    last_sid: str | None = None
    last_text = ""
    for r in recs:
        ev = r.get("event")
        if ev == "llm_request":
            call += 1
        if ev == "assistant_message":
            txt = _as_text((r.get("response") or {}).get("content"))
            if txt and txt == last_text and r.get("session_id") == last_sid:
                continue
        if ev in ("llm_response", "assistant_message"):
            last_sid = r.get("session_id")
            last_text = _as_text((r.get("response") or {}).get("content"))
        lines.extend(fmt.render(r, call))
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    # Clamp before slicing. Unclamped, `len(lines) - scroll` goes NEGATIVE
    # once the user scrolls past the top: Python's negative slice end then
    # returns a window LARGER than the viewport (and empty right at the
    # boundary), so the frame grows past the screen, the terminal scrolls,
    # and every `\x1b[H` repaint afterwards lands out of sync. The clamp
    # makes scroll-above-top pin to the first page instead.
    scroll = max(0, min(scroll, max(0, len(lines) - visible)))
    if scroll <= 0:
        return lines[-visible:]
    return lines[len(lines) - visible - scroll:len(lines) - scroll]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agenttrace watch")
    ap.add_argument("dirs", nargs="*", type=Path)
    ap.add_argument("--agent", action="append", choices=AGENTS)
    ap.add_argument("--model")
    ap.add_argument("--event")
    ap.add_argument("--since")
    ap.add_argument("--session")
    ap.add_argument("--contains", default=None)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--history", type=int, default=0,
                    help="preload N records of the current session (default 0: "
                         "live tail only)")
    ap.add_argument("--all-sessions", action="store_true",
                    help="show every session instead of only the current one")
    ap.add_argument("--follow", action="store_true", default=True)
    ap.add_argument("--no-follow", dest="follow", action="store_false")
    args = ap.parse_args(argv)
    return watch(args)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(main())
