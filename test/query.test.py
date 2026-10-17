#!/usr/bin/env python3
"""The query service layer over a fixture store.

The shapes here are what the HTTP API serializes verbatim, so pinning them
pins the API contract the dashboard is written against.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import query  # noqa: E402
from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


TS = "2026-10-17T10:%02d:00.000Z"


def v1(event: str, minute: int, **kw) -> dict:
    rec = {"v": 1, "ts": TS % minute, "agent": "hermes", "event": event,
           "session_id": "s1", "turn_id": "t1"}
    rec.update(kw)
    return rec


td = Path(tempfile.mkdtemp())
traces_dir = td / "traces"
traces_dir.mkdir()
(traces_dir / "a.jsonl").write_text("\n".join(json.dumps(r) for r in [
    # turn t1: healthy, 100/20 tokens
    v1("session_start", 0),
    v1("llm_request", 1, request_id="r1", model="m1"),
    v1("llm_response", 2, request_id="r1", model="m1",
       response={"usage": {"input_tokens": 100, "output_tokens": 20}}),
    # turn t2: one tool error
    v1("tool_call", 3, turn_id="t2",
       tool={"name": "bash", "call_id": "c1", "args": {}}),
    v1("tool_result", 4, turn_id="t2",
       tool={"name": "bash", "call_id": "c1", "status": "error"},
       error={"type": "exit", "message": "rc=1"}),
    # turn t3: never answered -> incomplete
    v1("llm_request", 5, turn_id="t3", request_id="r3", model="m1"),
]) + "\n", encoding="utf-8")

store = TraceStore(td / "q.db")
Indexer(store, [traces_dir]).scan_once()

ov = query.overview(store)
check("overview counts", ov["traces"] == 3 and ov["spans"] == 6,
      json.dumps({k: ov[k] for k in ("traces", "spans", "errors")}))
check("overview errors + unclosed", ov["errors"] == 1 and ov["unclosed"] == 1)
check("overview tokens", ov["tokens"]["input"] == 100)
check("overview per_agent", ov["per_agent"][0]["adapter_id"] == "hermes")
check("overview p50 present", ov["task_duration_ms"]["p50"] is not None)

all_t = query.list_traces(store, limit=50)
check("three traces listed", all_t["total"] == 3 and len(all_t["traces"]) == 3)
by_status = {t["status"] for t in all_t["traces"]}
check("all three statuses derived", by_status == {"ok", "error", "incomplete"},
      str(by_status))
check("traces ordered newest first",
      all_t["traces"][0]["start"] >= all_t["traces"][-1]["start"])
check("status filter works",
      query.list_traces(store, status="error")["total"] == 1
      and query.list_traces(store, status="incomplete")["total"] == 1
      and query.list_traces(store, status="ok")["total"] == 1)
check("agent filter works",
      query.list_traces(store, agent="codex")["total"] == 0)
check("limit/offset paginate",
      len(query.list_traces(store, limit=2)["traces"]) == 2
      and query.list_traces(store, limit=2, offset=2)["total"] == 3)

tid = next(t["trace_id"] for t in all_t["traces"] if t["status"] == "ok")
detail = query.get_trace(store, tid)
check("trace detail has spans + tree fields",
      detail is not None and len(detail["spans"]) == 2
      and all("parent_span_id" in s for s in detail["spans"]))
root = next(s for s in detail["spans"] if s["kind"] == "task")
check("task root identified and marked synthetic",
      root["synthetic"] is True and root["parent_span_id"] is None)
check("display status of closed span is its terminal state",
      root["status"] == "ok")

tid_inc = next(t["trace_id"] for t in all_t["traces"] if t["status"] == "incomplete")
detail_inc = query.get_trace(store, tid_inc)
open_span = next(s for s in detail_inc["spans"] if s["kind"] == "llm")
check("unclosed span displayed incomplete",
      open_span["ended"] is False and open_span["status"] == "incomplete")

check("missing trace returns None", query.get_trace(store, "nope") is None)

sessions = query.list_sessions(store)
check("one session with three traces",
      len(sessions["sessions"]) == 1 and sessions["sessions"][0]["trace_count"] == 3)

adapters = query.list_adapters(store)
hermes = next(a for a in adapters["adapters"] if a["adapter_id"] == "hermes")
check("adapter observed stats joined to registry entry",
      hermes["observed"]["spans"] == 6)
check("ingest file health exposed",
      len(adapters["ingest_files"]) == 1
      and adapters["ingest_files"][0]["corrupt_lines"] == 0)

store.close()

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("query layer OK")
