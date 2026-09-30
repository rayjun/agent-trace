#!/usr/bin/env python3
"""agenttrace — a persistent, incrementally-maintained index over the traces.

Why this exists
---------------
The trace files are big and sparse: 839 MB on this machine in 9978 lines, i.e.
~84 KB per record, because every `llm_request` stores the whole conversation.
`json.loads` over all of it costs ~60 s of the ~68 s `agenttrace ls` used to
take, and `ls` materialised every match before slicing 40 rows off the end, so
it peaked at 1.3 GB of RSS to print 40 lines.

None of the record *bodies* are needed to answer `ls`, `sessions` or `stats`.
They need `ts`, `agent`, `event`, `session_id`, `model`, `cwd`, `duration_ms`
and `usage` — a few hundred bytes each. So those fields are extracted once and
cached beside the data; everything else is read on demand, by seeking to the
record's byte range and parsing only that.

Design rules that keep this honest
----------------------------------
* Append-only: only bytes past the last indexed newline are parsed. A file
  that shrank (rotation, truncation) invalidates its own index completely —
  stale offsets would silently read the wrong record.
* Fail OPEN. If the cache directory is unwritable, the state file is corrupt,
  or the index version changed, the caller falls back to parsing bodies. A
  cache that loses your data is a bug; a cache that merely misses is not.
* Bodies are always rendered from the file, never from the index, so a
  projection can never go stale and print a wrong preview. The index only
  routes (which file, which offset) and aggregates (counts, sums).
* `--contains` bypasses it: grepping content needs the bodies, which the index
  deliberately does not keep.

Usage lives in cli/agenttrace.py; this module knows nothing about argparse.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
import agenttrace_common as common  # noqa: E402

#: Bump whenever the projection below changes shape or meaning. A mismatch
#: makes every cached file be rebuilt from scratch rather than replayed into a
#: reader that expects fields which are not there.
INDEX_VERSION = 1

#: Top-level fields copied into the index. Deliberately short: this is the
#: routing/aggregation table, not a second copy of the traces.
#:   ts/agent/event/session_id/model  -> sorting, filtering, grouping
#:   request_id                       -> `show <request_id>` lookup
#:   cwd                              -> the `sessions` table
#:   duration_ms, usage               -> the `stats` table
PROJECTED = (
    "ts", "agent", "event", "session_id", "request_id",
    "model", "cwd", "duration_ms",
)


def index_enabled() -> bool:
    """AGENTTRACE_INDEX=0 turns the index off (full scan). Default: on."""
    return (os.environ.get("AGENTTRACE_INDEX") or "").strip().lower() not in (
        "0", "false", "no", "off")


def index_dir() -> Path:
    """Where cached state lives.

    Deliberately NOT under any trace directory and not `.agent-trace`: both of
    those are scanned for `*.jsonl` traces, and an index written there would be
    indexed as a trace, forever.
    """
    override = os.environ.get("AGENTTRACE_INDEX_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "agent-trace"


# Trace files are bucketed by the UTC day they were written
# (`hermes-20260930.jsonl`), which is what lets `entries` skip whole days it
# does not need. Anything without a day in its name counts as unknown — see
# `entries`.
_DAY_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")


def _day_key(source: Path) -> str:
    """`20260930` from the filename, or `""` when the name carries no day."""
    m = _DAY_RE.search(source.name)
    return m.group(1) if m else ""


def _ts_day(value) -> str:
    """`20260930` from a record timestamp, or `""` when unusable."""
    if isinstance(value, str) and len(value) >= 10:
        return value[:10].replace("-", "")
    return ""


def _state_path(source: Path) -> Path:
    key = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
    return index_dir() / f"{key}.json"


def _read_state(source: Path) -> dict | None:
    try:
        data = json.loads(_state_path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != INDEX_VERSION or data.get("source") != str(source):
        return None
    records = data.get("records")
    if not isinstance(records, list):
        return None
    return data


def _write_state(source: Path, indexed_bytes: int, records: list[dict]) -> None:
    """Write atomically: a reader must never see a half-written index, and two
    processes indexing the same file must not corrupt each other."""
    state = {"version": INDEX_VERSION, "source": str(source),
             "indexed_bytes": indexed_bytes, "records": records}
    dest = _state_path(source)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, dest)
    except OSError:
        # Unwritable cache is not an error: the next run just re-reads bodies.
        try:
            dest.with_suffix(f".tmp{os.getpid()}").unlink()
        except OSError:
            pass


def _invalidate(source: Path) -> None:
    try:
        _state_path(source).unlink()
    except OSError:
        pass


def _project(rec: dict, offset: int, length: int) -> dict:
    """One index row: where the record lives plus the fields readers aggregate.

    `o`/`l` are a byte range, not a line number — `load()` seeks straight to it
    instead of re-reading everything before it.
    """
    entry: dict = {"o": offset, "l": length}
    for key in PROJECTED:
        value = rec.get(key)
        if value is not None:
            entry[key] = value
    response = rec.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        # Nested exactly as it is in a real record, so `cmd_stats` reads a
        # projection and a body through the same `(rec.get("response") or {})`
        # expression instead of branching on which shape it was handed.
        entry["response"] = {"usage": response["usage"]}
    return entry


def _read_new(source: Path, start: int, records: list[dict]) -> tuple[bool, int]:
    """Parse complete lines from `start`; append projections to `records`.

    Returns (anything_new, next_start). A trailing partial line is left for
    the next run — advancing past it would index a truncated record.
    """
    try:
        with source.open("rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError:
        return False, start

    cut = data.rfind(b"\n")
    if cut < 0:
        return False, start
    chunk = data[: cut + 1]

    added = False
    pos = 0
    for raw in chunk.split(b"\n"):
        offset = start + pos
        pos += len(raw) + 1
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:  # includes UnicodeDecodeError
            continue
        if not isinstance(rec, dict) or rec.get("v") != common.SCHEMA_V:
            continue
        if "event" not in rec:
            continue
        records.append(_project(rec, offset, len(raw)))
        added = True
    return added, start + len(chunk)


def refresh(source: Path) -> list[dict]:
    """Index rows for one trace file, parsing only what was appended."""
    state = _read_state(source)
    records: list[dict] = []
    start = 0
    if state is not None:
        records = [r for r in state["records"] if isinstance(r, dict)]
        try:
            start = max(0, int(state.get("indexed_bytes", 0)))
        except (TypeError, ValueError):
            records, start = [], 0

    try:
        size = source.stat().st_size
    except OSError:
        return records

    if size < start:
        # Truncated or rotated: every cached offset now points into the wrong
        # record. Drop the lot rather than hand back wrong rows.
        _invalidate(source)
        records, start = [], 0

    if size > start:
        added, start = _read_new(source, start, records)
        if added:
            _write_state(source, start, records)
    return records


def entries(dirs: list[Path], *, accept=None, need: int = 0) -> list[tuple[str, dict]]:
    """`(source_path, index_row)` for every record under `dirs`.

    `accept` filters rows as they are produced (so the count below reflects
    what the caller will actually use); `need` is how many rows the caller
    wants. When both are given, whole DAYS can be abandoned once enough rows
    are in hand — which is what turns a first-ever `agenttrace ls` from a 63 s
    parse of 839 MB into a few seconds over today's files alone.

    The skip is provably safe rather than heuristic: sources are visited newest
    day first, and a day is abandoned only once every remaining source is
    strictly older than the OLDEST day among the rows already held. Records go
    into the file named for their write time, so a file dated D can only hold
    records with `ts` on day D or earlier — a source older than every held row
    cannot contribute anything that belongs in "the newest N". (Codex is the
    one adapter that rewrites `ts` from the turn start, so a turn crossing
    midnight lands in tomorrow's file with yesterday's stamp; that file is the
    NEWER one and is therefore visited, never skipped.)

    A source whose name carries no day forces `need` to zero: ordering it
    against the rest would be a guess.

    Rows come back even for files the index has never seen — `refresh` builds
    them on first contact. An empty list means "no traces", the same answer the
    body-scanning path gives.
    """
    sources: list[Path] = []
    for directory in dirs:
        try:
            sources += list(directory.rglob("*.jsonl"))
        except OSError:
            continue
    sources.sort(key=lambda p: _day_key(p), reverse=True)
    if any(not _day_key(p) for p in sources):
        need = 0                      # undated source: never guess the order

    out: list[tuple[str, dict]] = []
    for source in sources:
        if need and len(out) >= need:
            held = min((_ts_day(row.get("ts")) for _, row in out
                        if _ts_day(row.get("ts"))), default="")
            day = _day_key(source)
            if held and day and day < held:
                break                 # every remaining source is older still
        for row in refresh(source):
            if accept is None or accept(row):
                out.append((str(source), row))
    return out


def load(source: Path | str, row: dict) -> dict | None:
    """Read the one record a row points at.

    Returns None if the bytes moved under us — in which case the index for that
    file is dropped, so the next run rebuilds instead of failing the same way.
    """
    try:
        offset, length = int(row["o"]), int(row["l"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        with Path(source).open("rb") as fh:
            fh.seek(offset)
            raw = fh.read(length)
    except OSError:
        return None
    if len(raw) != length:
        _invalidate(Path(source))
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        _invalidate(Path(source))
        return None
    return rec if isinstance(rec, dict) else None


def load_many(rows: list[tuple[str, dict]]) -> list[dict]:
    """Full records for rows, in the order given; unreachable rows are dropped."""
    out = []
    for source, row in rows:
        rec = load(source, row)
        if rec is not None:
            out.append(rec)
    return out
