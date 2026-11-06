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

#: Bounds for the FILE fallback (no index yet): how many files to sample and how
#: many bytes of each. A live panel needs recent context; `agenttrace session`
#: and the index are the tools for exact, complete history.
PRELOAD_TAIL_BYTES = 2 << 20
#: Total bytes the preload may read before it gives up filling `history`. 600
#: real records are ~18 MB, so a normal request is exact; the cap is what stops a
#: pathological one (a filter matching nothing) from reading gigabytes.
PRELOAD_BUDGET_BYTES = 128 << 20
PRELOAD_MAX_FILES = 6
#: Ordering inside one instant. Files are not in timestamp order (a backfill
#: appends August records into a file written today), so "the last N lines" is
#: NOT "the last N by ts" — verified on the traces here: the tail's oldest record
#: was 2026-09-02 where the exact last 200 went back only to 2026-10-17. The index
#: is ordered by (ts, rowid) and is the primary source; files are the fallback.


def _tail_records(path: Path, want: int, keep,
                  max_bytes: int | None = None) -> tuple[list[dict], int]:
    """Records from the END of one file, reading backwards in growing blocks.

    Reading whole files made `watch` start in 23 seconds on the 2.4 GiB this
    machine holds: it parsed every record in every file, sorted them, and kept
    the last few hundred. The records a live panel needs are at the tail of the
    newest files, so the tail is what gets read — newest first, stopping as soon
    as `want` matching records are in hand.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0
    limit = PRELOAD_TAIL_BYTES if max_bytes is None else max_bytes
    collected: list[dict] = []
    pos = size
    stop_at = max(0, size - limit)
    block = min(256 << 10, max(1, limit))
    spent = 0
    leftover = b""          # the (possibly half) line at the START of the window
    while pos > stop_at and len(collected) < want:
        step = min(block, pos - stop_at)
        try:
            with path.open("rb") as fh:
                fh.seek(pos - step)
                chunk = fh.read(step)
        except OSError:
            break
        spent += len(chunk)
        data = chunk + leftover
        lines = data.split(b"\n")
        if pos - step > stop_at:
            leftover = lines[0]            # incomplete: finish it next block
            complete = lines[1:]
        else:
            leftover = b""
            complete = lines
        # `reversed` walks the block newest-line-first, which is what makes the
        # read stop early. `collected` therefore stays NEWEST-FIRST across blocks
        # (a later block is an older window), and is flipped once on return — so
        # two records stamped with the SAME ts keep the order they were written
        # in. Sorting by ts alone reversed them, and equal timestamps are common
        # in real traces (147 in a 2 MiB sample).
        for raw in reversed(complete):
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(rec, dict) and keep(rec):
                collected.append(rec)
                if len(collected) >= want:
                    break
        pos -= step
        block = min(block * 2, PRELOAD_TAIL_BYTES)
    return list(reversed(collected)), spent   # newest-first -> file order


def _epoch(ts: str) -> float:
    """Epoch SECONDS from an ISO-8601 ts, for comparing against mtime."""
    try:
        from datetime import datetime, timezone
        base = ts.rstrip("Z")
        fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in base else "%Y-%m-%dT%H:%M:%S"
        return datetime.strptime(base, fmt).replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _tails_newer_than(dirs: list[Path], keep, newer_than: str,
                      want: int, slack_s: float = 120.0) -> list[dict]:
    """File records newer than `newer_than` — the gap the index has not scanned.

    Trace files are append-only, so a file NOT written since `newer_than` cannot
    hold anything later than it: the mtime check is exact, not a heuristic. It
    matters: reading six file tails to find two records cost 1.9s, and the file
    that actually had them was the newest one.
    """
    floor = _epoch(newer_than) - slack_s
    files: list[Path] = []
    for d in dirs:
        try:
            for p in d.rglob("*.jsonl"):
                try:
                    if p.is_file() and p.stat().st_mtime >= floor:
                        files.append(p)
                except OSError:
                    continue      # vanished mid-scan: the exact reader skips it too
        except OSError:
            continue

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    files.sort(key=_mtime, reverse=True)
    found: list[dict] = []
    for path in files[:PRELOAD_MAX_FILES]:
        for rec in _tail_records(path, want, lambda r: keep(r)
                                 and (r.get("ts") or "") > newer_than):
            if rec.get("ts") and rec["ts"] > newer_than:
                found.append(rec)
        if len(found) >= want:
            break
    return found


def _rank_by_last_ts(files: list[Path]) -> list[Path]:
    """Files ordered by the timestamp of their LAST record, newest first.

    mtime is not a usable proxy: files written in the same second share it, and a
    backfill can append August records to today's file. Reading the last 64 KB of
    each file is cheap (30 files ≈ 2 MB) and says which files actually hold the
    newest records.
    """
    ranked: list[tuple[str, float, Path]] = []
    for p in files:
        last_ts = ""
        try:
            st = p.stat()
            with p.open("rb") as fh:
                fh.seek(max(0, st.st_size - 65536))
                for line in reversed(fh.read().decode("utf-8", "replace").splitlines()):
                    if '"ts"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and isinstance(rec.get("ts"), str):
                        last_ts = rec["ts"]
                        break
        except OSError:
            continue
        ranked.append((last_ts, st.st_mtime, p))
    ranked.sort(reverse=True)
    return [p for _, _, p in ranked]


def load_history(dirs: list[Path], keep, history: int) -> list[dict]:
    """Preload the last `history` records that pass the filter.

    With an index this is exact and cheap: the projection is ordered by ts, so
    "the last N by ts" is one indexed query, topped up from the file tails for
    whatever the last scan has not reached yet.

    Without one it falls back to reading the TAILS of the newest files (ranked by
    the timestamp of their last record, not by mtime). That fallback is BOUNDED —
    PRELOAD_MAX_FILES files, PRELOAD_TAIL_BYTES each — so it can return fewer than
    `history` records on a directory of gigabytes. `agenttrace index` is what makes
    the preload both exact and complete.

    The filter and the history size are passed in rather than re-derived from
    argparse, so preloaded history and live-tailed records go through the
    identical predicate. It used to call agenttrace.matches (strict `agent == value`
    equality) while the live loop used its own `in` membership test, so the two
    could disagree about the same record depending on which path it arrived by,
    and it read a module-global `args` that never existed.
    """
    if not history:
        recs = [r for _, _, r in iter_records(dirs) if keep(r)]
        recs.sort(key=lambda r: r.get("ts") or "")
        return recs
    files: list[Path] = []
    for d in dirs:
        try:
            files.extend(p for p in d.rglob("*.jsonl") if p.is_file())
        except OSError:
            continue
    # Newest first: a session's newest records live in the newest files.
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    collected: list[dict] = []
    # Read the newest files' tails and THEN pick the newest by ts. Stopping at the
    # first file that filled `history` assumed mtime identifies the newest file,
    # and files written in the same second share it — the preload then returned a
    # window tens of seconds old while a newer file sat right there.
    #
    # The budget is what keeps this honest: it reads until `history` is satisfied
    # or the bytes run out, so a normal request is exact (600 real records ≈ 18MB,
    # well inside the budget) and a pathological one degrades visibly instead of
    # silently. `agenttrace session`/`report` remain the exact tools.
    budget = PRELOAD_BUDGET_BYTES
    for path in files:
        if len(collected) >= history or budget <= 0:
            break
        recs, spent = _tail_records(path, history, keep,
                                    max_bytes=min(budget, PRELOAD_TAIL_BYTES), )
        collected.extend(recs)
        budget -= spent
    collected.sort(key=lambda r: r.get("ts") or "")
    return collected[-history:]


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


