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
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agenttrace import default_trace_dirs, iter_records  # noqa: E402

# --------------------------------------------------------------------------
# terminal handling
# --------------------------------------------------------------------------

# Terminal control is POSIX-only. Windows users get the non-follow dump path
# (`--no-follow`), which needs no termios at all.
_HAVE_TERMIOS = os.name == "posix" and sys.platform != "win32"


def _termios():
    """Import termios/tty on demand so --no-follow works on any platform."""
    import termios
    import tty
    return termios, tty


class Screen:
    """Raw-mode line editor + frame renderer.

    stdin is put in cbreak/raw mode so single keypresses arrive without Enter.
    Falls back to line-mode input when stdin is not a tty (piped runs).
    """

    def __init__(self):
        self.tty_in = sys.stdin.isatty()
        self.out = sys.stdout
        self._saved = None
        self._pending = ""
        self.width, self.height = self._size()

    @staticmethod
    def _size():
        # A pty that was never sized reports 0 columns; treat that as unknown
        # rather than clamping to the minimum, which would clip every line.
        try:
            sz = os.get_terminal_size()
            cols = sz.columns or (int(os.environ.get("COLUMNS", 0)) or 80)
            rows = sz.lines or (int(os.environ.get("LINES", 0)) or 24)
            return max(40, cols), max(10, rows)
        except (OSError, ValueError):
            cols = int(os.environ.get("COLUMNS", 0) or 80)
            rows = int(os.environ.get("LINES", 0) or 24)
            return max(40, cols), max(10, rows)

    def __enter__(self):
        if self.tty_in and _HAVE_TERMIOS:
            termios, tty = _termios()
            self._saved = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
            self.out.write("\x1b[?1049h\x1b[?25l")   # alt screen, hide cursor
            self.out.flush()
        return self

    def __exit__(self, *_exc):
        if self._saved is not None:
            termios, _ = _termios()
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
            self.out.write("\x1b[?25h\x1b[?1049l")
            self.out.flush()
        return False

    def resize(self):
        self.width, self.height = self._size()

    def poll_key(self, timeout: float) -> str | None:
        """Return a keypress, or None on timeout. Blocking read in raw mode.

        Reads up to 64 bytes at a time and decodes ONE key per call, keeping
        the rest in `self._pending`. The old version read a single byte, then
        drained 16 more — two PgUp presses arriving within 20 ms landed as
        one blob (`\\x1b[5~\\x1b[5~`), the decoder took seq[-1] from the
        CONCATENATION and produced garbage, so held-down PgUp dropped every
        other repeat. Buffering also survives a sequence split across reads.
        """
        import select

        if not self.tty_in:
            line = sys.stdin.readline()
            return line.strip() if line else "q"
        while True:
            status, key, rest = _split_key(self._pending)
            self._pending = rest
            if status == "key":
                if key is None:
                    continue          # unknown sequence: ignored, decode next
                if len(key) == 1:
                    if key in "\r\n":
                        return "ENTER"
                    if key == "\x03":
                        return "q"
                    if key in ("\x04", "\x1a"):
                        return "q"
                return key
            # "partial": an escape sequence has started but its final byte has
            # not arrived — wait only briefly, so a bare ESC still resolves.
            wait = 0.02 if self._pending else timeout
            r, _, _ = select.select([sys.stdin], [], [], wait)
            if not r:
                if self._pending == "\x1b":
                    # Nothing followed ESC: the user pressed the back key.
                    self._pending = ""
                    return "ESC"
                return None            # partial stays buffered for next call
            # Read ONE byte normally, 64 only mid-sequence. A burst of plain
            # bytes (`/filter-text\n` written in one go) must stay in the
            # kernel buffer: the filter prompt reads it with readline(), and
            # swallowing it here would starve that read. Only after ESC —
            # where a burst MEANS concatenated key sequences (held-down PgUp:
            # `\x1b[5~\x1b[5~` in one drain) — do we take the whole blob.
            n = 64 if self._pending else 1
            self._pending += os.read(sys.stdin.fileno(), n).decode(
                "utf-8", "replace")


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

_CSI_KEYS = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT",
             "H": "HOME", "F": "END"}
_TILDE_KEYS = {"1": "HOME", "4": "END", "5": "PGUP", "6": "PGDN",
               "7": "HOME", "8": "END"}


def decode_esc(seq: str) -> str | None:
    """Map the bytes after ESC to a key name; None = ignore (never quit).

    xterm sends two families: `ESC [ <final>` for arrows/Home/End and
    `ESC [ <params> ~` for PgUp/PgDn/Home/End/Delete. The old code always
    took seq[-1] as the final byte — for `\\x1b[5~` that is `~`, which matched
    nothing and fell back to "ESC", so PgUp/PgDn (keys the footer advertises)
    QUIT the panel. Now the `~` family is parsed by its parameter, and any
    sequence we do not know returns None so it is ignored instead of killing
    the session (a stray F-key must not close the panel).
    """
    if not seq or not (seq.startswith("[") or seq.startswith("O")):
        return None
    if seq.endswith("~"):
        return _TILDE_KEYS.get(seq[1:-1])
    return _CSI_KEYS.get(seq[-1])


def _split_key(blob: str) -> tuple[str, str | None, str]:
    """Decompose the pending byte blob: (status, key, rest).

    status "key" — one complete key decoded (key None = unknown sequence,
    intentionally ignored); rest holds anything after it.
    status "partial" — an escape sequence started but its final byte is not
    in the blob yet; caller should wait for more input.
    status "empty" — nothing buffered.
    """
    if not blob:
        return ("empty", None, "")
    if blob[0] != "\x1b":
        return ("key", blob[0], blob[1:])
    if len(blob) == 1:
        return ("partial", None, blob)
    if blob.startswith(("\x1b[", "\x1bO")):
        # CSI/SS3 run until the FINAL byte (0x40-0x7E); parameters and
        # intermediates precede it. If the blob ends before a final byte the
        # sequence is split across reads — keep it whole.
        i = 2
        while i < len(blob) and not ("\x40" <= blob[i] <= "\x7e"):
            i += 1
        if i >= len(blob):
            return ("partial", None, blob)
        return ("key", decode_esc(blob[1:i + 1]), blob[i + 1:])
    # ESC followed by something that starts no sequence: the back key,
    # with the remainder queued as its own keystroke.
    return ("key", "ESC", blob[1:])


