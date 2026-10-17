#!/usr/bin/env python3
"""The conformance kit, tested against good and broken streams.

A checker that only ever sees good input is unverified itself: each
violation class the kit promises to catch gets its own broken fixture here,
plus one known-good stream that must pass clean (built through the converter,
so the two halves of M1 verify each other).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapter_sdk.testing import check_stream  # noqa: E402
from core.convert.v1_to_v2 import convert_file  # noqa: E402

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


TS = "2026-10-17T10:00:%02d.000Z"


def ev(etype: str, minute: int, **kw) -> dict:
    rec = {"v": 2, "event_id": f"id-{etype}-{minute}-{'x' * 8}",
           "type": etype, "ts": TS % minute, "observed_at": TS % minute,
           "adapter_id": "hermes", "session_id": "s1"}
    rec.update(kw)
    return rec


def span_start(span_id: str, kind: str = "llm", parent: str | None = None,
               minute: int = 1, eid: str | None = None) -> dict:
    return ev("span_start", minute, event_id=eid or f"start-{span_id}-{'x' * 8}",
              trace_id="t1", span_id=span_id, parent_span_id=parent,
              span={"kind": kind, "name": "n", "status": "running"})


def span_end(span_id: str, status: str = "ok", minute: int = 2,
             error: dict | None = None, kind: str = "llm",
             name: str = "n") -> dict:
    span = {"kind": kind, "name": name, "status": status}
    if error:
        span["error"] = error
    return ev("span_end", minute, event_id=f"end-{span_id}-{'y' * 8}",
              trace_id="t1", span_id=span_id, span=span)


def write(lines: list[dict | str]) -> Path:
    td = tempfile.mkdtemp()
    path = Path(td) / "stream.jsonl"
    path.write_text("\n".join(
        json.dumps(l) if isinstance(l, dict) else l for l in lines) + "\n",
        encoding="utf-8")
    return path


# --- known-good stream: a real converter run must pass its own kit ---------

v1_records = [
    {"v": 1, "ts": TS % 0, "agent": "hermes", "event": "llm_request",
     "session_id": "s", "turn_id": "t", "request_id": "r1"},
    {"v": 1, "ts": TS % 1, "agent": "hermes", "event": "llm_response",
     "session_id": "s", "turn_id": "t", "request_id": "r1",
     "response": {"content": "ok"}},
]
good_path = write([])
with open(good_path, "w", encoding="utf-8") as fh:
    src = Path(good_path.parent) / "v1.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in v1_records) + "\n",
                   encoding="utf-8")
    for e in convert_file(src):
        fh.write(json.dumps(e) + "\n")
report = check_stream(good_path, adapter_id="hermes")
check("converter output passes the kit", report.ok, report.render())
check("stats count span kinds", report.stats.get("span.llm") == 1
      and report.stats.get("span.task") == 1)

# --- each violation class ---------------------------------------------------

report = check_stream(write(["{not json"]))
check("corrupt line is a violation", not report.ok and report.corrupt_lines == 1)

report = check_stream(write([span_end("ghost")]))
check("span_end without start is a violation",
      any("without any span_start" in v for v in report.violations))

report = check_stream(write([span_start("a", parent="missing-parent")]))
check("dangling parent_span_id is a violation",
      any("parent_span_id" in v for v in report.violations))

dupe_a = span_start("a")
dupe_b = {**dupe_a, "span": {"kind": "tool", "name": "other", "status": "running"}}
report = check_stream(write([dupe_a, dupe_b]))
check("same event_id, different payload is a violation",
      any("different" in v for v in report.violations))

report = check_stream(write([span_start("a"), span_start("a", eid="second-id-xx")]))
check("duplicate span_start for one span_id is a violation",
      any("duplicate span_start" in v for v in report.violations))

report = check_stream(write([
    span_start("a"), span_end("a", error={"type": "t", "message": "m"})]))
check("error payload with status=ok is a violation",
      any("error payload" in v for v in report.violations))

report = check_stream(write([span_start("a")]))
check("unclosed span is a warning, not a failure",
      report.ok and any("never closed" in w for w in report.warnings))

report = check_stream(write([span_end("a", minute=1), span_start("a", minute=2)]))
check("end-before-start reconciles to a warning",
      report.ok and any("precedes" in w for w in report.warnings))

report = check_stream(write([
    span_start("t", kind="task"),
    span_start("c", parent="t"),
    span_end("c"),
    span_end("t", minute=3, kind="task"),
]))
check("well-formed tree passes clean", report.ok, report.render())

report = check_stream(write([span_start("c", kind="llm"), span_end("c")]),
                      adapter_id="codex")
check("codex stream with llm spans passes (partial, not unsupported)",
      report.ok, report.render())

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("conformance kit OK")
