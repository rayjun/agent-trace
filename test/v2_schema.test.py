#!/usr/bin/env python3
"""Pin core/schema/model.py against schema/events.schema.json.

Same contract-pinning job as test/schema.test.py does for v1: the JSON file
is the language-agnostic contract, the Python module is the ergonomic mirror,
and this test is the thing that fails when one side moves without the other.
It also exercises validate_event against the invariants consumers depend on
(materializer: terminal states; UI: metadata-mode means no bodies).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.schema import model  # noqa: E402

SCHEMA = json.loads(
    (ROOT / "schema" / "events.schema.json").read_text(encoding="utf-8"))

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


# --- constants mirror the JSON contract -----------------------------------

check("schema version pinned",
      SCHEMA["properties"]["v"]["const"] == model.SCHEMA_V2)
check("event types mirror schema",
      sorted(SCHEMA["properties"]["type"]["enum"]) == sorted(model.EVENT_TYPES))
check("span kinds mirror schema",
      sorted(SCHEMA["properties"]["span"]["properties"]["kind"]["enum"])
      == sorted(model.SPAN_KINDS))
check("span statuses mirror schema",
      sorted(s for s in SCHEMA["properties"]["span"]["properties"]["status"]["enum"]
             if s is not None)
      == sorted(model.SPAN_STATUSES))
check("terminal statuses are the span_end subset",
      set(model.TERMINAL_STATUSES) == {"ok", "error", "cancelled", "interrupted"}
      and set(model.TERMINAL_STATUSES) < set(model.SPAN_STATUSES))
check("adapter_id is an open string, not an enum",
      "enum" not in SCHEMA["properties"]["adapter_id"]
      and "pattern" in SCHEMA["properties"]["adapter_id"])


# --- helpers ---------------------------------------------------------------

def span_event(etype: str = "span_start", **over) -> dict:
    rec = {
        "v": 2,
        "event_id": "e" * 32,
        "type": etype,
        "ts": "2026-10-17T10:00:00.000Z",
        "observed_at": "2026-10-17T10:00:00.001Z",
        "adapter_id": "hermes",
        "session_id": "s1",
        "trace_id": "t1",
        "span_id": "sp1",
        "span": {"kind": "llm", "name": "gpt-x",
                 "status": "running" if etype == "span_start" else "ok"},
    }
    rec.update(over)
    return rec


# --- validate_event --------------------------------------------------------

check("valid span_start passes", model.validate_event(span_event()) == [])
check("valid span_end passes",
      model.validate_event(span_event("span_end")) == [])
check("wrong version rejected",
      model.validate_event({**span_event(), "v": 1}) != [])
check("bad ts rejected",
      model.validate_event({**span_event(), "ts": "yesterday"}) != [])
check("span marker without span_id rejected",
      model.validate_event({k: v for k, v in span_event().items() if k != "span_id"})
      != [])
check("span_start cannot open as ok",
      model.validate_event(span_event(span={"kind": "llm", "name": "m",
                                            "status": "ok"})) != [])
check("span_end must be terminal",
      model.validate_event(span_event("span_end",
                                      span={"kind": "llm", "name": "m",
                                            "status": "running"})) != [])
check("'incomplete' is never emitted",
      model.validate_event(span_event("span_end",
                                      span={"kind": "llm", "name": "m",
                                            "status": "incomplete"})) != [])
check("error payload needs failure status",
      model.validate_event(span_event(
          "span_end",
          span={"kind": "llm", "name": "m", "status": "ok",
                "error": {"type": "x", "message": "y"}})) != [])
check("self-parent rejected",
      model.validate_event(span_event(parent_span_id="sp1")) != [])
check("event type needs event.name",
      model.validate_event({**span_event("event"), "span": None,
                            "event": None}) != [])
check("valid discrete event passes",
      model.validate_event({**span_event("event"), "span": None, "span_id": None,
                            "trace_id": None,
                            "event": {"name": "session_start"}}) == [])
check("metadata capture must not carry bodies",
      model.validate_event(span_event(
          privacy={"capture": "metadata"},
          span={"kind": "llm", "name": "m", "status": "running",
                "input": {"messages": []}})) != [])
check("adapter_id must be lowercase slug",
      model.validate_event({**span_event(), "adapter_id": "Hermes CLI"}) != [])


# --- CanonicalEvent ergonomics ---------------------------------------------

d = model.CanonicalEvent(
    type="span_start", adapter_id="pi", session_id="s", trace_id="t",
    span_id="x", span={"kind": "tool", "name": "read", "status": "running"},
).to_dict()
check("to_dict fills ids and timestamps",
      len(d["event_id"]) == 32 and d["ts"].endswith("Z") and d["v"] == 2)
check("to_dict omits unknown optionals",
      "parent_span_id" not in d and "resource" not in d)
check("to_dict output validates", model.validate_event(d) == [])

a = model.derive_event_id("hermes", "s1", "r1", "llm_request")
b = model.derive_event_id("hermes", "s1", "r1", "llm_request")
c = model.derive_event_id("hermes", "s1", "r1", "llm_response")
check("derive_event_id deterministic", a == b and a != c and len(a) == 32)
check("derive_event_id splits concatenation",
      model.derive_event_id("ab", "c") != model.derive_event_id("a", "bc"))

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("v2 schema contract OK")