def _args_preview(arguments) -> str:
    """One-line preview of a tool_call's `arguments`.

    Prefer the argument that identifies WHAT was decided on (path/command/
    query/url/pattern — the same preference the tool summary line uses); fall
    back to a compact JSON blob. `arguments` arrives as a JSON string from
    providers, sometimes already parsed, sometimes missing.
    """
    args = arguments
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return ""
        try:
            args = json.loads(s)
        except ValueError:
            return sane(s)
    if isinstance(args, dict):
        for k in ("path", "command", "query", "url", "pattern"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return sane(v)
        try:
            blob = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            blob = str(args)
        return sane(blob)
    return sane(str(args or ""))


# -- prompt scoring --------------------------------------------------------
# Every user prompt earns ONE number, computed locally: deterministic, free,
# instant, and prompt content never leaves the machine. Four dimensions add
# up to 90, length sanity adds 10, and a long-but-vague message loses up to
# 15. The panel prints the total, a letter grade and the dimensions that
# carried it, so the score is explainable instead of mystical.

_RE_PATH = re.compile(r"https?://\S+|[\w./~-]*\.[A-Za-z]{1,6}\b|/\S{2,}")
_RE_TICK = re.compile(r"`[^`\n]+`")
_RE_QUOT = re.compile(r"[\"'][^\"'\n]{3,}[\"']")
_RE_NUM = re.compile(r"\b\d[\d,.]*\b")
_RE_ACR = re.compile(r"\b[A-Z]{2,6}\b")   # KV, LLM, API — named tech terms
_RE_ACTION = re.compile(
    r"帮我|修复|检查|生成|解释|分析|总结|优化|实现|运行|测试|对比|列出|改为|改成"
    r"|删除|添加|验证|调试|部署|安装|读取|梳理|调研|对照|跑一下"
    r"|提取|解析|转换|翻译|构建|创建|统计|重命名|汇总|校验|整理"
    r"|\b(?:review|fix|add|write|explain|check|update|implement|refactor|test"
    r"|summarize|compare|find|debug|install|deploy|run|trace|measure"
    r"|read|reply|count|list|show|give|tell|translate|convert|parse|extract"
    r"|create|delete|rename|move|copy|format|lint|verify|build|draft)\b",
    re.IGNORECASE)
_RE_CONTEXT = re.compile(
    r"因为|所以|目前|之前|已经|现在|背景|要求|不要|注意|如果|但是|同时|参考"
    r"|原因|比如|例如|应该|需要|按照|直接|不用|为什么|怎么|如何|是不是|搞懂"
    r"|明白|想知道|哪里|什么时候|先.*再"
    r"|\b(?:when|given|because|currently|however|instead|note that|for context"
    r"|make sure|instead of|after|before)\b",
    re.IGNORECASE)


def score_prompt(text: str) -> tuple[int, list[str]]:
    """Heuristic quality score for one user prompt: (0-100, strong dims).

    Dimensions (caps): specific 30 (paths/URLs/`code`/quotes/numbers/tech
    acronyms), action 30 (explicit action verbs — a terse imperative can
    score well without any background), context 15 (background/constraint
    language), structure 15 (line breaks, bullets, sentences), length 10
    (substance band). Penalty −15 when a long message has no specifics,
    context or action at all. The returned labels are the dimensions that
    scored ≥60% of their cap — what the prompt did well.
    """
    t = str(text or "")
    if not t.strip():
        return 0, []
    markers = (len(_RE_PATH.findall(t)) + len(_RE_TICK.findall(t))
               + min(3, len(_RE_QUOT.findall(t)))
               + min(4, len(_RE_NUM.findall(t)))
               + min(4, len(_RE_ACR.findall(t))))
    specific = min(30, markers * 8)
    ctx_n = len(_RE_CONTEXT.findall(t))
    context = min(15, ctx_n * 5)
    act_n = len(_RE_ACTION.findall(t))
    action = min(30, act_n * 9)
    struct = 0
    if t.count("\n") >= 1:
        struct += 4
    if len(re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)])\s", t)) >= 2:
        struct += 5
    if len(re.findall(r"[。！？!?]|(?:\.\s)", t)) >= 2:
        struct += 6
    struct = min(15, struct)
    n = len(t)
    if n < 12:
        length = 0
    elif n < 30:
        length = 4
    elif n < 60:
        length = 7
    elif n <= 4000:
        length = 10
    else:
        length = 8                      # a wall of text is not automatically good
    penalty = 15 if (specific == 0 and context == 0 and action < 9
                     and n > 60) else 0   # long but nothing concrete to act on
    score = max(0, min(100, specific + context + action + struct + length - penalty))
    dims = []
    for label, val, cap in (("specific", specific, 30), ("action", action, 30),
                            ("context", context, 15), ("structure", struct, 15),
                            ("length", length, 10)):
        if val >= 0.6 * cap:
            dims.append(label)
    return score, dims


def grade_for(score: int) -> str:
    return "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 \
        else "D" if score >= 40 else "E"


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _bar(pct: float, cells: int = 8) -> str:
    """A solid-block meter, `████████░░` for 80%.

    `█`/`░` occupy exactly one cell in every modern terminal (no wide-char
    surprises), so the bar never desyncs the frame. Pure text — the colour
    is applied by the caller around it.
    """
    n = max(0, min(cells, round(max(0.0, pct)) / 100 * cells))
    return "█" * int(n) + "░" * (cells - int(n))


