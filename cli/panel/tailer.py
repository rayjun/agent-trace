"""agenttrace watch — following trace files as they grow

Part of the `agenttrace watch` TUI; split out of agenttrace_watch.py so each
piece has one job and one test surface. The main-loop module re-exports the
names the CLI and the tests reach for.
"""
from __future__ import annotations

import json
from pathlib import Path

from agenttrace import iter_records

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


