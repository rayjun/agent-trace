"""Rendering modules for the `agenttrace watch` TUI.

    text.py       colours, display-width maths, ANSI sanitising
    scoring.py    prompt-quality heuristic
    stats.py      the session spend row
    terminal.py   raw termios input + Screen
    formatter.py  record -> display lines
    tailer.py     incremental file following

`agenttrace_watch` imports all of these and re-exports their public names, so
callers keep using `agenttrace_watch.Formatter` and friends.

The shared trace helpers live at the repo root in `common/`. Bootstrap the
import path HERE rather than in each submodule: `panel` must be importable on
its own (tests do `import agenttrace_watch`, which sets the path first, but a
submodule imported directly would otherwise fail), and one probe is enough
because `panel/__init__` always runs before any `panel.x`.
"""
import sys
from pathlib import Path

_common = Path(__file__).resolve().parent.parent.parent / "common"
if _common.is_dir() and str(_common) not in sys.path:
    sys.path.insert(0, str(_common))
