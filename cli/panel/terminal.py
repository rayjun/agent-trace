"""agenttrace watch — raw-mode keyboard input and the screen surface

Part of the `agenttrace watch` TUI; split out of agenttrace_watch.py so each
piece has one job and one test surface. The main-loop module re-exports the
names the CLI and the tests reach for.
"""
from __future__ import annotations

import os
import sys

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


