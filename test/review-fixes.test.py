#!/usr/bin/env python3
"""Regression tests for the findings of the two M4 review agents.

Every case here is a counterexample one of the reviewers actually produced.
They are kept as tests rather than fixed-and-forgotten because each one was a
silent lie rather than a crash: a round trip that dropped a field, a cost that
was confidently wrong, a checker that said PASS, a capability that claimed
more than the code delivered.

Grouped by the original claim they falsified.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.otlp import export_trace, import_payload  # noqa: E402
from adapter_sdk.capabilities import Capability, SupportLevel  # noqa: E402
from adapter_sdk.testing import check_stream  # noqa: E402
from core.convert.v1_to_v2 import convert_record  # noqa: E402
from core.pricing import cost_usd  # noqa: E402
from core.query import get_trace, list_traces, overview  # noqa: E402
from core.schema.model import validate_event  # noqa: E402
from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402
from evaluation import datasets as ds  # noqa: E402
from evaluation import experiments as ex  # noqa: E402

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


def store_with(records: list[dict], name: str = "t") -> TraceStore:
    td = Path(tempfile.mkdtemp())
    d = td / "traces"
    d.mkdir()
    (d / f"{name}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    store = TraceStore(td / "s.db")
    Indexer(store, [d]).scan_once()
    return store


# ==========================================================================
# CLAIM 1 — OTLP round trip
# ==========================================================================

# 1a. export must not CRASH on an unclosed, attribute-less span (a crashed run)
crash_store = store_with([v1("llm_request", 0, request_id="orphan")])
crash_trace = get_trace(crash_store, list_traces(crash_store)["traces"][0]["trace_id"])
try:
    payload = export_trace(crash_trace)
    check("export survives a span with attributes=None", len(payload) == 1)
except Exception as exc:  # noqa: BLE001
    check("export survives a span with attributes=None", False, repr(exc))

# 1b. cancelled / interrupted must not come back as 'error'
for status in ("cancelled", "interrupted"):
    st = store_with([v1("llm_request", 0, request_id="r"),
                     v1("llm_error", 1, request_id="r",
                        error={"type": "abort", "message": "stopped"})])
    t = get_trace(st, list_traces(st)["traces"][0]["trace_id"])
    for span in t["spans"]:
        if span["span_id"] == "llm:r":
            span["status"] = status
    back = import_payload(export_trace(t))
    end = next(e for e in back if e["type"] == "span_end"
               and e["span_id"] == "llm:r")
    check(f"status {status} survives the round trip",
          end["span"]["status"] == status, end["span"]["status"])
    st.close()

# 1c. an event's owning span must survive
ev_store = store_with([
    v1("llm_request", 0, request_id="r1", model="m"),
    v1("llm_response", 1, request_id="r1", response={"content": "x"}),
    v1("tool_call", 2, tool={"name": "bash", "call_id": "c1", "args": {}}),
    v1("tool_result", 3, tool={"name": "bash", "call_id": "c1", "status": "ok"}),
])
ev_trace = get_trace(ev_store, list_traces(ev_store)["traces"][0]["trace_id"])
ev_trace["events"] = [{"ts": TS % 3, "name": "user_prompt",
                       "attributes": {"text": "hi"}, "span_id": "tool:c1"}]
back = import_payload(export_trace(ev_trace))
imported_event = next(e for e in back if e["type"] == "event")
check("event-to-span ownership survives the round trip",
      imported_event["span_id"] == "tool:c1", imported_event["span_id"])
ev_store.close()

# 1d. privacy / source / agent_version survive
pv_store = store_with([v1("llm_request", 0, request_id="r1", model="m")])
pv_trace = get_trace(pv_store, list_traces(pv_store)["traces"][0]["trace_id"])
for span in pv_trace["spans"]:
    span["privacy"] = {"capture": "metadata", "redacted": True}
    span["source"] = {"file": "/tmp/x.jsonl", "line": 3}
    span["agent_version"] = "1.2.3"
pv_back = import_payload(export_trace(pv_trace))
check("privacy survives the round trip",
      any(e.get("privacy", {}).get("capture") == "metadata" for e in pv_back))
check("agent_version survives the round trip",
      any(e.get("agent_version") == "1.2.3" for e in pv_back))
check("source survives the round trip",
      any((e.get("source") or {}).get("file") == "/tmp/x.jsonl" for e in pv_back))
check("exported payload still validates as canonical on import",
      all(not validate_event(e) for e in pv_back))
pv_store.close()
crash_store.close()

# ==========================================================================
# CLAIM 3 — the conformance kit must not report PASS on violations
# ==========================================================================

def stream(lines: list[dict]) -> Path:
    td = tempfile.mkdtemp()
    p = Path(td) / "s.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return p


def ev(etype: str, **kw) -> dict:
    tag = str(kw.get("span_id", "x")) + etype
    base = {"v": 2, "event_id": ("e" * 8 + tag)[:32], "type": etype,
            "ts": TS % 1, "observed_at": TS % 1, "adapter_id": "hermes",
            "session_id": "s", "trace_id": "t1"}
    base.update(kw)
    return base


# 3a. the two halves must describe the same span
impostor = stream([
    ev("span_start", span_id="x", span={"kind": "llm", "name": "real",
                                        "status": "running"}),
    ev("span_end", span_id="x", span={"kind": "tool", "name": "IMPOSTOR",
                                      "status": "ok"}),
])
r = check_stream(impostor)
check("half-vs-half kind mismatch is a violation",
      not r.ok and any("starts as kind" in v for v in r.violations), r.render())

# 3b. a parent in a different trace
cross = stream([
    ev("span_start", span_id="p", trace_id="t1",
       span={"kind": "task", "name": "p", "status": "running"}),
    ev("span_start", span_id="c", trace_id="t2", parent_span_id="p",
       span={"kind": "llm", "name": "c", "status": "running"}),
])
r = check_stream(cross)
# A parent is identified within its trace, so a child in t2 pointing at a
# parent that only exists in t1 has no resolvable parent at all.
check("cross-trace parent is a violation",
      not r.ok and any("parent_span_id p" in v for v in r.violations), r.render())

# 3c. metadata privacy must cover event attributes, not just span.input
leak_event = stream([
    ev("event", privacy={"capture": "metadata"},
       event={"name": "user_prompt", "attributes": {"text": "secret prompt"}}),
])
r = check_stream(leak_event)
check("metadata-privacy leak in event.attributes is caught",
      not r.ok and any("attributes carries text" in v for v in r.violations),
      r.render())

leak_span_attrs = stream([
    ev("span_start", span_id="x", privacy={"capture": "metadata"},
       span={"kind": "llm", "name": "n", "status": "running",
             "attributes": {"messages": [{"content": "secret"}]}}),
])
r = check_stream(leak_span_attrs)
check("metadata-privacy leak in span.attributes is caught",
      not r.ok and any("attributes carries" in v for v in r.violations),
      r.render())

# ... and the legitimate cases still pass
r = check_stream(stream([
    ev("span_start", span_id="x", privacy={"capture": "metadata", "redacted": True},
       span={"kind": "llm", "name": "n", "status": "running"}),
    ev("span_end", span_id="x", privacy={"capture": "metadata", "redacted": True},
       span={"kind": "llm", "name": "n", "status": "ok"}),
]))
check("metadata privacy with no bodies still passes", r.ok, r.render())

# ==========================================================================
# CLAIM 4 — capability declarations must match the code
# ==========================================================================

from adapter_sdk.registry import get as registry_get  # noqa: E402

check("codex token_usage is no longer claimed as fully supported",
      registry_get("codex").capabilities.level(Capability.TOKEN_USAGE)
      == SupportLevel.PARTIAL)
check("codex session_lifecycle is no longer claimed as supported",
      registry_get("codex").capabilities.level(Capability.SESSION_LIFECYCLE)
      == SupportLevel.PARTIAL)
check("pi errors is no longer claimed as supported",
      registry_get("pi").capabilities.level(Capability.ERRORS)
      == SupportLevel.PARTIAL)
check("langgraph subagents is partial (chains, not agent spans)",
      registry_get("langgraph").capabilities.level(Capability.SUBAGENTS)
      == SupportLevel.PARTIAL)

# ...and the codex token claim is now actually delivered
codex_rollout = Path(tempfile.mkdtemp()) / "rollout.jsonl"
codex_rollout.write_text("\n".join(json.dumps(l) for l in [
    {"type": "session_meta", "payload": {"id": "codex-s", "cwd": "/tmp",
                                         "model_provider": "openai"}},
    {"type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-4o"}},
    {"type": "event_msg", "payload": {"type": "token_count",
                                      "info": {"last_token_usage": {
                                          "input_tokens": 1200,
                                          "output_tokens": 340,
                                          "cached_input_tokens": 900}}}},
]) + "\n", encoding="utf-8")
import subprocess  # noqa: E402
codex_out = Path(tempfile.mkdtemp())
subprocess.run([sys.executable, str(ROOT / "codex" / "codex_import.py"),
                str(codex_rollout)], capture_output=True, text=True,
               env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
                    "AGENTTRACE_CODEX_DIR": str(codex_out)}, timeout=60)
codex_records = [json.loads(l) for l in
                 next(codex_out.rglob("*.jsonl")).read_text().splitlines()
                 if l.strip()]
usage_rec = next((r for r in codex_records if r.get("event") == "llm_response"),
                 None)
check("codex token_count is projected into response.usage",
      usage_rec is not None
      and (usage_rec.get("response") or {}).get("usage", {}).get("input_tokens")
      == 1200,
      json.dumps(usage_rec)[:200] if usage_rec else "no llm_response record")

# ==========================================================================
# CLAIM 5 — cost is never fabricated
# ==========================================================================

mixed_store = store_with([
    v1("llm_request", 0, request_id="a", model="gpt-4o"),
    v1("llm_response", 1, request_id="a", model="gpt-4o",
       response={"usage": {"input_tokens": 1_000_000, "output_tokens": 0}}),
    v1("llm_request", 2, request_id="b", model="gpt-5"),
    v1("llm_response", 3, request_id="b", model="gpt-5",
       response={"usage": {"input_tokens": 1_000_000, "output_tokens": 0}}),
], name="mixed")
d = ds.create_dataset(mixed_store, "mixed-model")
ds.from_trace(mixed_store, d["dataset_id"],
              list_traces(mixed_store)["traces"][0]["trace_id"],
              {"tests_pass": True})
exp = ex.create_experiment(mixed_store, "mixed", d["dataset_id"],
                           ["det:tests_pass"])
ex.run_experiment(mixed_store, exp["experiment_id"])
run = ex.get_experiment(mixed_store, exp["experiment_id"])["runs"][0]
# 1M @ $2.50 (gpt-4o) + 1M @ $1.25 (gpt-5) = 3.75, NOT 2M @ 2.50 = 5.00
check("mixed-model cost is blended per model, not billed at the first model's "
      "price",
      run["cost_usd"] is not None and abs(run["cost_usd"] - 3.75) < 0.01,
      str(run["cost_usd"]))
mixed_store.close()

# an unpriced model in the mix makes the total UNKNOWN, not partial
unknown_store = store_with([
    v1("llm_request", 0, request_id="a", model="gpt-4o"),
    v1("llm_response", 1, request_id="a", model="gpt-4o",
       response={"usage": {"input_tokens": 1000}}),
    v1("llm_request", 2, request_id="b", model="space-bunny-free"),
    v1("llm_response", 3, request_id="b", model="space-bunny-free",
       response={"usage": {"input_tokens": 1000}}),
], name="unknown")
d2 = ds.create_dataset(unknown_store, "mixed-unknown")
ds.from_trace(unknown_store, d2["dataset_id"],
              list_traces(unknown_store)["traces"][0]["trace_id"], {"a": 1})
exp2 = ex.create_experiment(unknown_store, "unknown", d2["dataset_id"],
                            ["det:tests_pass"])
ex.run_experiment(unknown_store, exp2["experiment_id"])
run2 = ex.get_experiment(unknown_store, exp2["experiment_id"])["runs"][0]
check("one unpriced model in the mix -> cost unknown, not a partial sum",
      run2["cost_usd"] is None, str(run2["cost_usd"]))

# the judge rollup: confidence=0 must not vote
summ = ex.summary(unknown_store, exp2["experiment_id"])
check("pricing zero-token unknown stays None",
      cost_usd("space-bunny-free", None, None) is None)
check("gpt-4o prefix matching still works",
      cost_usd("gpt-4o-2024-08-06", 1_000_000, 0) == 2.5)
unknown_store.close()

# ==========================================================================
# ARCH findings — each with the reviewer's repro
# ==========================================================================

# A. confidence=0 scores must not decide pass_rate
jd_store = store_with([v1("llm_request", 0, request_id="r", model="gpt-4o"),
                       v1("llm_response", 1, request_id="r", model="gpt-4o",
                          response={"usage": {"input_tokens": 10}})])
d3 = ds.create_dataset(jd_store, "judge")
ds.from_trace(jd_store, d3["dataset_id"],
              list_traces(jd_store)["traces"][0]["trace_id"], {"tests_pass": True})
exp3 = ex.create_experiment(jd_store, "with-noconfig-judge", d3["dataset_id"],
                            ["det:tests_pass", "judge:task_completion"])
s3 = ex.run_experiment(jd_store, exp3["experiment_id"])
run3 = ex.get_experiment(jd_store, exp3["experiment_id"])["runs"][0]
# This trace has one llm span and no test command, so NEITHER evaluator has a
# signal. The honest verdict is "nothing was measured" — not a pass (which the
# empty `all()` produced) and not a failure (which D12 forbids).
check("a run with no measurable signal is neither pass nor fail",
      run3["status"] == "no_signal", run3["status"])
check("unconfigured judge is still recorded with its error",
      run3["scores"]["judge:task_completion"]["error"]
      and run3["scores"]["judge:task_completion"]["confidence"] == 0.0)
check("pass_rate is None when nothing could be measured",
      s3["pass_rate"] is None, str(s3["pass_rate"]))
check("summary reports the no_signal count",
      s3["no_signal"] == 1, str(s3["no_signal"]))
check("per_evaluator agrees with the run verdict",
      s3["per_evaluator"]["judge:task_completion"]["no_signal"] == 1
      and s3["per_evaluator"]["judge:task_completion"]["n"] == 0,
      json.dumps(s3["per_evaluator"]))
jd_store.close()

# ...and a real signal still produces a real verdict
sig_store = store_with([
    v1("tool_call", 0, tool={"name": "bash", "call_id": "c", "args": {"cmd": "pytest"}}),
    v1("tool_result", 1, tool={"name": "bash", "call_id": "c", "status": "ok"},
       response={"content": "3 passed"}),
])
d_sig = ds.create_dataset(sig_store, "signal")
ds.from_trace(sig_store, d_sig["dataset_id"],
              list_traces(sig_store)["traces"][0]["trace_id"], {"tests_pass": True})
exp_sig = ex.create_experiment(sig_store, "sig", d_sig["dataset_id"],
                               ["det:tests_pass"])
s_sig = ex.run_experiment(sig_store, exp_sig["experiment_id"])
check("a measurable run still gets a real pass_rate",
      s_sig["pass_rate"] == 1.0 and s_sig["no_signal"] == 0, json.dumps(s_sig)[:200])
sig_store.close()

# ---- N4: tokens spent by a span with no model make the total unknown ----
unnamed_store = store_with([
    v1("llm_request", 0, request_id="a", model="gpt-4o"),
    v1("llm_response", 1, request_id="a", model="gpt-4o",
       response={"usage": {"input_tokens": 1_000_000}}),
    # a second call that spent tokens but recorded no model
    v1("llm_request", 2, request_id="b"),
    v1("llm_response", 3, request_id="b",
       response={"usage": {"input_tokens": 1_000_000}}),
], name="unnamed")
d_un = ds.create_dataset(unnamed_store, "unnamed-model")
ds.from_trace(unnamed_store, d_un["dataset_id"],
              list_traces(unnamed_store)["traces"][0]["trace_id"], {"a": 1})
exp_un = ex.create_experiment(unnamed_store, "unnamed", d_un["dataset_id"],
                              ["det:tests_pass"])
ex.run_experiment(unnamed_store, exp_un["experiment_id"])
run_un = ex.get_experiment(unnamed_store, exp_un["experiment_id"])["runs"][0]
check("tokens from a model-less span make the total cost unknown",
      run_un["cost_usd"] is None, str(run_un["cost_usd"]))
unnamed_store.close()

# ---- N6: a stale duplicate span_end must not corrupt a finished span ----
stale_store = store_with([v1("llm_request", 0, request_id="r", model="m")])
sid = "llm:r"
stale_store.ingest({"v": 2, "event_id": "s" * 32, "type": "span_end",
                    "ts": TS % 5, "observed_at": TS % 5, "adapter_id": "hermes",
                    "session_id": "s1", "trace_id": "t1", "span_id": sid,
                    "span": {"kind": "llm", "name": "m", "status": "ok"}})
stale_store.ingest({"v": 2, "event_id": "t" * 32, "type": "span_end",
                    "ts": TS % 2, "observed_at": TS % 2, "adapter_id": "hermes",
                    "session_id": "s1", "trace_id": "t1", "span_id": sid,
                    "span": {"kind": "llm", "name": "m", "status": "error",
                             "error": {"type": "stale", "message": "old"}}})
row = stale_store.one("SELECT status, end_ts FROM spans WHERE span_id=? AND trace_id=?",
                      (sid, "t1"))
check("a stale (earlier-ts) span_end cannot overwrite a newer terminal state",
      row["status"] == "ok" and row["end_ts"] == TS % 5, str(dict(row)))
stale_store.close()

# ---- arch 4: two sessions may both own the same span id ----
collide_store = store_with([v1("llm_request", 0, request_id="r1", model="m")],
                           name="sess-a")
collide_store.ingest({"v": 2, "event_id": "c" * 32, "type": "span_start",
                      "ts": TS % 0, "observed_at": TS % 0, "adapter_id": "hermes",
                      "session_id": "s2", "trace_id": "other", "span_id": "llm:r1",
                      "span": {"kind": "llm", "name": "m2", "status": "running"}})
collide_store.ingest({"v": 2, "event_id": "d" * 32, "type": "span_end",
                      "ts": TS % 1, "observed_at": TS % 1, "adapter_id": "hermes",
                      "session_id": "s2", "trace_id": "other", "span_id": "llm:r1",
                      "span": {"kind": "llm", "name": "m2", "status": "error",
                               "error": {"type": "e", "message": "boom"}}})
rows = collide_store.query(
    "SELECT trace_id, status FROM spans WHERE span_id='llm:r1' ORDER BY trace_id")
check("the same span id in two traces yields two independent rows",
      len(rows) == 2 and rows[0]["trace_id"] != rows[1]["trace_id"],
      json.dumps([dict(r) for r in rows]))
# the first trace's span never closed in the v1 fixture, so it is `running`;
# what matters is that the SECOND trace's error did not land on it.
first = next(r for r in rows if r["trace_id"] != "other")
second = next(r for r in rows if r["trace_id"] == "other")
check("one trace's error does not contaminate the other",
      first["status"] != "error" and second["status"] == "error",
      json.dumps([dict(r) for r in rows]))
collide_store.close()

# ---- N5: an event-only trace whose trace_id equals another trace's span_id ----
collide2 = store_with([v1("llm_request", 0, request_id="r", model="m")], name="a")
collide2.ingest({"v": 2, "event_id": "e" * 32, "type": "event",
                 "ts": TS % 1, "observed_at": TS % 1, "adapter_id": "hermes",
                 "session_id": "s9", "trace_id": "llm:r", "span_id": "llm:r",
                 "event": {"name": "note", "attributes": {"text": "hello"}}})
collide2.refresh_projections()
check("an event-only trace is reachable even when its trace_id is a span id "
      "of another trace",
      get_trace(collide2, "llm:r") is not None
      and get_trace(collide2, "llm:r")["trace_id"] == "llm:r",
      str([t["trace_id"] for t in list_traces(collide2)["traces"]]))
collide2.close()

# ---- N7: the conformance half-identity check must not depend on order ----
reversed_stream = stream([
    ev("span_end", span_id="x", span={"kind": "tool", "name": "IMPOSTOR",
                                      "status": "ok"}),
    ev("span_start", span_id="x", span={"kind": "llm", "name": "real",
                                        "status": "running"}),
])
r = check_stream(reversed_stream)
check("half-vs-half mismatch is caught even with span_end first",
      not r.ok and any("starts as kind" in v for v in r.violations), r.render())

# ---- N8: key-based redaction must apply to attributes too, and must not crash
from core.privacy import redact_event  # noqa: E402

red = redact_event({"v": 2, "event_id": "r" * 32, "type": "event",
                    "ts": TS % 1, "observed_at": TS % 1, "adapter_id": "hermes",
                    "event": {"name": "note",
                              "attributes": {"api_key": "abc123",
                                             "authorization": "Basic dXNlcjpwYXNz"}}})
check("key-based redaction applies inside event.attributes",
      red["event"]["attributes"]["api_key"] == "[REDACTED]"
      and red["event"]["attributes"]["authorization"] == "[REDACTED]",
      json.dumps(red["event"]["attributes"]))
red2 = redact_event({"v": 2, "event_id": "s" * 32, "type": "event",
                     "ts": TS % 1, "observed_at": TS % 1, "adapter_id": "hermes",
                     "span": {"kind": "llm", "name": "n", "status": "running",
                              "attributes": {"api_key": "k-123456789012"}}})
check("key-based redaction applies inside span.attributes",
      red2["span"]["attributes"]["api_key"] == "[REDACTED]")
check("a malformed privacy field cannot crash the host",
      redact_event({"v": 2, "event_id": "t" * 32, "type": "event",
                    "ts": TS % 1, "observed_at": TS % 1, "adapter_id": "hermes",
                    "privacy": "metadata",
                    "event": {"name": "note"}})["privacy"]["redacted"] is True)
check("a counter named 'token' is not destroyed",
      redact_event({"v": 2, "event_id": "u" * 32, "type": "event",
                    "ts": TS % 1, "observed_at": TS % 1, "adapter_id": "hermes",
                    "span": {"kind": "llm", "name": "n", "status": "running",
                             "attributes": {"token": 4096}}}
                   )["span"]["attributes"]["token"] == 4096)

# ---- N1: per-span privacy must not be collapsed to one trace-level value ----
mixed_store2 = store_with([
    v1("llm_request", 0, request_id="r1", model="m"),
    v1("llm_response", 1, request_id="r1", model="m"),
    v1("tool_call", 2, tool={"name": "bash", "call_id": "c1", "args": {}}),
    v1("tool_result", 3, tool={"name": "bash", "call_id": "c1", "status": "ok"}),
])
mixed_trace = get_trace(mixed_store2, list_traces(mixed_store2)["traces"][0]["trace_id"])
by_id = {s["span_id"]: s for s in mixed_trace["spans"]}
# llm = metadata, tool = full: two DIFFERENT non-synthetic spans
by_id["llm:r1"]["privacy"] = {"capture": "metadata"}
by_id["tool:c1"]["privacy"] = {"capture": "full"}
back = import_payload(export_trace(mixed_trace))
captures = sorted({(e.get("privacy") or {}).get("capture")
                   for e in back if e["type"] == "span_start"})
check("per-span privacy is preserved, not stamped from one span",
      captures == ["full", "metadata"], str(captures))
by_id["tool:c1"]["attributes"] = {"retries": 2, "custom_key": "v"}
check("custom span attributes survive the round trip",
      any((e["span"].get("attributes") or {}).get("custom_key") == "v"
          for e in import_payload(export_trace(mixed_trace))
          if e["type"] == "span_start"))
mixed_store2.close()

# H. two different records in the same millisecond must not share an event_id
same_ms = [
    v1("note", 5, note="first note"),
    v1("note", 5, note="second note"),
    v1("user_prompt", 5, request={"messages": [{"role": "user", "content": "a"}]}),
    v1("user_prompt", 5, request={"messages": [{"role": "user", "content": "b"}]}),
]
ids = [convert_record(r)["event_id"] for r in same_ms]
check("same-millisecond records get distinct event_ids",
      len(set(ids)) == 4, str(ids))
check("byte-identical records still share an id (real dedup)",
      convert_record(same_ms[0])["event_id"] == convert_record(same_ms[0])["event_id"])
same_store = store_with(same_ms, name="same-ms")
check("same-millisecond records all land in the store",
      same_store.one("SELECT COUNT(*) AS n FROM events")["n"] == 4,
      str(same_store.one("SELECT COUNT(*) AS n FROM events")["n"]))
same_store.close()

# I. per_agent error count must agree with the totals
ic_store = store_with([
    v1("llm_request", 0, request_id="r"),
    v1("llm_error", 1, request_id="r", error={"type": "x", "message": "boom"}),
])
ov = overview(ic_store)
check("overview totals and per_agent agree on error count",
      ov["errors"] == 1 and ov["per_agent"][0]["errors"] == 1,
      f"totals={ov['errors']} per_agent={ov['per_agent'][0]['errors']}")
ic_store.close()

# J. the `since` filter must not clamp a trace's aggregates
j_store = store_with([
    v1("llm_request", 0, request_id="r1", model="m"),
    v1("llm_response", 2, request_id="r1", model="m",
       response={"usage": {"input_tokens": 500}}),
])
tid = list_traces(j_store)["traces"][0]["trace_id"]
full = get_trace(j_store, tid)
filtered = list_traces(j_store, since=TS % 0)["traces"]
check("since filter keeps the whole trace's start",
      filtered and filtered[0]["start"] == full["start"],
      f"{filtered[0]['start'] if filtered else None} vs {full['start']}")
check("since filter keeps the whole trace's tokens",
      filtered and filtered[0]["tokens"]["input"] == 500,
      str(filtered[0]["tokens"] if filtered else None))
check("since filter keeps the whole trace's span count",
      filtered and filtered[0]["span_count"] == len(full["spans"]),
      f"{filtered[0]['span_count'] if filtered else None} vs {len(full['spans'])}")
j_store.close()

# V. an event-only turn must be reachable
v_store = store_with([
    v1("user_prompt", 0,
       request={"messages": [{"role": "user", "content": "just talking"}]}),
    v1("assistant_message", 1, response={"content": "ok"}),
])
v_list = list_traces(v_store)
check("an event-only turn produces a reachable trace",
      v_list["total"] == 1, str(v_list["total"]))
if v_list["total"]:
    v_detail = get_trace(v_store, v_list["traces"][0]["trace_id"])
    check("its events are visible", len(v_detail["events"]) == 2,
          str(len(v_detail["events"])))
v_store.close()

# G. deduped events must survive another file's shrink
g_td = Path(tempfile.mkdtemp())
g_dir = g_td / "traces"
g_dir.mkdir()
shared = v1("note", 0, note="shared across files")
(g_dir / "a.jsonl").write_text(json.dumps(shared) + "\n", encoding="utf-8")
(g_dir / "b.jsonl").write_text(json.dumps(shared) + "\n", encoding="utf-8")
g_store = TraceStore(g_td / "g.db")
g_idx = Indexer(g_store, [g_dir])
g_idx.scan_once()
check("duplicate across files is ingested once",
      g_store.one("SELECT COUNT(*) AS n FROM events")["n"] == 1)
(g_dir / "a.jsonl").write_text("", encoding="utf-8")   # rotation: shrink to 0
g_idx.scan_once()
check("the surviving file re-supplies the deduped event after a shrink",
      g_store.one("SELECT COUNT(*) AS n FROM events")["n"] == 1,
      str(g_store.one("SELECT COUNT(*) AS n FROM events")["n"]))
g_store.close()

# N. a native span_start must be able to displace a synthetic task row
n_td = Path(tempfile.mkdtemp())
n_dir = n_td / "traces"
n_dir.mkdir()
n_store = TraceStore(n_td / "n.db")
n_idx = Indexer(n_store, [n_dir])
# a child first, so refresh_projections synthesizes the root
(n_dir / "n.jsonl").write_text(json.dumps(
    v1("llm_request", 0, request_id="r", model="m")) + "\n", encoding="utf-8")
n_idx.scan_once()
root = n_store.one("SELECT span_id, synthetic, name FROM spans WHERE kind='task'")
check("a synthetic root was materialized", root["synthetic"] == 1)
# now the adapter's own task span arrives for the same id
n_events = [
    {"v": 2, "event_id": "n" * 32, "type": "span_start",
     "ts": TS % 0, "observed_at": TS % 0, "adapter_id": "pi",
     "session_id": "s1", "trace_id": root["span_id"],
     "span_id": root["span_id"],
     "span": {"kind": "task", "name": "real task", "status": "running"}},
    {"v": 2, "event_id": "o" * 32, "type": "span_end",
     "ts": TS % 1, "observed_at": TS % 1, "adapter_id": "pi",
     "session_id": "s1", "trace_id": root["span_id"],
     "span_id": root["span_id"],
     "span": {"kind": "task", "name": "real task", "status": "error",
              "error": {"type": "boom", "message": "real failure"}}},
]
for e in n_events:
    n_store.ingest(e)
n_store.refresh_projections()
root2 = n_store.one("SELECT synthetic, name, status FROM spans WHERE span_id=?",
                    (root["span_id"],))
check("a native task span_start clears the synthetic flag",
      root2["synthetic"] == 0, str(dict(root2)))
check("...and its real name and error status are adopted",
      root2["name"] == "real task" and root2["status"] == "error",
      str(dict(root2)))
check("...so the real error now counts in overview",
      overview(n_store)["errors"] == 1, str(overview(n_store)["errors"]))
n_store.close()

# P/Q. the server's Host parsing and constant-time token compare
from server.main import make_server  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

srv_td = Path(tempfile.mkdtemp())
(srv_td / "x.jsonl").write_text(json.dumps(v1("note", 0, note="n")) + "\n",
                                encoding="utf-8")
srv_store = TraceStore(srv_td / "srv.db")
srv = make_server(srv_store, Indexer(srv_store, [srv_td]), host="127.0.0.1",
                  port=0, token="tok", scan_interval=999)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def hit(path: str, host: str) -> int:
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                               headers={"Host": host})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


check("IPv6 loopback Host with port is accepted",
      hit("/api/health?token=tok", "[::1]:9999") == 200)
check("foreign Host is still rejected",
      hit("/api/health?token=tok", "evil.com") == 403)
check("token prefix is not accepted",
      hit("/api/health?token=to", "127.0.0.1") == 401)
srv.shutdown()
srv.server_close()
srv_store.close()

# O. discovery must not hang on a non-regular manifest
import os  # noqa: E402
import subprocess as sp  # noqa: E402
import time  # noqa: E402

fifo_dir = Path(tempfile.mkdtemp())
os.mkfifo(fifo_dir / "pipe.json")
env = {**os.environ, "AGENTTRACE_ADAPTER_DIR": str(fifo_dir)}
t0 = time.time()
proc = sp.run([sys.executable, "-c",
               "import sys; sys.path.insert(0, %r);"
               "from adapter_sdk import discovery;"
               "print(len(discovery.discover()))" % str(ROOT)],
              capture_output=True, text=True, env=env, timeout=20)
check("a FIFO manifest does not hang discovery",
      proc.returncode == 0 and (time.time() - t0) < 10,
      f"rc={proc.returncode} {proc.stderr[-120:]}")

# ==========================================================================
# Round 3 — the second review of the FIXES
# ==========================================================================

# R3-1: the conformance kit must key spans by (trace_id, span_id), like the store
cross_pair = stream([
    ev("span_start", span_id="llm:r1", trace_id="T-A",
       span={"kind": "llm", "name": "n", "status": "running"}),
    ev("span_end", span_id="llm:r1", trace_id="T-B",
       span={"kind": "llm", "name": "n", "status": "ok"}),
])
r = check_stream(cross_pair)
check("a start in one trace cannot pair with an end in another",
      not r.ok and sum("without any span_start" in v for v in r.violations) == 1,
      r.render())
check("the mismatched pair is reported once, not twice",
      sum("starts as kind" in v for v in r.violations) == 0,
      str(r.violations))

# R3-2: two shrinks in ONE pass must both be detected
multi_td = Path(tempfile.mkdtemp())
multi_dir = multi_td / "traces"
multi_dir.mkdir()
for name, note in (("a", "aaaa-event"), ("b", "bbbb-event"), ("c", "cccc-event")):
    (multi_dir / f"{name}.jsonl").write_text(
        json.dumps(v1("note", 0, note=note)) + "\n", encoding="utf-8")
multi_store = TraceStore(multi_td / "m.db")
multi_idx = Indexer(multi_store, [multi_dir])
multi_idx.scan_once()
check("three files ingested", multi_store.one("SELECT COUNT(*) AS n FROM events")["n"] == 3)
(multi_dir / "b.jsonl").write_text("", encoding="utf-8")
(multi_dir / "c.jsonl").write_text("", encoding="utf-8")
report = multi_idx.scan_once()
notes = set()
for row in multi_store.query("SELECT payload FROM events"):
    ev_payload = json.loads(row["payload"])
    text = ((ev_payload.get("event") or {}).get("attributes") or {}).get("text")
    if text:
        notes.add(text)
check("a later file's shrink in the same pass is not masked",
      notes == {"aaaa-event"}, str(sorted(notes)))
multi_store.close()

# R3-3: cost must never be negative, and no tokens must mean unknown
check("a huge cache_read cannot produce a negative cost",
      (cost_usd("gpt-4o", 0, 0, 10_000_000) or 0) >= 0,
      str(cost_usd("gpt-4o", 0, 0, 10_000_000)))
check("negative token counts are clamped",
      (cost_usd("gpt-4o", -10_000_000, 0) or 0) == 0.0,
      str(cost_usd("gpt-4o", -10_000_000, 0)))
check("no token figures at all -> cost unknown, not $0",
      cost_usd("gpt-4o", None, None, None) is None)
check("a cache-only trace is still priced",
      cost_usd("gpt-4o", 0, 0, 1000) == 0.0
      or cost_usd("gpt-4o", 0, 0, 1000) == 0.0)

# R3-4/R3-5: the schema migration must survive NULL trace_id and keep indexes
mig_td = Path(tempfile.mkdtemp())
mig_db = mig_td / "old.db"
import sqlite3  # noqa: E402
mc = sqlite3.connect(mig_db)
mc.executescript(
    "CREATE TABLE spans (span_id TEXT PRIMARY KEY, trace_id TEXT, session_id TEXT,"
    " parent_span_id TEXT, adapter_id TEXT NOT NULL, kind TEXT NOT NULL,"
    " name TEXT NOT NULL, status TEXT NOT NULL, start_ts TEXT, end_ts TEXT,"
    " duration_ms INTEGER, model TEXT, input_tokens INTEGER, output_tokens INTEGER,"
    " cache_read_tokens INTEGER, input_json TEXT, output_json TEXT, error_json TEXT,"
    " attributes_json TEXT, synthetic INTEGER NOT NULL DEFAULT 0,"
    " ended INTEGER NOT NULL DEFAULT 0);"
    "CREATE INDEX idx_spans_span ON spans(span_id);"
    "CREATE INDEX idx_spans_session ON spans(session_id);"
    "CREATE INDEX idx_spans_parent ON spans(parent_span_id);")
mc.execute("INSERT INTO spans (span_id,trace_id,adapter_id,kind,name,status,"
           "synthetic,ended) VALUES ('orphan',NULL,'hermes','llm','m','ok',0,1)")
mc.execute("INSERT INTO spans (span_id,trace_id,adapter_id,kind,name,status,"
           "synthetic,ended) VALUES ('llm:r1','t1','hermes','llm','m','ok',0,1)")
mc.commit()
mc.close()
mig_store = TraceStore(mig_db)          # must not raise
check("migration survives a NULL trace_id",
      mig_store.one("SELECT trace_id FROM spans WHERE span_id='orphan'")["trace_id"]
      == "orphan")
check("migration keeps all rows",
      mig_store.one("SELECT COUNT(*) AS n FROM spans")["n"] == 2)
indexes = {r[0] for r in mig_store.query(
    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='spans'")}
check("migration re-creates the span/session/parent indexes",
      {"idx_spans_span", "idx_spans_session", "idx_spans_parent"}
      <= indexes, str(sorted(indexes)))
mig_store.close()

# R3-8/R3-9: redaction must not destroy identifiers, must catch alpha secrets
from core.privacy import redact_event as _re  # noqa: E402


def scrub(payload: dict) -> dict:
    """Redact a payload through the same path the sink uses."""
    return _re({"v": 2, "event_id": "z" * 32, "type": "span_start",
                "ts": TS % 1, "observed_at": TS % 1, "adapter_id": "hermes",
                "session_id": "s", "trace_id": "t", "span_id": "sp",
                "span": {"kind": "tool", "name": "bash", "status": "running",
                         "input": payload}})["span"]["input"]


kept = scrub({"args": {"key": "customer_profile_42"},
              "auth": "oauth", "usage": {"token": 45}})
check("a KV tool's `key` operand survives redaction",
      kept["args"]["key"] == "customer_profile_42", json.dumps(kept))
check("an auth METHOD name survives redaction",
      kept["auth"] == "oauth", json.dumps(kept))
check("a token COUNTER survives redaction", kept["usage"]["token"] == 45)
alpha = scrub({"token": "abcdefghijklmnop", "value": "zqxjkvbwmfp"})
check("an all-alphabetic secret under `token` is redacted",
      alpha["token"] == "[REDACTED]", json.dumps(alpha))
check("a value-only walk is never weaker than the event path "
      "(public redact() is key-aware)",
      _re({"v": 2, "event_id": "y" * 32, "type": "span_start",
           "ts": TS % 1, "observed_at": TS % 1, "adapter_id": "hermes",
           "span": {"kind": "llm", "name": "n", "status": "running",
                    "input": {"api_key": "k-123456789012"}}}
          )["span"]["input"]["api_key"] == "[REDACTED]")

# R3-6: summary cost fields must not contradict the verdict fields
mix_td = Path(tempfile.mkdtemp())
mix_dir = mix_td / "traces"
mix_dir.mkdir()
(mix_dir / "a.jsonl").write_text("\n".join(json.dumps(r) for r in [
    v1("tool_call", 0, tool={"name": "bash", "call_id": "c", "args": {"cmd": "pytest"}}),
    v1("tool_result", 1, tool={"name": "bash", "call_id": "c", "status": "ok"},
       response={"content": "1 passed"}),
    v1("llm_request", 2, request_id="u", model="space-bunny-free"),
    v1("llm_response", 3, request_id="u", model="space-bunny-free",
       response={"usage": {"input_tokens": 100}}),
]) + "\n", encoding="utf-8")
mix_store = TraceStore(mix_td / "mix.db")
Indexer(mix_store, [mix_dir]).scan_once()
d_mix = ds.create_dataset(mix_store, "mixed-signal")
ds.from_trace(mix_store, d_mix["dataset_id"],
              list_traces(mix_store)["traces"][0]["trace_id"], {"tests_pass": True})
exp_mix = ex.create_experiment(mix_store, "mixed", d_mix["dataset_id"],
                               ["det:tests_pass", "judge:task_completion"])
s_mix = ex.run_experiment(mix_store, exp_mix["experiment_id"])
check("an unpriced verdict run keeps cost_unknown true",
      s_mix["cost_unknown"] is True, json.dumps(
          {k: s_mix[k] for k in ("cost_usd", "cost_unknown", "pass_rate",
                                 "no_signal")}))
check("pass_rate still reflects the measurable evaluator only",
      s_mix["pass_rate"] == 1.0 and s_mix["no_signal"] == 0,
      f"pass_rate={s_mix['pass_rate']} no_signal={s_mix['no_signal']}")
mix_store.close()

# R3-11: an OTLP round trip must be idempotent ACROSS PRODUCERS, not just
# OTLP->OTLP. Found by an end-to-end smoke test: the v1 path and the OTLP path
# minted different event ids for the same span, so re-importing an export
# duplicated the trace instead of deduping it.
from core.schema.model import span_event_id  # noqa: E402

v1_path_id = convert_record(v1("llm_request", 0, request_id="r", model="m"))["event_id"]
rt_store = store_with([v1("llm_request", 0, request_id="r", model="m"),
                       v1("llm_response", 1, request_id="r", model="m",
                          response={"usage": {"input_tokens": 5}}),
                       v1("user_prompt", 2,
                          request={"messages": [{"role": "user", "content": "hi"}]})])
rt_trace = get_trace(rt_store, list_traces(rt_store)["traces"][0]["trace_id"])
before = rt_store.one("SELECT COUNT(*) AS n FROM events")["n"]
reimported = import_payload(export_trace(rt_trace))
added = sum(1 for e in reimported if rt_store.ingest(e))
check("re-importing our own OTLP export adds nothing",
      added == 0, f"{added} of {len(reimported)} events were new")
check("the archive did not grow",
      rt_store.one("SELECT COUNT(*) AS n FROM events")["n"] == before)
_conv = convert_record(v1("llm_request", 0, request_id="r", model="m"))
check("a span's identity is the canonical (trace, span, half) tuple, "
      "not the producer's",
      v1_path_id == span_event_id("hermes", _conv["trace_id"], "llm:r",
                                  "span_start"),
      v1_path_id)
check("the derived task root stayed derived (not imported as an observation)",
      rt_store.one("SELECT COUNT(*) AS n FROM events WHERE type='span_start'"
                   " AND span_id = trace_id")["n"] == 0)
check("...and is still materialized from the children",
      rt_store.one("SELECT COUNT(*) AS n FROM spans WHERE kind='task'")["n"] == 1)
rt_store.close()

# ==========================================================================
# Round 4 — found by running the pipeline over REAL traces from all three
# agents (28 rollouts of codex, real pi sessions, real hermes sessions)
# ==========================================================================

# R4-1: a span that only an END was recorded for must not round-trip into a
# start+end pair. Codex's orphan tool_results/uses produce end-only spans.
endonly = store_with([v1("llm_request", 0, request_id="r", model="m")])
endonly.ingest({"v": 2, "event_id": "e" * 31 + "1", "type": "span_end",
                "ts": TS % 5, "observed_at": TS % 5, "adapter_id": "hermes",
                "session_id": "s1", "trace_id": "tE", "span_id": "orphan-end",
                "span": {"kind": "llm", "name": "m", "status": "ok"}})
endonly.refresh_projections()
t_e = get_trace(endonly, "tE")
before_e = endonly.one("SELECT COUNT(*) AS n FROM events")["n"]
added_e = sum(1 for e in import_payload(export_trace(t_e)) if endonly.ingest(e))
check("an end-only span round-trips without inventing a span_start",
      added_e == 0, f"{added_e} event(s) invented")
check("the archive did not grow",
      endonly.one("SELECT COUNT(*) AS n FROM events")["n"] == before_e)
endonly.close()

# R4-2: re-reading the same data reports DEDUP, not "rejected"/"invalid".
# Dedup happens when an event we already hold is presented again — two files
# carrying the same record, or a full rebuild replaying the archive. The old
# single `events_rejected` counter called all of it a rejection, so 28 routine
# dedups on real data read as "28 broken events".
td_c = Path(tempfile.mkdtemp())
d = td_c / "traces"
d.mkdir()
shared = v1("note", 0, note="shared")
(d / "a.jsonl").write_text(json.dumps(shared) + "\n", encoding="utf-8")
(d / "b.jsonl").write_text(json.dumps(shared) + "\n", encoding="utf-8")
c_store = TraceStore(td_c / "c.db")
c_idx = Indexer(c_store, [d])
r1 = c_idx.scan_once()
check("the same event in two files is added once and deduped once",
      r1.events_added == 1 and r1.events_deduped == 1
      and r1.events_invalid == 0 and r1.event_id_conflicts == 0,
      str(r1.__dict__))
check("a dedup is not a problem", r1.problems == [], str(r1.problems))

# A full rebuild CLEARS the archive and re-reads the files. It used not to:
# re-reading produced the same event ids, ingest dedups by id, and the stored
# payloads kept whatever an OLDER converter had written — so a converter fix
# never reached a trace that was already indexed. The counts below are therefore
# "everything re-added, and the cross-file duplicate deduped once".
ids_before = {r["event_id"] for r in c_store.query("SELECT event_id FROM events")}
r2 = c_idx.full_rebuild()
check("a full rebuild re-ingests from the files",
      r2.events_added == 1 and r2.events_deduped == 1
      and r2.events_invalid == 0 and r2.event_id_conflicts == 0,
      str(r2.__dict__))
check("...to the same event ids (the conversion is deterministic)",
      {r["event_id"] for r in c_store.query("SELECT event_id FROM events")}
      == ids_before)
check("an unchanged scan is entirely quiet",
      c_idx.scan_once().__dict__["events_added"] == 0)

# ...and the reason for clearing: a payload written by an older converter is
# REPLACED. Simulated by overwriting what is on disk in the projection.
c_store._conn().execute(
    "UPDATE events SET payload = ? WHERE event_id = ?",
    (json.dumps({"v": 2, "event_id": next(iter(ids_before)), "type": "event",
                 "ts": TS % 1, "observed_at": TS % 1, "adapter_id": "hermes",
                 "session_id": "s", "trace_id": "old",
                 "event": {"name": "note", "attributes": {"text": "STALE"}}}),
     next(iter(ids_before))))
c_store._conn().commit()
check("a stale stored payload is present before the rebuild",
      "STALE" in c_store.one("SELECT payload FROM events WHERE event_id = ?",
                            (next(iter(ids_before)),))["payload"])
c_idx.full_rebuild()
check("a full rebuild REPLACES a payload an older converter wrote",
      "STALE" not in c_store.one("SELECT payload FROM events WHERE event_id = ?",
                                (next(iter(ids_before)),))["payload"],
      "the archive was cleared, so the file's version won")

# same id, DIFFERENT payload is the one case that IS an adapter bug
c_store.ingest({"v": 2, "event_id": shared and convert_record(shared)["event_id"],
                "type": "event", "ts": TS % 3, "observed_at": TS % 3,
                "adapter_id": "hermes", "session_id": "s", "trace_id": "other",
                "event": {"name": "note", "attributes": {"text": "DIFFERENT"}}})
check("a conflicting payload raises the conflict counter",
      c_store.duplicate_conflicts == 1, str(c_store.duplicate_conflicts))

# ...and a genuine contract violation is reported separately
c_store.ingest({"v": 2, "event_id": "q" * 32, "type": "span_end",
                "ts": "not-a-timestamp", "observed_at": TS % 0,
                "adapter_id": "hermes", "session_id": "s", "trace_id": "t",
                "span_id": "sp", "span": {"kind": "llm", "name": "n",
                                          "status": "ok"}})
check("an invalid event raises the invalid counter",
      c_store.invalid == 1, str(c_store.invalid))
c_store.close()

# R4-3: a trace's root span status must AGREE with the trace's own status, and
# a synthetic root must not vote in its own aggregates (the root being
# `interrupted` used to flip the trace from `incomplete` to `error`).
cons = store_with([
    v1("llm_request", 0, request_id="a", model="m"),
    v1("llm_response", 1, request_id="a", model="m"),
    v1("llm_request", 2, turn_id="t2", request_id="b", model="m"),  # never ends
], name="consistency")
for t in list_traces(cons, limit=10)["traces"]:
    d = get_trace(cons, t["trace_id"])
    root = next((sp for sp in d["spans"] if sp["kind"] == "task"), None)
    expected_root = {"error": "error", "incomplete": "interrupted",
                     "ok": "ok"}[t["status"]]
    check(f"root status agrees with trace status for {t['status']}",
          root is None or root["status"] == expected_root,
          f"trace={t['status']} root={root and root['status']}")
check("the interrupted trace is not also counted as an error",
      overview(cons)["errors"] == 0 and overview(cons)["unclosed"] == 1,
      f"errors={overview(cons)['errors']} unclosed={overview(cons)['unclosed']}")
cons.close()

# R4-4: codex's per-turn request/response pair. A turn's usage must land on
# that turn's llm span, and two token_count readings must not collide.
codex_roll = Path(tempfile.mkdtemp()) / "rollout-x.jsonl"
codex_roll.write_text("\n".join(json.dumps(l) for l in [
    {"type": "session_meta", "payload": {"id": "s-cx", "cwd": "/tmp",
                                         "model_provider": "openai",
                                         "base_instructions": {"text": "SYS"}}},
    {"type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-5"}},
    {"type": "event_msg", "payload": {"type": "user_message",
                                      "message": "one", "turn_id": "t1"}},
    {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": 100, "output_tokens": 5}}}},
    {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": 220, "output_tokens": 9}}}},
    {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1",
                                      "last_agent_message": "reply one"}},
    {"type": "turn_context", "payload": {"turn_id": "t2", "model": "gpt-5"}},
    {"type": "event_msg", "payload": {"type": "user_message",
                                      "message": "two", "turn_id": "t2"}},
    {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": 400, "output_tokens": 20}}}},
    {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t2",
                                      "last_agent_message": "reply two"}},
]) + "\n", encoding="utf-8")
cx_out = Path(tempfile.mkdtemp())
import subprocess as _sp  # noqa: E402
_sp.run([sys.executable, str(ROOT / "codex" / "codex_import.py"), str(codex_roll)],
        capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "AGENTTRACE_CODEX_DIR": str(cx_out),
             "AGENTTRACE_STATE_DIR": str(cx_out / "state")})
cx_recs = [json.loads(l) for l in
           next(cx_out.rglob("*.jsonl")).read_text().splitlines() if l.strip()]
reqs = [r for r in cx_recs if r["event"] == "llm_request"]
resp = [r for r in cx_recs if r["event"] == "llm_response"]
check("codex emits one request/response pair per turn",
      len(reqs) == 2 and len(resp) == 2, f"{len(reqs)}/{len(resp)}")
check("each codex turn's pair joins on request_id",
      {r["request_id"] for r in reqs} == {r["request_id"] for r in resp},
      str([r.get("request_id") for r in reqs + resp]))
check("no request_id carries two readings",
      len({r["request_id"] for r in resp}) == len(resp))
check("the turn's usage is the last reading, on that turn's response",
      [(r["turn_id"], (r["response"].get("usage") or {}).get("input_tokens"))
       for r in resp] == [("t1", 220), ("t2", 400)],
      str([(r["turn_id"], r["response"].get("usage")) for r in resp]))
check("the system prompt is stored on the first turn only",
      bool(reqs[0]["request"].get("system_prompt"))
      and not reqs[1]["request"].get("system_prompt"))

cx_store = TraceStore(cx_out / "cx.db")
Indexer(cx_store, [cx_out]).scan_once()
cx_llm = cx_store.query("SELECT ended, input_tokens FROM spans"
                        " WHERE kind='llm' ORDER BY start_ts")
check("every codex llm span closed with its usage projected",
      len(cx_llm) == 2 and all(r["ended"] for r in cx_llm)
      and [r["input_tokens"] for r in cx_llm] == [220, 400],
      str([dict(r) for r in cx_llm]))
cx_store.close()

# R4-5: the EXPORT MUST BE EXACT. A full-payload re-import comparison is the
# only thing that catches "the round trip lost a field", and it is what
# surfaced the nested-None -> "null" encoding, the missing per-half
# timestamps/attributes, and event privacy/source being dropped.
rt_exact = store_with([
    v1("user_prompt", 0, request={"messages": [{"role": "user", "content": "hi"}]}),
    v1("llm_request", 1, request_id="r", model="gpt-4o",
       request={"messages": [], "system_prompt": None, "instructions": None,
                "tools": [{"name": "bash"}], "tool_count": 1}),
    v1("llm_response", 2, request_id="r", model="gpt-4o",
       response={"content": "done", "usage": {"input_tokens": 10}},
       duration_ms=5),
], name="rt-exact")
t_rt = get_trace(rt_exact, list_traces(rt_exact)["traces"][0]["trace_id"])
_tid = t_rt["trace_id"]
raw = list(rt_exact.iter_events(_tid))
before_ids = {json.loads(r["payload"])["event_id"]
              for r in rt_exact.query("SELECT payload FROM events WHERE trace_id=?",
                                      (_tid,))}
stored = {json.loads(r["payload"])["event_id"]: json.loads(r["payload"])
          for r in rt_exact.query("SELECT payload FROM events WHERE trace_id=?",
                                  (_tid,))}
c_before = rt_exact.duplicate_conflicts
back_rt = import_payload(export_trace(t_rt, raw))
check("the exact export reproduces the same event ids",
      {e["event_id"] for e in back_rt} == before_ids,
      f"{len({e['event_id'] for e in back_rt})} vs {len(before_ids)}")
added_rt = sum(1 for e in back_rt if rt_exact.ingest(e))
check("re-importing the exact export adds nothing and conflicts nowhere",
      added_rt == 0 and rt_exact.duplicate_conflicts == c_before,
      f"added={added_rt} conflicts={rt_exact.duplicate_conflicts - c_before}")
# a nested null must not come back as the STRING "null"
for e in back_rt:
    if e["type"] == "span_start" and e["span_id"] == "llm:r":
        req = e["span"].get("input") or {}
        check("a nested null stays absent, not the string \"null\"",
              req.get("system_prompt") is None and req.get("instructions") is None,
              json.dumps(req)[:160])
        check("...and the real keys around it survive",
              req.get("tool_count") == 1 and req.get("tools"),
              json.dumps(req)[:160])
# per-half attribute attribution must survive (the projection merges it)
rt_exact.ingest({"v": 2, "event_id": "h" * 32, "type": "span_start",
                 "ts": TS % 3, "observed_at": TS % 3, "adapter_id": "hermes",
                 "session_id": "s1", "trace_id": _tid, "span_id": "llm:r",
                 "span": {"kind": "llm", "name": "gpt-4o", "status": "running",
                          "attributes": {"cwd": "/tmp", "start_only": 1}}})
t_rt2 = get_trace(rt_exact, _tid)
back2 = import_payload(export_trace(t_rt2, list(rt_exact.iter_events(_tid))))
start_half = next(e for e in back2 if e["type"] == "span_start"
                  and e["span_id"] == "llm:r")
check("start-half attributes are not mixed with the end half's",
      (start_half["span"].get("attributes") or {}).get("start_only") == 1,
      json.dumps(start_half["span"].get("attributes"))[:160])
rt_exact.close()

# R4-6: the identity-based conflict rule. Two different events sharing an id is
# a conflict; the same event with a normalised payload is not.
ident = store_with([v1("note", 0, note="one")])
ident.ingest({"v": 2, "event_id": "i" * 32, "type": "event", "ts": TS % 1,
              "observed_at": TS % 1, "adapter_id": "hermes", "session_id": "s",
              "trace_id": "t", "event": {"name": "note",
                                         "attributes": {"text": "one"}}})
c1 = ident.duplicate_conflicts
ident.ingest({"v": 2, "event_id": "i" * 32, "type": "event", "ts": TS % 1,
              "observed_at": TS % 1, "adapter_id": "hermes", "session_id": "s",
              "trace_id": "t", "event": {"name": "note"}})   # payload normalised
check("a normalised payload for the same identity is not a conflict",
      ident.duplicate_conflicts == c1, str(ident.duplicate_conflicts))
ident.ingest({"v": 2, "event_id": "i" * 32, "type": "event", "ts": TS % 9,
              "observed_at": TS % 9, "adapter_id": "hermes", "session_id": "s",
              "trace_id": "t", "event": {"name": "note"}})   # different time
check("an id reused for a different EVENT is a conflict",
      ident.duplicate_conflicts == c1 + 1, str(ident.duplicate_conflicts))
ident.close()

# R4-7: the e2e record-count gate must actually gate. It used to print "0\n0"
# when a directory was empty (cat failed AND wc counted zero), which made
# `[ "$after" -le "$before" ]` error out and stop comparing anything.
for script in ("e2e-pi.sh", "e2e-hermes.sh"):
    check(f"{script} counts records without relying on glob success",
          "count_records()" in (ROOT / "test" / script).read_text(encoding="utf-8"))

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("review regressions OK")
