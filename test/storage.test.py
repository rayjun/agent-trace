#!/usr/bin/env python3
"""The M2 data pipeline: JSONL -> indexer -> SQLite store.

What is being protected: the store is a PROJECTION over JSONL truth, so the
properties that matter are exactly the recovery properties — idempotent
re-ingest, dedup, shrink-invalidation, corrupt-line quarantine, synthetic
task maintenance, and the incomplete-span rule (start without end shows as
incomplete, and self-heals when the end arrives).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


TS = "2026-10-17T10:00:%02d.000Z"


def v1(event: str, minute: int, **kw) -> dict:
    rec = {"v": 1, "ts": TS % minute, "agent": "hermes", "event": event,
           "session_id": "s1", "turn_id": "t1"}
    rec.update(kw)
    return rec


def write(path: Path, lines: list[dict | str], mode: str = "w") -> None:
    with open(path, mode, encoding="utf-8") as fh:
        for line in lines:
            fh.write((json.dumps(line) if isinstance(line, dict) else line) + "\n")


td = Path(tempfile.mkdtemp())
traces_dir = td / "traces"
traces_dir.mkdir()
f1 = traces_dir / "hermes-20261017.jsonl"
write(f1, [
    v1("session_start", 0),
    v1("llm_request", 1, request_id="r1", model="m1",
       request={"messages": [{"role": "user", "content": "hi"}]}),
    v1("tool_call", 2, tool={"name": "bash", "call_id": "c1", "args": {"cmd": "ls"}}),
    v1("tool_result", 3, tool={"name": "bash", "call_id": "c1", "status": "ok"},
       response={"content": "ok"}, duration_ms=50),
    v1("llm_response", 4, request_id="r1", model="m1",
       response={"content": "done",
                 "usage": {"input_tokens": 100, "output_tokens": 20,
                           "cache_read_tokens": 60}},
       duration_ms=1500),
    "{corrupt",
])

store = TraceStore(td / "test.db")
idx = Indexer(store, [traces_dir])
report = idx.scan_once()

check("scan ingested the file", report.files_ingested == 1)
check("corrupt line quarantined", report.corrupt_lines == 1)
check("events added", report.events_added == 5, str(report.events_added))

spans = store.query("SELECT * FROM spans ORDER BY start_ts")
by_id = {s["span_id"]: s for s in spans}
check("llm span paired and closed",
      by_id["llm:r1"]["ended"] == 1 and by_id["llm:r1"]["status"] == "ok")
check("llm tokens projected to columns",
      by_id["llm:r1"]["input_tokens"] == 100
      and by_id["llm:r1"]["output_tokens"] == 20
      and by_id["llm:r1"]["cache_read_tokens"] == 60)
check("llm duration from v1 attribute", by_id["llm:r1"]["duration_ms"] == 1500)
check("tool span paired", by_id["tool:c1"]["ended"] == 1)
check("synthetic task span derived",
      any(s["kind"] == "task" and s["synthetic"] == 1 for s in spans))
task = next(s for s in spans if s["kind"] == "task")
check("task bounds cover children",
      task["start_ts"] <= by_id["llm:r1"]["start_ts"]
      and task["end_ts"] >= by_id["llm:r1"]["end_ts"])
check("task status ok", task["status"] == "ok")
sess = store.one("SELECT * FROM sessions WHERE session_id='s1'")
check("session row exists", sess is not None and sess["ended"] == 0)

# idempotency: a second scan adds nothing
report2 = idx.scan_once()
check("second scan is a no-op",
      report2.events_added == 0 and report2.files_ingested == 0,
      str(report2.__dict__))
check("row counts stable after rescan",
      store.one("SELECT COUNT(*) AS n FROM events")["n"] == 5
      and store.one("SELECT COUNT(*) AS n FROM spans")["n"] == 3)

# tail append: a late span_end widens the task and closes the story
write(f1, [
    v1("llm_request", 5, request_id="r2", model="m1"),
    v1("llm_error", 6, request_id="r2",
       error={"type": "rate_limit", "message": "slow down"}),
    v1("session_end", 7),
], mode="a")
report3 = idx.scan_once()
check("tail ingest picks up new lines", report3.events_added == 3,
      str(report3.__dict__))
check("error span lands",
      store.one("SELECT status FROM spans WHERE span_id='llm:r2'")["status"] == "error")
task = store.one("SELECT * FROM spans WHERE span_id=trace_id AND kind='task'")
check("task flips to error after failing child",
      task["status"] == "error" and task["ended"] == 1)
check("session marked ended",
      store.one("SELECT ended FROM sessions WHERE session_id='s1'")["ended"] == 1)

# a span whose end never arrives is materialized incomplete
write(f1, [v1("llm_request", 8, request_id="r3", model="m1")], mode="a")
idx.scan_once()
r3 = store.one("SELECT * FROM spans WHERE span_id='llm:r3'")
check("unclosed span stored running, displayed incomplete",
      r3["ended"] == 0 and r3["status"] == "running")
# ... and self-heals when the end finally lands
write(f1, [v1("llm_response", 9, request_id="r3", response={"content": "late"})],
      mode="a")
idx.scan_once()
r3 = store.one("SELECT * FROM spans WHERE span_id='llm:r3'")
check("late span_end closes the span", r3["ended"] == 1 and r3["status"] == "ok")

# shrink: rotation invalidates the file and rebuilds cleanly
original_events = store.one("SELECT COUNT(*) AS n FROM events")["n"]
write(f1, [v1("llm_request", 1, request_id="fresh", model="m2",
              session_id="s2", turn_id="t9")])  # truncate + one line
report4 = idx.scan_once()
check("shrink invalidates and re-ingests",
      report4.files_invalidated == 1 and report4.events_added >= 1)
check("old events gone after shrink",
      store.one("SELECT COUNT(*) AS n FROM events")["n"] < original_events)
check("projections rebuilt after shrink",
      store.one("SELECT COUNT(*) AS n FROM spans WHERE span_id='llm:fresh'")["n"] == 1
      and store.one("SELECT COUNT(*) AS n FROM spans WHERE span_id='llm:r1'")["n"] == 0)

# A file REPLACED by a different, larger one is not an append. Size alone only
# catches truncation: the stored offset then pointed into the middle of the new
# file's records and the tail read ingested fragments of them (measured: 6 junk
# events from a regenerated trace file).
replace_dir = td / "replace"
replace_dir.mkdir()
target = replace_dir / "t.jsonl"
write(target, [v1("note", 0, note="original")])
r_store = TraceStore(td / "replace.db")
r_idx = Indexer(r_store, [replace_dir])
r_idx.scan_once()
check("the original file ingested one event",
      r_store.one("SELECT COUNT(*) AS n FROM events")["n"] == 1)
# a NEW file, larger, at the same path — a rewrite, a re-export, a re-import
replacement = replace_dir / "t2.jsonl"
write(replacement, [v1("note", 1, note=f"replacement {i}") for i in range(5)])
os.replace(replacement, target)
r_idx.scan_once()
notes = set()
for row in r_store.query("SELECT payload FROM events"):
    payload = json.loads(row["payload"])
    text = ((payload.get("event") or {}).get("attributes") or {}).get("text")
    if text:
        notes.add(text)
check("a replaced file's old records are gone, not merged",
      "original" not in notes, str(sorted(notes)))
check("...and every record of the new file is present exactly once",
      notes == {f"replacement {i}" for i in range(5)}, str(sorted(notes)))

# v2-native file: no conversion needed
f2 = traces_dir / "v2-native.jsonl"
write(f2, [
    {"v": 2, "event_id": "a" * 32, "type": "span_start",
     "ts": TS % 10, "observed_at": TS % 10, "adapter_id": "pi",
     "session_id": "s9", "trace_id": "native1", "span_id": "native1",
     "span": {"kind": "task", "name": "real task", "status": "running"}},
    {"v": 2, "event_id": "b" * 32, "type": "span_end",
     "ts": TS % 11, "observed_at": TS % 11, "adapter_id": "pi",
     "session_id": "s9", "trace_id": "native1", "span_id": "native1",
     "span": {"kind": "task", "name": "real task", "status": "ok"}},
])
idx.scan_once()
native = store.one("SELECT * FROM spans WHERE span_id='native1'")
check("v2-native task span ingested directly",
      native["ended"] == 1 and native["synthetic"] == 0)

# duplicate event_id with identical payload dedups; a rejected event does not
report5 = idx.scan_once()
check("v2 re-scan dedups by event_id", report5.events_added == 0)

store.close()

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("storage + indexer OK")
