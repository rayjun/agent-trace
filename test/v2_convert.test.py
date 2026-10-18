#!/usr/bin/env python3
"""The v1 -> v2 converter, tested against realistic v1 records.

What is being protected: ~GB of existing v1 JSONL on users' machines is the
installed base; the converter is the only bridge to the v2 platform, so its
guarantees (deterministic ids, correct pairing, honest synthetic task spans,
nothing invented that v1 never knew) are pinned here.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.convert.v1_to_v2 import convert_file, convert_record  # noqa: E402
from core.schema.model import validate_event  # noqa: E402

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
           "session_id": "sess-1", "turn_id": "turn-1"}
    rec.update(kw)
    return rec


RECORDS = [
    v1("session_start", 0, model="m1"),
    v1("user_prompt", 1, request={"messages": [{"role": "user", "content": "fix it"}]}),
    v1("llm_request", 2, request_id="r1", model="m1", provider="p",
       request={"messages": [], "approx_input_tokens": 10}),
    v1("tool_call", 3, tool={"name": "bash", "call_id": "c1", "args": {"cmd": "pytest"}}),
    v1("tool_result", 4, tool={"name": "bash", "call_id": "c1", "status": "ok"},
       response={"content": "1 passed"}, duration_ms=900),
    v1("llm_response", 5, request_id="r1", model="m1",
       response={"content": "done", "usage": {"input_tokens": 10, "output_tokens": 5}},
       duration_ms=1200),
    v1("llm_request", 6, request_id="r2", model="m1"),  # never answered: crash
    v1("session_end", 7, note="interrupted=True"),
]

with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "traces.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS) + "\n"
                    + "{corrupt line\n", encoding="utf-8")
    events = list(convert_file(path))

# every emitted event must itself satisfy the v2 contract
violations = [p for e in events for p in validate_event(e)]
check("all converted events validate", violations == [], "; ".join(violations[:3]))

by_type = {}
for e in events:
    by_type.setdefault(e["type"], []).append(e)

spans = [e for e in events if e["type"] == "span_start"]
ends = [e for e in events if e["type"] == "span_end"]
check("llm request/response pair became one span's two halves",
      any(s["span_id"] == "llm:r1" for s in spans)
      and any(e["span_id"] == "llm:r1" and e["span"]["status"] == "ok" for e in ends))
check("tool pair linked by call_id",
      any(s["span_id"] == "tool:c1" and s["span"]["name"] == "bash" for s in spans)
      and any(e["span_id"] == "tool:c1" and e["span"]["status"] == "ok" for e in ends))
check("tool result body preserved",
      any(e["span_id"] == "tool:c1"
          and (e["span"].get("output") or {}).get("result") == "1 passed"
          for e in ends))
check("usage moved to span attributes",
      any(e["span_id"] == "llm:r1"
          and (e["span"].get("attributes") or {}).get("usage", {}).get("output_tokens") == 5
          for e in ends))

tasks = [e for e in spans if e["span"]["kind"] == "task"]
check("one synthetic task span per (session, turn)",
      len([t for t in tasks if t["trace_id"] == t["span_id"]]) == 1)
check("task span parents everything in the turn",
      all(s.get("parent_span_id") == tasks[0]["span_id"]
          for s in spans if s["span"]["kind"] != "task"))
check("task span marked synthetic",
      tasks[0]["span"]["attributes"].get("synthetic") is True)
task_end = next(e for e in ends if e["span"]["kind"] == "task")
check("unanswered llm_request leaves task ok (span-level truth)",
      task_end["span"]["status"] == "ok")
check("orphan request survives as an unclosed span_start",
      any(s["span_id"].startswith("llm:r2") for s in spans)
      and not any(e["span_id"] == "llm:r2" for e in ends))

discrete = [e for e in events if e["type"] == "event"]
sess = [e for e in discrete if e["event"]["name"] == "session_start"]
check("session events carry no trace_id",
      sess and sess[0]["trace_id"] is None)
prompt = next(e for e in discrete if e["event"]["name"] == "user_prompt")
check("user_prompt text preserved",
      prompt["event"]["attributes"]["text"] == "fix it"
      and prompt["trace_id"] == tasks[0]["trace_id"])

# determinism: converting twice yields identical event ids
with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "traces.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS) + "\n", encoding="utf-8")
    again = list(convert_file(path))
ids1 = [e["event_id"] for e in events]
ids2 = [e["event_id"] for e in again]
check("conversion is deterministic", ids1 == ids2)
check("event ids unique per stream",
      len(set(ids1)) == len(ids1), f"{len(ids1) - len(set(ids1))} dupes")

# corrupt line was skipped, not fatal
check("corrupt line quarantined",
      len(events) == len(RECORDS) + 2)  # + synthetic task start/end

check("non-v1 record returns None", convert_record({"v": 99}) is None)

# ---- convert_stream: a trace split across files gets ONE task span ---------
# An agent's daily files split a session across days, and the task span is
# derived from a trace's children. Synthesizing per FILE emitted one task span
# per file for the same trace — same span id, different bounds — which the
# conformance kit rejected as a duplicate.
import tempfile as _tempfile  # noqa: E402

from adapter_sdk.testing import check_stream  # noqa: E402
from core.convert.v1_to_v2 import convert_file, convert_stream  # noqa: E402

_split = Path(_tempfile.mkdtemp())
day1, day2 = _split / "day1.jsonl", _split / "day2.jsonl"
day1.write_text(json.dumps(v1("llm_request", 1, request_id="a", model="m")) + "\n",
                encoding="utf-8")
day2.write_text(json.dumps(v1("llm_response", 2, request_id="a", model="m",
                              response={"content": "x"})) + "\n",
                encoding="utf-8")

per_file = list(convert_file(day1)) + list(convert_file(day2))
tasks_per_file = [e for e in per_file
                  if e["type"] == "span_start" and e["span"]["kind"] == "task"]
check("per-file conversion emits one task span PER FILE (the bug)",
      len(tasks_per_file) == 2
      and tasks_per_file[0]["span_id"] == tasks_per_file[1]["span_id"],
      str([t["span_id"][:12] for t in tasks_per_file]))

stream = list(convert_stream([day1, day2]))
tasks_stream = [e for e in stream
                if e["type"] == "span_start" and e["span"]["kind"] == "task"]
check("convert_stream synthesizes the task span ONCE for the whole trace",
      len(tasks_stream) == 1, str(len(tasks_stream)))
check("...and the whole split trace still validates",
      all(not validate_event(e) for e in stream))

# the stream's own conformance, which is what caught it
_stream_file = _split / "stream.jsonl"
_stream_file.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in per_file)
                        + "\n", encoding="utf-8")
check("the per-file stream FAILS conformance (duplicate span id)",
      not check_stream(_stream_file).ok)
_stream_file.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in stream)
                        + "\n", encoding="utf-8")
check("the cross-file stream PASSES conformance",
      check_stream(_stream_file, adapter_id="hermes").ok)

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("v1 -> v2 conversion OK")
