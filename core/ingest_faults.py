"""Read side of the ingest-fault log — the events that never made it to disk.

The WRITER lives in `common/agenttrace_common.py`, not here, because the v1
capture path is byte-copied into host processes (the Hermes plugin ships as
one file plus this module) and cannot import `core/`. One writer, one format;
this module is the reader the platform surfaces use (doctor, and anything
else that wants to say "capture is losing data").

Why the log exists at all: before it, a full disk or an unwritable trace dir
made `emit_record` return None, the adapter dropped the event, and nothing
anywhere said so. The dashboard then looked the same as an idle agent. See
docs/decisions.md D27.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
import agenttrace_common as common  # noqa: E402


def path() -> Path:
    """The log's location (~/.agenttrace/ingest-faults.jsonl)."""
    return common.faults_path()


def enabled() -> bool:
    return common.faults_enabled()


def read(limit: int = 2000) -> list[dict[str, Any]]:
    """Fault records, oldest first. Tolerant of a half-written last line.

    The writer is a fail-open path that can be killed mid-append (SIGKILL on
    a disk that is already misbehaving), so a truncated tail is expected
    rather than exceptional: skip it, keep everything before it.
    """
    dest = path()
    try:
        raw = dest.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw[-limit:] if limit > 0 else raw:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("ts"):
            out.append(rec)
    return out


def summarize(limit: int = 5000) -> list[dict[str, Any]]:
    """Group by (adapter, kind), newest group first.

    Counts are the number of LOG LINES, not dropped events: the writer
    throttles a burst to one line per window, so the number of lost events is
    at least this and unknowable above it. Said plainly here so no surface
    reports a precise-looking number it cannot know.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in read(limit=limit):
        key = (str(rec.get("adapter_id") or "?"), str(rec.get("kind") or "?"))
        entry = groups.get(key)
        if entry is None:
            groups[key] = {
                "adapter_id": key[0],
                "kind": key[1],
                "lines": 1,
                "first_ts": rec.get("ts"),
                "last_ts": rec.get("ts"),
                "last_error": rec.get("error") or "",
                "path": rec.get("path") or "",
            }
        else:
            entry["lines"] += 1
            entry["last_ts"] = rec.get("ts") or entry["last_ts"]
            entry["last_error"] = rec.get("error") or entry["last_error"]
            entry["path"] = rec.get("path") or entry["path"]
            entry["first_ts"] = entry["first_ts"] or rec.get("ts")
    return sorted(groups.values(), key=lambda g: str(g["last_ts"]), reverse=True)


def newest() -> dict[str, Any] | None:
    recs = read(limit=1)
    return recs[0] if recs else None
