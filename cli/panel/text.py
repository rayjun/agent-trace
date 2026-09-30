"""agenttrace watch — colours, display-width maths and output sanitising

Part of the `agenttrace watch` TUI; split out of agenttrace_watch.py so each
piece has one job and one test surface. The main-loop module re-exports the
names the CLI and the tests reach for.
"""
from __future__ import annotations

import re

from agenttrace_common import flatten_content

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


# The panel must show the SAME text the adapters recorded, so it does not keep
# its own copy of the flattening rules — one implementation in
# common/agenttrace_common.py is imported here. Three near-identical versions
# of this function used to exist (hermes, codex, watch); a part shape added to
# one and not the others made the panel disagree with the trace file about
# what a message said.
_as_text = flatten_content


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



# The panel and the adapters must agree on what a message said, so this is the
# shared flattening rule rather than a fourth local copy: three near-identical
# versions (hermes, codex, watch) drifted on which content parts they read.
_as_text = flatten_content
