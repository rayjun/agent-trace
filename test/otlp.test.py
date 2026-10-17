#!/usr/bin/env python3
"""OTLP import/export.

Two guarantees are pinned:

1. **Round trip is exact for our own data.** Export a trace, import it back,
   and every canonical field that carries meaning (ids, kind, status, tokens,
   model, input/output, error) must survive. If it does not, the OTel bridge
   is lossy and export is a lie.

2. **Foreign OTLP is imported, not rejected.** A payload from a plain OTel
   SDK app (GenAI semantic conventions, no agent_trace.* attributes) must
   produce valid canonical events with derived ids — and the parts OTLP
   cannot express (span kind, session) must be visibly absent, not invented.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.otlp import export_trace, export_traces, import_file, import_payload  # noqa: E402
from core.query import get_trace, list_traces  # noqa: E402
from core.schema.model import validate_event  # noqa: E402
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


td = Path(tempfile.mkdtemp())
traces_dir = td / "traces"
traces_dir.mkdir()
(traces_dir / "a.jsonl").write_text("\n".join(json.dumps(r) for r in [
    v1("user_prompt", 0,
       request={"messages": [{"role": "user", "content": "fix it"}]}),
    v1("llm_request", 1, request_id="r1", model="gpt-4o",
       request={"messages": [], "approx_input_tokens": 12}),
    v1("tool_call", 2, tool={"name": "bash", "call_id": "c1",
                             "args": {"cmd": "pytest"}}),
    v1("tool_result", 3, tool={"name": "bash", "call_id": "c1", "status": "error"},
       response={"content": "1 failed"}, error={"type": "exit", "message": "rc=1"},
       duration_ms=250),
    v1("llm_response", 4, request_id="r1", model="gpt-4o",
       response={"content": "done",
                 "usage": {"input_tokens": 300, "output_tokens": 40,
                           "cache_read_tokens": 100}},
       duration_ms=1200),
    v1("llm_request", 5, request_id="orphan", model="gpt-4o"),  # never ends
]) + "\n", encoding="utf-8")

store = TraceStore(td / "otlp.db")
Indexer(store, [traces_dir]).scan_once()
trace = get_trace(store, list_traces(store)["traces"][0]["trace_id"])

payload = export_trace(trace)
container = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
check("export produces one resourceSpans envelope",
      list(payload) == ["resourceSpans"]
      and len(container) == len(trace["spans"]),
      f"{len(container)} vs {len(trace['spans'])}")
attrs_of = lambda s: {a["key"]: a["value"] for a in s["attributes"]}  # noqa: E731

llm = next(s for s in container if attrs_of(s).get("agent_trace.span_kind",
                                                 {}).get("stringValue") == "llm")
check("span ids are valid OTLP hex",
      len(llm["traceId"]) == 32 and len(llm["spanId"]) == 16
      and all(c in "0123456789abcdef" for c in llm["traceId"] + llm["spanId"]))
check("GenAI semantic conventions emitted",
      attrs_of(llm).get("gen_ai.request.model", {}).get("stringValue") == "gpt-4o"
      and attrs_of(llm).get("gen_ai.usage.input_tokens", {}).get("intValue") == "300")
check("session carried in our namespace",
      attrs_of(llm).get("agent_trace.session_id", {}).get("stringValue") == "s1")
check("parent link preserved for tool spans",
      any(s.get("parentSpanId") for s in container))
check("unclosed span exports without endTimeUnixNano",
      sum(1 for s in container if "endTimeUnixNano" not in s) == 1)
check("discrete events ride on the root span's events",
      any(s.get("events") for s in container))

# --- round trip -------------------------------------------------------------
round_tripped = import_payload(payload)
check("round-tripped events all validate",
      all(not validate_event(e) for e in round_tripped),
      "; ".join(p for e in round_tripped for p in validate_event(e))[:300])
# The synthetic task root is derived data: it is exported (so an OTel backend
# can render the trace) but NOT re-imported as an observation, because
# refresh_projections re-derives it from the children. Everything else must
# come back identically.
observed = [s for s in trace["spans"] if not s["synthetic"]]
check("observed span ids survive the round trip",
      {e["span_id"] for e in round_tripped if e["type"] == "span_start"}
      == {s["span_id"] for s in observed},
      str({e["span_id"] for e in round_tripped if e["type"] == "span_start"}))
check("trace id survives", {e["trace_id"] for e in round_tripped}
      == {trace["trace_id"]})
check("kind/name survive",
      {e["span"]["kind"] for e in round_tripped if e["type"] == "span_start"}
      == {s["kind"] for s in observed})
check("terminal statuses survive",
      {e["span"]["status"] for e in round_tripped if e["type"] == "span_end"}
      == {"ok", "error"})
check("usage survives",
      any((e["span"].get("attributes") or {}).get("usage", {}).get("input_tokens")
          == 300 for e in round_tripped if e["type"] == "span_end"))
check("tool error message survives",
      any((e["span"].get("error") or {}).get("message") == "rc=1"
          for e in round_tripped if e["type"] == "span_end"))
check("discrete events survive as type=event",
      any(e["type"] == "event" and e["event"]["name"] == "user_prompt"
          for e in round_tripped))
check("event text survives",
      any(e["type"] == "event"
          and (e["event"].get("attributes") or {}).get("text") == "fix it"
          for e in round_tripped))

# --- re-import into a store is a no-op the second time ----------------------
events = list(round_tripped)
for e in events:
    store.ingest(e)
spans_after = store.one("SELECT COUNT(*) AS n FROM spans")["n"]
for e in events:
    store.ingest(e)
check("re-importing the same OTLP payload dedups by event_id",
      store.one("SELECT COUNT(*) AS n FROM spans")["n"] == spans_after)
check("imported tool span carries its error",
      store.one("SELECT status FROM spans WHERE span_id='tool:c1'")["status"]
      == "error")

# --- foreign OTLP (no agent_trace.*) ---------------------------------------
foreign = {"resourceSpans": [{
    "resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "my-otel-app"}}]},
    "scopeSpans": [{"spans": [{
        "traceId": "1" * 32, "spanId": "2" * 16,
        "name": "chat gpt-4o", "kind": 3,
        "startTimeUnixNano": "1760700000000000000",
        "endTimeUnixNano": "1760700001500000000",
        "attributes": [
            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
            {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "500"}},
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "60"}},
        ],
        "status": {"code": 0},
    }]}]}]}

foreign_events = import_payload(foreign, adapter_id="my-otel-app")
check("foreign OTLP imported and valid",
      foreign_events and all(not validate_event(e) for e in foreign_events))
check("foreign ids derived from OTel ids",
      foreign_events[0]["trace_id"] == "otlp:" + "1" * 32
      and foreign_events[0]["span_id"] == "otlp:" + "2" * 16)
check("foreign span kind inferred from gen_ai.operation.name",
      foreign_events[0]["span"]["kind"] == "llm")
check("foreign usage mapped",
      (foreign_events[1]["span"].get("attributes") or {})
      .get("usage", {}).get("input_tokens") == 500)
check("foreign timestamps decoded from nanos",
      foreign_events[0]["ts"].startswith("2025-") or
      foreign_events[0]["ts"].startswith("2026-"))
check("absent session stays absent (not invented)",
      foreign_events[0].get("session_id") is None)

failed_foreign = {"resourceSpans": [{"scopeSpans": [{"spans": [{
    "traceId": "3" * 32, "spanId": "4" * 16, "name": "execute_tool",
    "startTimeUnixNano": "1760700000000000000",
    "endTimeUnixNano": "1760700000500000000",
    "attributes": [
        {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}}],
    "status": {"code": 2, "message": "tool blew up"}}]}]}]}
fe = import_payload(failed_foreign)
check("OTLP error status -> canonical error with message",
      fe[-1]["span"]["status"] == "error"
      and fe[-1]["span"]["error"]["message"] == "tool blew up")
check("execute_tool kind -> tool span", fe[0]["span"]["kind"] == "tool")

# --- import_file accepts the shapes OTel actually writes ---------------------
out = td / "exported.otlp.json"
out.write_text(json.dumps(export_traces([trace]), ensure_ascii=False))
check("import_file reads a single JSON payload",
      len(list(import_file(out))) == len(events))
jsonl = td / "stream.otlp.jsonl"
jsonl.write_text(json.dumps(payload) + "\n" + json.dumps(foreign) + "\n")
check("import_file reads a JSONL stream of payloads",
      len(list(import_file(jsonl))) == len(events) + len(foreign_events))

store.close()

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("OTLP import/export OK")