def session_stats(recs: list[dict], width: int | None = None) -> str | None:
    """One row: session token spend, KV-cache hit rate, average prompt score.

    Sums the usage inside `recs` — already scoped to the current session and
    filter — so the line answers "what has THIS conversation cost", not what
    the whole trace file ever contained. Cache rate is cached_read/input
    across all replies: the single number that says how much of the input
    the provider served from prompt cache instead of recomputing it.

    With `width`, whole segments drop from the right until the line fits
    (token spend first: it is the headline number; a mid-segment hard clip
    would leave `cache 90.` — a truncated percentage is worse than none).
    """
    in_t = out_t = cached = 0
    scores: list[int] = []

    def _i(u: dict, k: str) -> int:
        try:
            return int(u.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    for r in recs:
        ev = r.get("event")
        if ev == "llm_response":
            u = (r.get("response") or {}).get("usage") or {}
            in_t += _i(u, "input_tokens")
            out_t += _i(u, "output_tokens")
            cached += _i(u, "cache_read_tokens")
        elif ev == "user_prompt":
            msgs = ((r.get("request") or {}).get("messages") or [])
            txt = "".join(_as_text(m.get("content")) for m in msgs
                          if isinstance(m, dict))
            scores.append(score_prompt(txt)[0])
    if not (in_t or out_t or scores):
        return None
    bits: list[str] = []
    if in_t or out_t:
        bits.append(f"Σ tok {C_BOLD}{_fmt_tok(in_t + out_t)}{C_RESET}"
                    f"{C_DIM} (in {_fmt_tok(in_t)} · out {_fmt_tok(out_t)}){C_RESET}")
        if in_t and cached:
            pct = 100.0 * cached / in_t
            hue = C_AI if pct >= 80 else (C_YELLOW if pct >= 50 else C_RED)
            # Number + meter together: the % reads at a glance, the bar makes
            # 92% vs 60% visible without parsing digits.
            bits.append(f"{C_DIM}cache {hue}{pct:.1f}%{C_RESET}"
                        f" {hue}{_bar(pct)}{C_RESET}")
    if scores:
        avg = round(sum(scores) / len(scores))
        hue = C_AI if avg >= 70 else (C_YELLOW if avg >= 50 else C_RED)
        bits.append(f"{C_DIM}prompt {hue}{avg}{C_RESET}{C_DIM} avg · {len(scores)} turns{C_RESET}")
    if width is not None:
        while bits:
            line = f"  {C_DIM}" + " · ".join(bits) + C_RESET
            if vlen(strip_ansi(line)) <= width:
                return line
            bits.pop()               # drop whole segments, right to left
        return None
    return f"  {C_DIM}" + " · ".join(bits) + C_RESET


C_RESET = "\x1b[0m"
C_DIM = "\x1b[2m"
C_BOLD = "\x1b[1m"
C_REV = "\x1b[7m"       # inverse: title pill only
# Two-tone content palette: what the human said vs what the model said. Keeping
# these distinct is the whole point — the old panel gave a tool result and a
# model reply the same visual weight.
C_USER = "\x1b[36m"      # cyan: the operator
C_AI = "\x1b[32m"        # green: the model
C_BLUE = "\x1b[34m"      # blue: incoming traffic (the request arrow)
C_YELLOW = "\x1b[33m"    # yellow: tool traffic (call, result, called names)
C_MAGENTA = "\x1b[35m"   # magenta: session boundaries (start/end markers)
C_RED = "\x1b[31m"       # red: failures
C_WHITE = "\x1b[37m"     # white: sixth call hue (red stays reserved for errors)
# Each LLM call owns a colour for its whole lifetime: the `call #N` banner,
# the `#N` on the request line and the same `#N` on its response. Six hues
# rotate, so a request/reply pair can be matched by number AND by colour even
# when several calls are on screen at once.
CALL_HUES = (C_USER, C_YELLOW, C_MAGENTA, C_AI, C_BLUE, C_WHITE)


def call_hue(n: int) -> str:
    """Colour for call number `n` (1-based); 0 means 'no call context'."""
    if not n:
        return ""
    return CALL_HUES[(n - 1) % len(CALL_HUES)]


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

# A long folded field is taller than this and gets the (N chars) stub instead.
FOLD_THRESHOLD = 160
# Display column where field text starts, so continuation lines stay aligned.
FIELD_W = 8
# Hard cap on expanded text. A 21K-char system prompt is unreadable in full;
# this is generous for real use and keeps the panel responsive.
EXPAND_LIMIT = 20000
# How many lines an unfolded body field may occupy. A model reply is the point
# of the panel, so it gets real room; the system prompt and tool output fold
# behind `e`. Chosen to fill a typical half-screen before the next record.
NORMAL_BODY_LINES = 14


def _take_cells(s: str, width: int) -> tuple[str, str]:
    """Split off the leading `width` display cells of `s`, counting CJK as 2.

    Returns (chunk, rest). Width-aware so a wrapped line never spills past the
    panel edge and desynchronises the layout.
    """
    out, w, i = [], 0, 0
    while i < len(s):
        ch = s[i]
        cw = 2 if _wide(ch) else 1
        if w + cw > width:
            break
        out.append(ch)
        w += cw
        i += 1
    return "".join(out), s[i:]


def _as_text(content) -> str:
    """Flatten a message content field (string, or provider part list) to text.

    Mirrors the adapters' own coercion so the panel shows what was actually
    sent rather than a JSON dump of content blocks.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                t = part.get("text") or part.get("content") or part.get("thinking")
                if t:
                    out.append(str(t))
        return "\n".join(out)
    if isinstance(content, dict):
        return _as_text(content.get("content") or content.get("text"))
    return str(content)


def vlen(s: str) -> int:
    """Display width, counting CJK as two cells."""
    import unicodedata
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def clip(s: str, width: int) -> str:
    if vlen(s) <= width:
        return s
    out, w = [], 0
    for ch in s:
        cw = 2 if _wide(ch) else 1
        if w + cw > width - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def _wide(ch: str) -> bool:
    import unicodedata
    return unicodedata.east_asian_width(ch) in ("W", "F")


def pad(s: str, width: int) -> str:
    gap = width - vlen(s)
    return s + " " * gap if gap > 0 else s


def _fit(prefix: str, body: str, suffix: str, width: int, reset: str = "") -> str:
    """Join prefix + body + suffix on one line, never exceeding `width` columns.

    The budget is derived by measuring the parts that are already fixed rather
    than by hand-tallying them. Every hand-budget in this file was wrong by a
    gap or a label at least once, and a line even 1 column too wide is worse
    than useless: the terminal hard-wraps it, and everything after it lands one
    row out of position.
    """
    fixed = vlen(strip_ansi(prefix)) + vlen(strip_ansi(suffix))
    room = width - fixed
    if room < 4:
        # Too narrow to show anything useful; keep the identity, drop the rest.
        return clip_plain(prefix, width)
    return prefix + clip(body, room) + suffix + reset


def avail_for(width: int, indent: int, label_w: int) -> int:
    """Columns left for text after `indent` and a `label_w`-wide label block.

    Single source of truth for the available-width calculation. The contract is
    one line: the result is never larger than `width - indent - label_w`, and
    never negative. Callers can therefore use it as a budget without
    re-deriving the arithmetic.

    The floor is relative, never an absolute constant. `max(20, ...)` looked
    harmless and overrode the real budget on any panel narrower than the
    constant (18 -> 20), pushing the line off the edge by the difference. And
    the "label eats everything" case must return a small positive number, not
    `width - indent`: ignoring the label there handed back more columns than
    the panel has and overflowed by 30+ on a 40-column panel.
    """
    room = width - indent - label_w
    if room >= 8:
        return room
    # No room for text: fall back to a sliver that still fits the indent.
    return max(0, min(room, width - indent))


# --------------------------------------------------------------------------
# record -> lines
# --------------------------------------------------------------------------

class Formatter:
    """Renders one record into display lines. `expanded` toggles long fields."""

    def __init__(self, expand: bool = False, width: int = 96):
        self.expand = expand
        self.width = width          # content width, excluding indent

    def _field(self, label: str, value, indent: int, color: str = "") -> list[str]:
        if value is None or value == "":
            return []
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = sane(text)
        # Continuation lines must line up under the first line's text, so the
        # label is padded to a fixed column instead of using len(label).
        pad = " " * max(0, FIELD_W - len(label))
        cont_indent = " " * (indent + FIELD_W + 2)
        lines: list[str] = []

        if not self.expand and vlen(text) > FOLD_THRESHOLD:
            # Fold to ONE line first. A system prompt starts with newlines, and
            # leaving them in turns a single budgeted record line into three
            # terminal rows — the panel then overflows by more than its budget
            # and every row below lands out of position.
            text = self._fold_one(text)
            stub = f"… ({len(text)} chars · e to expand)"
            # Reserve room for the stub so the fold notice always survives the
            # panel's own width clipping, and cut by DISPLAY columns rather
            # than characters: `text[:room]` on a CJK prompt overshoots by
            # nearly a full width and pushes the stub off the panel.
            room = avail_for(self.width, indent, FIELD_W + 2 + vlen(stub))
            if room < 4:
                # Narrow panel: the stub alone is wider than the space left for
                # the text. Shorten it rather than emit a line that cannot fit
                # — the char count is the useful part, "to expand" is not.
                stub = f"…{len(text)}c"
                room = avail_for(self.width, indent, FIELD_W + 2 + vlen(stub))
            if room < 2:
                # Even the short stub does not fit beside the label. Show the
                # stub alone: knowing there are 34k hidden characters is more
                # useful than a truncated preview of them.
                lines.append(_fit(f"{' ' * indent}{color}{label}{pad}",
                                 stub, "", self.width, C_RESET))
                return lines
            head, _ = _take_cells(text, room)
            if head != text:
                head = head.rsplit(" ", 1)[0]
            lines.append(f"{' ' * indent}{color}{label}{pad}{head}{stub}"
                         f"{C_RESET}")
            return lines

        if not self.expand:
            # The preview must be clipped to what is LEFT after the label, not
            # to the panel width: prefixing an already width-fitted preview
            # with the label overshoots by exactly the label block (10 cols),
            # and the terminal hard-wraps the line — the same bug class this
            # renderer has hit four times, here in the fold branch.
            room = self.width - indent - max(len(label), FIELD_W)
            preview = clip_plain(self._fold_one(text), room) if room > 0 else ""
            lines.append(f"{' ' * indent}{color}{label}{pad}"
                         f"{preview}{C_RESET}")
            return lines

        # Expanded: hard-wrap each source line to the panel width. The label
        # appears once, on the first line; the rest are continuation-indented.
        # The floor is a *relative* one: an absolute `max(20, ...)` silently
        # overrode the real budget on a 30-column panel (18 -> 20) and pushed
        # every continuation line 2 columns past the edge.
        avail = avail_for(self.width, indent, FIELD_W + 2)
        printed_label = False
        for raw in text[:EXPAND_LIMIT].splitlines() or [""]:
            if raw == "":
                lines.append("")
                continue
            body = raw
            while body:
                out, w = [], 0
                for ch in body:
                    cw = 2 if _wide(ch) else 1
                    if w + cw > avail:
                        break
                    out.append(ch)
                    w += cw
                if not out:                       # pathological: single wide char
                    out, body = [body[0]], body[1:]
                    chunk = "".join(out)
                else:
                    chunk = "".join(out)
                    body = body[len(chunk):]
                if not printed_label:
                    lines.append(f"{' ' * indent}{color}{label}{pad}{chunk}{C_RESET}")
                    printed_label = True
                else:
                    lines.append(f"{cont_indent}{color}{chunk}{C_RESET}")
        return lines

    def _fold_one(self, text: str) -> str:
        """Single-line preview of possibly-multiline text."""
        return sane(text).replace("\r", "").replace("\n", " ⏎ ")

    # -- what the panel is actually for -----------------------------------
    # The reader wants two things: what was SENT to the model, and what the
    # model SAID. Tool traffic is context, not content: a single read_file can
    # dump 6KB of file into the transcript, and rendering it inline buries the
    # two things that matter. So tool records collapse to one dim summary line
    # and only the prompt/reply pairs get body text.

    def _summary_line(self, rec: dict) -> str | None:
        """One dim line describing a tool interaction, or None to render fully."""
        ev = rec.get("event")
        t = rec.get("tool") or {}
        name = sane(t.get("name") or "?")
        if ev == "tool_call":
            args = t.get("args")
            detail = ""
            if isinstance(args, dict):
                # Prefer the single most identifying argument over the whole blob.
                for k in ("path", "command", "query", "url", "pattern"):
                    v = args.get(k)
                    if isinstance(v, str) and v.strip():
                        detail = v
                        break
                else:
                    detail = json.dumps(args, ensure_ascii=False)
            elif isinstance(args, str):
                detail = args
            # Assemble the line, then measure it. Hand-budgeting the columns
            # (mark, name, gaps, counter) kept missing one — a 2-space gap here,
            # a label there — and a line 2 columns too wide makes the terminal
            # hard-wrap, which desynchronises everything below it.
            tail = f"  ({len(str(args or ''))} chars args)"
            flat = sane(detail).replace("\r", "").replace("\n", " ⏎ ")
            return _fit(f"  {C_DIM}⚙ {C_YELLOW}{name}{C_RESET}{C_DIM}  ",
                        flat, tail, self.width, C_RESET)

        if ev == "tool_result":
            body = (rec.get("response") or {}).get("content") or ""
            status = t.get("status") or ""
            n = len(str(body))
            err = rec.get("error")
            bad = status == "error" or bool(err)
            mark = "✗" if bad else "✓"
            mark_c = C_RED if bad else C_AI
            # Collapsed is a STATUS line, not a content line: size + `e` is all
            # a healthy result earns. A read_file answer is 6KB of JSON that
            # buries the prompt/reply this panel exists for — `e` shows it.
            # A failure is the one case where the first words matter, so it
            # keeps a short clipped preview (clip in _fit bounds the width).
            # One assembly rule for all three cases:
            #   name block (ends with 2 spaces) + middle + " · e".
            head = (f"  {mark_c}{mark} {C_YELLOW}{name}{C_RESET}"
                    f"{C_DIM}  ")
            hint = f" · {C_BOLD}e"
            if bad:
                preview = self._fold_one(
                    str(err.get("message") if isinstance(err, dict) else "")
                ) or self._fold_one(str(body)) or ""
                return _fit(head, preview or "(no output)", hint,
                            self.width, C_RESET)
            if not str(body):
                return _fit(head, "(no output)", hint, self.width, C_RESET)
            return _fit(head, f"{n} chars", hint, self.width, C_RESET)
        return None

    def render(self, rec: dict, call: int = 0) -> list[str]:
        """Render one record. `call` is the current LLM-call sequence number
        (assigned by render_view, 0 = no call context, e.g. a standalone
        render in a test) — it is what ties a request, its reply and the
        `call #N` banner together as one complete call."""
        ev = rec.get("event", "?")
        agent = rec.get("agent", "?")

        # Tool traffic collapses to one dim line — but only while collapsed.
        # With `e` held down the reader asked for the real args/output, so the
        # full renderer takes over.
        if not self.expand:
            summary = self._summary_line(rec)
            if summary is not None:
                return [summary]

        # session_start / session_end are structural, not conversational.
        if ev in ("session_start", "session_end"):
            ts = (rec.get("ts") or "")[11:19]
            note = clip(sane(rec.get("note") or "").replace("\n", " "), max(10, self.width - 30))
            # Magenta marks the boundary between conversations — the one line
            # that answers "where did this session start/end" while scrolling.
            return [f"{C_MAGENTA}{ts} {rec.get('session_id', '')[-8:]} {ev}{C_RESET}"
                    + (f" {C_DIM}· {note}{C_RESET}" if note else "")]

        if ev == "llm_request":
            return self._render_request(rec, agent, call)
        if ev in ("llm_response", "assistant_message"):
            return self._render_response(rec, agent, call)
        if ev == "llm_error":
            return self._render_error(rec, agent, call)
        if ev == "user_prompt":
            return self._render_user(rec, agent)
        if ev in ("tool_call", "tool_result"):
            return self._render_tool(rec)
        return self._render_other(rec)

    def _render_tool(self, rec: dict) -> list[str]:
        """Full tool rendering — only reached with `e` held (see render())."""
        t = rec.get("tool") or {}
        status = t.get("status") or ""
        head = f"  {C_BOLD}⚙ {sane(t.get('name', '?'))}{C_RESET}"
        if status:
            head += f" {C_DIM}{sane(status)}{C_RESET}"
        out = [head, ""]
        if rec.get("event") == "tool_call":
            out += self._field("args", t.get("args"), 2, C_DIM)
        else:
            out += self._body("result",
                              _as_text((rec.get("response") or {}).get("content")),
                              C_DIM, 2)
            if rec.get("error"):
                out.append("")
                out += self._field("err", rec["error"].get("message"), 2, C_RED)
        return out

    # -- per-event renderers ------------------------------------------------

    def _meta_line(self, rec: dict, lead: str, call: int = 0) -> str:
        """`  ← codex llm_response · gpt-5.5 · 2 msgs · 24 tools · 18.8k chars`.

        Agent and event both stay on the line. With several agents writing into
        the same panel — the default, since `watch` tails every trace dir at
        once — the agent name is the only thing that says whose turn this is,
        and the event name is the cheapest way to tell a request from the reply
        that answered it.

        Bits are dropped from the right until the line fits. Order encodes
        importance (who/what/how long, then counts), so a long model name costs
        the least useful field rather than pushing the line off the panel.
        """
        head = [lead, sane(rec.get("agent", "?")), sane(rec.get("event", "?"))]
        tail: list[str] = []
        for k in ("model", "api_mode"):
            if rec.get(k):
                tail.append(sane(rec[k]))
        req = rec.get("request")
        if isinstance(req, dict):
            for label, key, fmt in (("msgs", "message_count", "{}"),
                                    ("tools", "tool_count", "{}"),
                                    ("tokens", "approx_input_tokens", "~{}")):
                v = req.get(key)
                if v is not None:
                    tail.append(f"{label} {fmt.format(v)}")
            ch = req.get("char_count")
            if ch:
                tail.append(f"{ch / 1000:.1f}k chars")

        # `head` is never dropped: without it the line is unattributable.
        # The arrow carries direction (blue in, green out) so scanning down the
        # panel you can see request/reply pairing without reading the words.
        # `#N` (in the call's colour) says WHICH request/reply pair this is.
        arrow = C_BLUE if str(head[0]).startswith("→") else C_AI
        tag = f"{C_BOLD}{call_hue(call)}#{call}{C_RESET}{C_DIM} " if call else ""
        pre = f"  {arrow}{head[0]}{C_RESET}{C_DIM} {tag}{' · '.join(head[1:])}"
        pre = clip_plain(pre, self.width)
        tail_bits = tail
        while tail_bits and vlen(strip_ansi(pre)) + 3 + vlen(tail_bits[0]) > self.width:
            tail_bits.pop(0)
        if not tail_bits:
            return pre + C_RESET
        return _fit(pre + " · ", " · ".join(tail_bits), "", self.width, C_RESET)

    def _body(self, label: str, text: str, color: str = "", indent: int = 2) -> list[str]:
        """Render one body field. Unfolded: the first real lines, hard-wrapped.

        Unlike the old `_field`, an unfolded body is NOT capped at
        FOLD_THRESHOLD. A model reply is the thing the reader opened this panel
        to see; truncating it to 160 chars to make room for a fold notice is
        backwards. It wraps across as many lines as it needs, and `e` is for
        the genuinely huge fields (system prompt, tool output).
        """
        if not text:
            return []
        if not self.expand:
            return self._wrap_body(label, text, color, indent,
                                   limit=NORMAL_BODY_LINES)
        return self._field(label, text, indent, color)

    def _wrap_body(self, label: str, text: str, color: str, indent: int,
                   limit: int) -> list[str]:
        """Hard-wrap text to the panel, keeping source line structure.

        The label is part of the line, so it comes out of the width budget. A
        `user` label plus its two-space gap is 7 columns; budgeting only
        `width - indent - 2` let every labelled line overshoot the panel by
        that much, which the terminal then hard-wraps and desynchronises the
        whole layout.
        """
        # The label renders as a reverse-video tag — ` you ` — so the role
        # reads as a block marker rather than a word. The pill's padding is
        # part of the label, so the width budget is derived from the assembled
        # head instead of a hand-counted label length (this function exists
        # because hand-counted budgets kept overshooting by exactly that).
        text = sane(text)
        head = (f"{' ' * indent}{C_REV}{C_BOLD}{color} {label} {C_RESET}"
                if label else "")
        # Columns the label block occupies: indent + pill + 2-space gap.
        label_w = (vlen(strip_ansi(head)) - indent) + 2 if label else 0
        avail = avail_for(self.width, indent, label_w)
        lines: list[str] = []
        printed_label = False
        cont = f"{' ' * (indent + label_w)}"

        for raw in text.splitlines()[:limit] or [""]:
            if raw == "":
                if lines:
                    lines.append("")
                continue
            body = raw
            while body:
                chunk, body = _take_cells(body, avail)
                if not chunk:
                    break
                if not printed_label and label:
                    lines.append(f"{head}  {chunk}{C_RESET}" if color else
                                 f"{head}  {chunk}")
                    printed_label = True
                else:
                    lines.append(f"{cont}{chunk}")
                if len(lines) >= limit:
                    break
            if len(lines) >= limit:
                break
        if len(lines) >= limit and text.strip().count("\n") >= limit - 1:
            lines.append(f"{cont}{C_DIM}… {len(text)} chars total · e to expand{C_RESET}")
        return lines

    def _call_banner(self, rec: dict, call: int) -> str:
        """Full-width rule that opens LLM call #N.

        This is the answer to "where does one complete call start?" — before
        it, everything belonged to the previous call (or to your prompt);
        after it, the request, its reply and the tools that reply triggered
        form one block. The number carries the block's colour so the matching
        `#N` on the request and reply lines reads as one pair.
        """
        hue = call_hue(call)
        model = sane(rec.get("model") or "-")
        ts = (rec.get("ts") or "")[11:19]
        label = f"call #{call}"
        tail_txt = f"{model} · {ts}" if ts else model
        # Measure the visible text, then fill the rest of the row with the
        # rule so the divider spans the panel — assembled, never hand-counted.
        fill = max(0, self.width - vlen(f"── {label} · {tail_txt} "))
        line = (f"{C_DIM}── {C_RESET}{C_BOLD}{hue}{label}{C_RESET}"
                f"{C_DIM} · {tail_txt} {C_DIM}{'─' * fill}{C_RESET}")
        # Narrow panel: the fill collapses to zero first, then the tail is
        # clipped — `call #N` sits at the front and survives.
        return clip_plain(line, self.width)

    def _via_line(self, rec: dict) -> str:
        """`  via opencode-go · chat · https://… · max_tok 8192`.

        How the request LEAVES this machine — provider, API mode, endpoint
        and the response ceiling when the adapter records one. The meta line
        says what the request contains; this one says where it goes.
        """
        bits = []
        for v in (rec.get("provider"), rec.get("api_mode"), rec.get("base_url")):
            if v:
                bits.append(sane(str(v)))
        req = rec.get("request")
        if isinstance(req, dict) and req.get("max_tokens"):
            try:
                bits.append(f"max_tok {int(req['max_tokens'])}")
            except (TypeError, ValueError):
                pass
        if not bits:
            return ""
        return _fit(f"  {C_DIM}via {C_WHITE}", " · ".join(bits), "",
                    self.width, C_RESET)

    def _anatomy_line(self, sysp: str, conv: list) -> str:
        """`  anatomy system 12.4k (22%) · conv 41.9k (78%) · msgs u5 a4 t9`.

        The meta line counts messages and tools; this splits the PROMPT ITSELF
        into system vs conversation with their share of the payload, plus the
        role histogram — the two numbers that decide what a request costs.
        """
        sys_c = len(str(sysp or ""))
        conv_c = sum(len(_as_text(m.get("content"))) for m in conv)
        total = sys_c + conv_c
        bits = []
        if sys_c:
            pct = f" ({100 * sys_c // total}%)" if total else ""
            bits.append(f"system {C_BOLD}{sys_c / 1000:.1f}k{C_RESET}{C_DIM}{pct}")
            bits[-1] += C_RESET
        if conv_c:
            pct = f" ({100 * conv_c // total}%)" if total else ""
            bits.append(f"conv {C_BOLD}{conv_c / 1000:.1f}k{C_RESET}{C_DIM}{pct}{C_RESET}")
        u = sum(1 for m in conv if m.get("role") == "user")
        a = sum(1 for m in conv if m.get("role") == "assistant")
        t = sum(1 for m in conv if m.get("role") not in ("user", "assistant"))
        if u or a or t:
            bits.append(f"msgs u{u} a{a} t{t}")
        if not bits:
            return ""
        return _fit(f"  {C_DIM}anatomy ", " · ".join(bits), "",
                    self.width, C_RESET)

    def _render_request(self, rec: dict, agent: str, call: int = 0) -> list[str]:
        req = rec.get("request")
        if isinstance(req, list):
            req = {"messages": req, "message_count": len(req)}
        elif not isinstance(req, dict):
            req = {}

        out: list[str] = []
        if call:
            out.append(self._call_banner(rec, call))
        out.append(self._meta_line(rec, "→", call))
        via = self._via_line(rec)
        if via:
            out.append(via)

        # A system prompt reaches the panel two ways depending on the adapter:
        # as a dedicated `system_prompt` field, or as a `system` message inside
        # `messages` (which is what the Hermes adapter does). Prefer the field
        # but fall back to the message, or the prompt silently disappears.
        sysp = (req.get("system_prompt") or req.get("instructions") or "")
        msgs = [m for m in (req.get("messages") or []) if isinstance(m, dict)]
        if not sysp:
            for m in msgs:
                if m.get("role") == "system":
                    sysp = _as_text(m.get("content"))
                    break
        conv = [m for m in msgs if m.get("role") != "system"]
        anat = self._anatomy_line(sysp, conv)
        if anat:
            out.append(anat)
        # The full prompt is system + conversation + TOOL SCHEMAS — the
        # schemas are a real part of the payload (25 tools ≈ several k
        # tokens) and the count alone says nothing about what the model can
        # call. List the names; `_fit` clips to the panel.
        tools = req.get("tools")
        if isinstance(tools, list) and tools:
            names = []
            for t in tools:
                if isinstance(t, dict):
                    n = t.get("name") or (t.get("function") or {}).get("name")
                else:
                    n = t
                if n:
                    names.append(sane(str(n)))
            if names:
                out.append(_fit(f"  {C_DIM}tools {C_RESET}", ", ".join(names),
                                "", self.width, C_RESET))
        out.append("")
        if sysp:
            out += self._field("system", sysp, 2, C_DIM)
            out.append("")

        # An agent request carries the WHOLE conversation — by turn 20 that is
        # hundreds of messages and megabytes of replay. The panel's job is the
        # exchange in front of you, so history collapses to a count and only the
        # last user message is shown. This is the single biggest readability
        # win in the panel: without it, one llm_request buried every live view.
        conv = [m for m in msgs if m.get("role") != "system"]
        if not conv:
            return out

        # Anchor on the last USER message, not the last message: by the time a
        # turn is on its 2nd LLM call the tail of the request is the previous
        # turn's tool result — raw JSON, and the opposite of what the reader
        # opened the panel for.
        users = [i for i, m in enumerate(conv) if m.get("role") == "user"]
        anchor = users[-1] if users else len(conv) - 1
        if anchor:
            # What the collapse hides, summarized: role histogram (first-seen
            # order) + how many characters of the prompt live back there.
            earlier = conv[:anchor]
            order: list[str] = []
            counts: dict[str, int] = {}
            for m in earlier:
                r = str(m.get("role") or "?")
                if r not in counts:
                    order.append(r)
                    counts[r] = 0
                counts[r] += 1
            roles = " ".join(f"{r[0]}{counts[r]}" for r in order)
            chars = sum(len(_as_text(m.get("content"))) for m in earlier)
            hist = (f"… {anchor} earlier msg{'s' if anchor != 1 else ''}"
                    + (f" ({roles} · {_fmt_tok(chars)} chars)" if roles else ""))
            out.append(_fit(f"  {C_DIM}", hist, "", self.width, C_RESET))

        last = conv[anchor]
        role = last.get("role", "user")
        label = sane("user" if role == "user" else role)
        color = C_USER if role == "user" else C_DIM
        out += self._body(label, _as_text(last.get("content")), color, 2)
        # Score the anchor right where it sits: the you-block's `prompt …`
        # line sits far up the stream by turn 3; inside a request the anchor
        # is the message the reader is actually looking at.
        if role == "user":
            out.append(self._score_line(_as_text(last.get("content"))))
        return out

    def _render_response(self, rec: dict, agent: str, call: int = 0) -> list[str]:
        resp = rec.get("response") or {}
        u = resp.get("usage") or {}
        d = rec.get("duration_ms")
        # Same fitting path as the request meta line: a reply row carries
        # model + duration + six usage figures and will overflow a narrow
        # panel if it is not measured rather than assumed.
        # Green `←` pairs with the blue `→` above it: one request/reply cycle
        # reads as two coloured marks down the left edge. The `#N` (in the
        # call's colour) is the explicit half of that pairing — it matches the
        # `#N` on the request line and on the `call #N` banner.
        tag = f"{C_BOLD}{call_hue(call)}#{call}{C_RESET}{C_DIM} " if call else ""
        pre = clip_plain(
            f"  {C_AI}←{C_RESET}{C_DIM} {tag}{sane(rec.get('model') or '-')}",
            self.width)
        rest: list[str] = []
        if d is not None:
            try:
                rest.append(f"{int(d)}ms")
            except (TypeError, ValueError):
                pass

        def _n(v):
            """usage value as int, or None when absent/unusable."""
            try:
                return None if v is None else int(v)
            except (TypeError, ValueError):
                return None

        # Two numbers raw counts don't give, both derived from usage:
        #   tok   — what THIS call consumed (input + output).  In an agent loop
        #           the input carries the whole history, so this is the cost
        #           of this turn, not of the conversation.
        #   cache — hit rate of that input against the prompt cache.  It is
        #           what separates "13318 input tokens" (expensive) from
        #           "13318 input, 13298 cached" (nearly free).
        in_t = _n(u.get("input_tokens"))
        out_t = _n(u.get("output_tokens"))
        cache_t = _n(u.get("cache_read_tokens"))
        if in_t is not None or out_t is not None:
            rest.append(f"tok {(in_t or 0) + (out_t or 0)}")
        for label, val in (("in", in_t), ("out", out_t)):
            if val is not None:
                rest.append(f"{label} {val}")
        if cache_t is not None:
            hit = 100.0 * cache_t / in_t if in_t else None
            if hit is not None:
                # `cache 99.8% ████████ 97152` — rate first with a meter so
                # the KV hit is visible without reading digits; the raw
                # cached tokens stay at the end for the exact figure.
                hue = C_AI if hit >= 90 else (C_YELLOW if hit >= 50 else C_RED)
                rest.append(f"cache {hue}{hit:.1f}%{C_RESET}"
                            f" {hue}{_bar(hit)}{C_RESET}"
                            f" {C_DIM}{cache_t}{C_RESET}")
            else:
                rest.append(f"cache {cache_t}")
        if resp.get("finish_reason"):
            rest.append(sane(resp["finish_reason"]))
        # Drop segments from the head until the whole line fits — but the
        # ruler must be THE SAME ONE _fit uses, or a line that passes here
        # gets decapitated there:
        #   * _fit measures the PREFIX with strip_ansi (display columns) …
        #   * … and clips the BODY with clip(), whose first check is a raw
        #     vlen() that counts escape sequences as visible characters.
        # Measuring the join with strip_ansi under-counted by ~20 columns
        # (one coloured segment ≈ 16 bytes of CSI), so the loop passed a
        # line that clip() then cut mid-escape (`cache 99.8% ████ \x1b[2…`
        # — the cached token count gone). Mirror the hybrid: strip the
        # pre, keep the join raw.
        # Priority order when dropping: head counters first (ms/tok/in/out),
        # then finish — cache dies LAST, it is the figure the reader came
        # for and it must survive a narrow panel.
        while rest and (vlen(strip_ansi(pre)) + 3
                        + vlen(" · ".join(rest))) > self.width:
            drop = next((i for i, s in enumerate(rest)
                         if not s.startswith("cache")), None)
            rest.pop(0 if drop is None else drop)
        if rest:
            head_line = _fit(pre + " · ", " · ".join(rest), "", self.width, C_RESET)
        else:
            head_line = _fit(pre, "", "", self.width, C_RESET)
        out = [head_line, ""]
        out += self._body("ai", _as_text(resp.get("content")), C_AI, 2)
        if resp.get("reasoning"):
            out.append("")
            out += self._field("think", _as_text(resp.get("reasoning")), 2, C_DIM)

        calls = resp.get("tool_calls") or []
        if calls:
            out.append("")
            for c in calls:
                if not isinstance(c, dict):
                    continue
                # A decision is the model's CHOICE, shown where it was made:
                # inside the reply that made it. The tool_call record below
                # then shows execution; this line shows intent (name + the
                # one argument that identifies it), numbered by call block.
                name = sane(c.get("name") or "?")
                prev = sane(_args_preview(c.get("arguments")))
                head = (f"  {C_BOLD}{call_hue(call)}↳{C_RESET}{C_DIM} decide "
                        f"{C_RESET}{C_YELLOW}{name}{C_RESET}{C_DIM}  ")
                out.append(_fit(head, prev, "", self.width, C_RESET))
        if not resp.get("content") and not calls:
            note = rec.get("note")
            if note:
                out += self._field("note", note, 2)
        return out

    def _render_error(self, rec: dict, agent: str, call: int = 0) -> list[str]:
        err = rec.get("error") or {}
        # The failed call keeps its number (in the call's hue) so the reader
        # can tell WHICH request never got a reply; the rest stays red.
        tag = f"{C_BOLD}{call_hue(call)}#{call}{C_RESET}{C_RED} " if call else ""
        head = f"✗ {tag}error · {sane(rec.get('model') or '-')}"
        if err.get("status_code"):
            head += f" · HTTP {err['status_code']}"
        if err.get("retryable") is not None:
            head += f" · retryable={err['retryable']}"
        # The head carries model, status code and retryability — several
        # variable-length parts assembled by string concat, so it gets the
        # same clip every other free-form line gets.
        out = [clip_plain(f"  {C_RED}{head}{C_RESET}", self.width), ""]
        out += self._body("err", _as_text(err.get("message")), C_RED, 2)
        if err.get("type"):
            out.append(f"  {C_DIM}{sane(err['type'])}{C_RESET}")
        return out

    def _score_line(self, text: str) -> str:
        """One line: `prompt 75/100 grade B ████████░░ · specific · action`.

        Score, letter grade AND a block meter — the number alone was too
        easy to miss in the stream. Hue: green ≥70, yellow ≥50, red below.
        Local and deterministic (score_prompt): same text, same number,
        and the prompt never leaves this machine.
        """
        score, dims = score_prompt(text)
        grade = grade_for(score)
        hue = C_AI if score >= 70 else (C_YELLOW if score >= 50 else C_RED)
        detail = " · ".join(dims) if dims else "thin specifics"
        head = (f"  {C_DIM}prompt {C_RESET}{C_BOLD}{hue}{score}{C_RESET}"
                f"{C_DIM}/100 grade {hue}{C_BOLD}{grade}{C_RESET}"
                f"{C_DIM} · {C_RESET}{hue}{_bar(score, 8)}{C_RESET}"
                f"{C_DIM} · {C_RESET}")
        return _fit(head, detail, "", self.width, C_RESET)

    def _render_user(self, rec: dict, agent: str) -> list[str]:
        req = rec.get("request") or {}
        out = []
        texts = [m for m in (req.get("messages") or []) if isinstance(m, dict)]
        for m in texts:
            out += self._body("you", _as_text(m.get("content")), C_USER, 2)
        if rec.get("note"):
            out.append(f"  {C_DIM}{clip(sane(rec['note']), max(10, self.width - 6))}{C_RESET}")
        # One prompt, one score — see _score_line for the format and why the
        # heuristic is local (deterministic, free, content never leaves).
        out.append(self._score_line(
            "".join(_as_text(m.get("content")) for m in texts)))
        return out

    def _render_other(self, rec: dict) -> list[str]:
        note = rec.get("note")
        if not note:
            return []
        return [f"  {C_DIM}· {clip(sane(note).replace(chr(10), ' '), self.width - 4)}{C_RESET}"]

# --------------------------------------------------------------------------
# tailer
# --------------------------------------------------------------------------

class Tailer:
    """Watches trace files for appended lines. Emits (record) for each new one."""

    def __init__(self, dirs: list[Path], seed_eof: bool = True):
        self.dirs = dirs
        self.offsets: dict[Path, int] = {}
        self.sizes: dict[Path, int] = {}
        for d in dirs:
            for p in sorted(d.rglob("*.jsonl")):
                self._note(p, seed=seed_eof)

    def _note(self, p: Path, seed: bool = False):
        try:
            st = p.stat()
        except OSError:
            return
        if p not in self.offsets:
            # Files that appear while we are watching are read from byte 0,
            # otherwise the records written to them before we noticed would be
            # silently skipped. Only files present at startup are seeded to
            # EOF, so watch does not replay all of history on launch.
            start = st.st_size if seed else 0
            self.offsets[p] = start
            self.sizes[p] = start

    def new_files(self) -> None:
        for d in self.dirs:
            for p in sorted(d.rglob("*.jsonl")):
                self._note(p)

    def poll(self) -> list[dict]:
        self.new_files()
        recs = []
        for p, off in list(self.offsets.items()):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size < self.sizes.get(p, 0):
                # truncated / rotated: restart from zero
                off = 0
            if st.st_size == self.sizes.get(p, st.st_size) and st.st_size == off:
                self.sizes[p] = st.st_size
                continue
            try:
                with p.open("rb") as fh:
                    fh.seek(off)
                    data = fh.read()
            except OSError:
                continue
            # Bytes after the last newline are a line still being written:
            # hold the offset (do not advance it) until the newline lands.
            #
            # Reading this way — binary, one read, cut at the last b"\n" — is
            # what makes partial writes safe. The old text-mode loop did
            # `for line in fh: if not line.endswith("\n"): break` and then
            # called `fh.tell()`; on a buffered reader tell() raises OSError
            # ("telling position disabled by next() call") whenever iteration
            # stopped mid-stream, and the except swallowed the WHOLE tail of
            # the loop — so neither the offset nor the size advanced and every
            # complete line before the partial one was re-emitted on the next
            # poll (one record, four emissions per second, live-reproduced).
            # A partial line left behind by a killed writer now waits instead
            # of corrupting what arrives after it.
            cut = data.rfind(b"\n")
            if cut < 0:
                self.sizes[p] = st.st_size
                continue
            for raw in data[:cut + 1].split(b"\n"):
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:   # includes UnicodeDecodeError
                    continue
                if isinstance(obj, dict) and obj.get("event"):
                    recs.append(obj)
            self.offsets[p] = off + cut + 1
            self.sizes[p] = st.st_size
        return recs


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def load_history(dirs: list[Path], keep, history: int) -> list[dict]:
    """Preload the last `history` records that pass the filter.

    The filter and the history size are passed in rather than re-derived from
    argparse, so preloaded history and live-tailed records go through the
    identical predicate. It used to call agenttrace.matches (strict `agent == value`
    equality) while the live loop used its own `in` membership test, so the two
    could disagree about the same record depending on which path it arrived by,
    and it read a module-global `args` that never existed.
    """
    recs = [r for _, _, r in iter_records(dirs) if keep(r)]
    recs.sort(key=lambda r: r.get("ts") or "")
    return recs[-history:] if history else recs


def latest_session(dirs: list[Path]) -> str | None:
    """The session id of the most recently written record, or None.

    Scanned backwards over file tails rather than by reading whole files: a
    busy day leaves megabytes of JSONL per agent, and the only thing needed to
    scope the panel is the id of the session still being appended to. Reading
    the 680-record default trace this way costs ~0.5ms; reading it whole to
    find the same answer costs ~550ms.

    Every trace file contributes its last parsable record, and the winner is the
    one with the greatest timestamp. Only the last 64KB of each file is read.
    Sorting by filename (the obvious shortcut) would be wrong: trace files are
    named after their agent, not their date, so `codex-…jsonl` sorts before
    `hermes-…jsonl` regardless of which was written last.
    """
    best_id: str | None = None
    best_key: tuple[str, float] = ("", -1.0)
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            files = [p for p in d.rglob("*.jsonl") if p.is_file()]
        except OSError:
            continue
        for p in files:
            try:
                st = p.stat()
                with p.open("rb") as fh:
                    # 64KB from the end is plenty to reach the final records.
                    fh.seek(max(0, st.st_size - 65536))
                    tail = fh.read().decode("utf-8", "replace")
            except OSError:
                continue
            # The last parsable record in the file is the most recent one it
            # holds; its ts is what matters, mtime only breaks ties between
            # files written in the same instant.
            for line in reversed(tail.splitlines()):
                if '"session_id"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                sid, ts = rec.get("session_id"), rec.get("ts") or ""
                if sid and (ts, st.st_mtime) >= best_key:
                    best_id, best_key = sid, (ts, st.st_mtime)
                break
    return best_id


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


# Record content is THIRD PARTY: tool output, web pages, file contents can
# carry ANSI/OSC sequences of their own. Unsantised, an embedded `\x1b[2J`
# clears the panel from inside a tool result, `\x1b]0;…\x07` sets the window
# title, and OSC-52 can push the clipboard (all reproduced rendering the raw
# sequence byte-for-byte). Only record-derived text passes through here —
# never the panel's own chrome, whose colours are exactly what gets removed.
_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.S)
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CTL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")   # keeps \t and \n


def sane(text) -> str:
    """Strip escape/control sequences out of record-derived display text."""
    t = str(text)
    t = _OSC_RE.sub("", t)      # OSC (title, clipboard) incl. the ESC itself
    t = _CSI_RE.sub("", t)      # CSI: cursor moves, erases, alt-screen …
    t = _CTL_RE.sub("", t)      # remaining C0/C1, incl. lone ESC; keeps \t \n
    return t


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def clip_plain(line: str, width: int) -> str:
    """Clip a coloured line to the panel width, keeping the trailing reset."""
    plain_w = vlen(re.sub(r"\x1b\[[0-9;]*m", "", line))
    if plain_w <= width:
        return line
    out, w, i = [], 0, 0
    while i < len(line):
        if line[i] == "\x1b":
            j = line.find("m", i)
            if j == -1:
                break
            out.append(line[i:j + 1])
            i = j + 1
            continue
        cw = 2 if _wide(line[i]) else 1
        if w + cw > width - 1:
            break
        out.append(line[i])
        w += cw
        i += 1
    out.append(C_RESET)
    return "".join(out)


def re_sub(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agenttrace watch")
    ap.add_argument("dirs", nargs="*", type=Path)
    ap.add_argument("--agent", action="append", choices=["hermes", "codex", "pi"])
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
