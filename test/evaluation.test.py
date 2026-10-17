#!/usr/bin/env python3
"""M3: datasets, evaluators, experiments — logic and API, end to end.

Two fixture traces are the whole story:
  A: agent runs pytest, tests pass, says so.        (clean)
  B: agent retries the same failing pytest 3x,      (flailing + false claim)
     grows context 10x, then declares success.

Every evaluator's reading of A vs B is pinned, plus the platform guarantees:
examples store pointers not trace copies, experiments pin evaluator versions,
cost is unknown for unknown models, and an unconfigured judge degrades to
confidence=0 instead of fabricating a verdict.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.query import get_trace  # noqa: E402
from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402
from evaluation import datasets as ds  # noqa: E402
from evaluation import experiments as ex  # noqa: E402
from evaluation.evaluators import get_evaluator  # noqa: E402
from evaluation.model import Example, TraceView  # noqa: E402

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
           "session_id": "s1"}
    rec.update(kw)
    return rec


td = Path(tempfile.mkdtemp())
(traces_dir := td / "traces").mkdir()
(traces_dir / "a.jsonl").write_text("\n".join(json.dumps(r) for r in [
    # --- trace A: clean run ------------------------------------------------
    v1("user_prompt", 0, turn_id="ta",
       request={"messages": [{"role": "user", "content": "fix the failing test"}]}),
    v1("llm_request", 1, turn_id="ta", request_id="ra", model="gpt-4o"),
    v1("tool_call", 2, turn_id="ta",
       tool={"name": "bash", "call_id": "ca1", "args": {"cmd": "pytest -x"}}),
    v1("tool_result", 3, turn_id="ta",
       tool={"name": "bash", "call_id": "ca1", "status": "ok"},
       response={"content": "3 passed in 0.4s"}, duration_ms=400),
    v1("llm_response", 4, turn_id="ta", request_id="ra", model="gpt-4o",
       response={"content": "Fixed — all tests pass now.",
                 "usage": {"input_tokens": 4200, "output_tokens": 310,
                           "cache_read_tokens": 2000}}),
    # --- trace B: flailing + false claim -----------------------------------
    v1("user_prompt", 10, turn_id="tb",
       request={"messages": [{"role": "user", "content": "fix the other test"}]}),
    v1("llm_request", 11, turn_id="tb", request_id="rb1", model="space-bunny"),
    v1("llm_response", 12, turn_id="tb", request_id="rb1",
       response={"usage": {"input_tokens": 1000, "output_tokens": 50}}),
    v1("tool_call", 13, turn_id="tb",
       tool={"name": "bash", "call_id": "cb1", "args": {"cmd": "pytest -x"}}),
    v1("tool_result", 14, turn_id="tb",
       tool={"name": "bash", "call_id": "cb1", "status": "error"},
       response={"content": "1 failed"}, error={"type": "exit", "message": "rc=1"}),
    v1("tool_call", 15, turn_id="tb",
       tool={"name": "bash", "call_id": "cb2", "args": {"cmd": "pytest -x"}}),
    v1("tool_result", 16, turn_id="tb",
       tool={"name": "bash", "call_id": "cb2", "status": "error"},
       response={"content": "1 failed"}, error={"type": "exit", "message": "rc=1"}),
    v1("tool_call", 17, turn_id="tb",
       tool={"name": "bash", "call_id": "cb3", "args": {"cmd": "pytest -x"}}),
    v1("tool_result", 18, turn_id="tb",
       tool={"name": "bash", "call_id": "cb3", "status": "error"},
       response={"content": "1 failed"}, error={"type": "exit", "message": "rc=1"}),
    v1("llm_request", 19, turn_id="tb", request_id="rb2", model="space-bunny"),
    v1("llm_response", 20, turn_id="tb", request_id="rb2",
       response={"usage": {"input_tokens": 5000, "output_tokens": 80}}),
    v1("llm_request", 21, turn_id="tb", request_id="rb3", model="space-bunny"),
    v1("llm_response", 22, turn_id="tb", request_id="rb3",
       response={"content": "All tests pass now!",
                 "usage": {"input_tokens": 10000, "output_tokens": 120}}),
]) + "\n", encoding="utf-8")

store = TraceStore(td / "eval.db")
Indexer(store, [traces_dir]).scan_once()

traces = {t["name"]: t for t in
          __import__("core.query", fromlist=["list_traces"]).list_traces(store)["traces"]} \
    if False else None
from core.query import list_traces  # noqa: E402
tlist = list_traces(store)["traces"]
trace_a = next(t for t in tlist if t["status"] == "ok")
trace_b = next(t for t in tlist if t["status"] == "error")
view_a = TraceView(get_trace(store, trace_a["trace_id"]))
view_b = TraceView(get_trace(store, trace_b["trace_id"]))
example = Example("e1", "d", {"task": "fix the failing test"},
                  {"tests_pass": True})

# --- deterministic evaluators ----------------------------------------------
tp = get_evaluator("det:tests_pass")
sa, sb = tp.evaluate(example, view_a), tp.evaluate(example, view_b)
check("tests_pass: A passes", sa.passed and sa.score == 1.0, str(sa.to_dict()))
check("tests_pass: B fails with evidence",
      not sb.passed and "last test run failed" in sb.evidence[0])

cv = get_evaluator("det:claimed_vs_actual")
check("claimed_vs_actual: A consistent",
      cv.evaluate(example, view_a).passed)
sb_cv = cv.evaluate(example, view_b)
check("claimed_vs_actual: B catches the false claim",
      not sb_cv.passed and "claims success=True" in sb_cv.evidence[0],
      str(sb_cv.to_dict()))

ne = get_evaluator("det:no_errors")
check("no_errors: A clean / B flags 3 spans",
      ne.evaluate(example, view_a).passed
      and len(ne.evaluate(example, view_b).evidence) == 3)

oc = get_evaluator("det:output_contains")
check("output_contains: hit + miss",
      oc.evaluate(Example("e", "d", {}, {"contains": ["tests pass"]}), view_a).passed
      and not oc.evaluate(Example("e", "d", {}, {"contains": ["nope"]}), view_a).passed)
check("output_contains: no expectation -> confidence 0",
      oc.evaluate(Example("e", "d", {}, {}), view_a).confidence == 0.0)

bud = get_evaluator("det:budget")
check("budget: within and exceeded",
      bud.evaluate(Example("e", "d", {}, {"max_tool_calls": 5}), view_a).passed
      and not bud.evaluate(Example("e", "d", {}, {"max_tool_calls": 2}), view_b).passed)

# --- trajectory evaluators ---------------------------------------------------
rl = get_evaluator("traj:retry_loops")
sb_rl = rl.evaluate(example, view_b)
check("retry_loops: B's 3x identical failure caught",
      not sb_rl.passed and "x3" in sb_rl.evidence[0], str(sb_rl.to_dict()))
check("retry_loops: A clean", rl.evaluate(example, view_a).passed)

er = get_evaluator("traj:error_recovery")
check("error_recovery: B never recovers",
      er.evaluate(example, view_b).score == 0.0)
check("error_recovery: A has nothing to recover",
      er.evaluate(example, view_a).passed)

cg = get_evaluator("traj:context_growth")
sb_cg = cg.evaluate(example, view_b)
check("context_growth: B's 10x growth flagged",
      not sb_cg.passed and "x10.0" in sb_cg.evidence[0], str(sb_cg.to_dict()))
check("context_growth: A too short to judge -> low confidence pass",
      cg.evaluate(example, view_a).passed)

fa = get_evaluator("traj:first_action_latency")
check("first_action_latency: A acts in 1 min",
      fa.evaluate(example, view_a).passed)

judge = get_evaluator("judge:task_completion")
sj = judge.evaluate(example, view_a)
check("judge unconfigured -> confidence 0 + error, no fake verdict",
      sj.confidence == 0.0 and sj.error and not sj.passed)

# --- datasets -----------------------------------------------------------------
d = ds.create_dataset(store, "coding-tasks", "regression set")
exa = ds.from_trace(store, d["dataset_id"], trace_a["trace_id"],
                    {"tests_pass": True})
check("example from trace A", exa is not None)
check("from_trace extracts the task only",
      exa.inputs["task"] == "fix the failing test")
meta = exa.metadata
check("metadata points back, does not copy",
      meta["source_trace_id"] == trace_a["trace_id"]
      and "response" not in json.dumps(meta) and "messages" not in json.dumps(meta))
ds.from_trace(store, d["dataset_id"], trace_b["trace_id"], {"tests_pass": True})

exported = ds.export_jsonl(store, d["dataset_id"])
check("export round-trips through import",
      ds.import_jsonl(store, exported)["imported"] == 2)
check("import skips garbage lines",
      ds.import_jsonl(store, "{bad\n" + exported)["skipped"] == 1)

# --- experiments ---------------------------------------------------------------
eval_ids = ["det:tests_pass", "det:claimed_vs_actual", "traj:retry_loops",
            "traj:error_recovery"]
e1 = ex.create_experiment(store, "gpt-4o run", d["dataset_id"], eval_ids,
                          {"model": "gpt-4o"})
check("experiment pins evaluator versions",
      all(v == "1" for v in e1["evaluator_ids"].values()))
s1 = ex.run_experiment(store, e1["experiment_id"])
check("auto-attach via source_trace_id: both examples scored",
      s1["scored"] == 2, json.dumps(s1, indent=0)[:300])
check("pass rate 0.5 (A ok, B fails)",
      s1["pass_rate"] == 0.5)
check("per-evaluator aggregates",
      s1["per_evaluator"]["det:tests_pass"]["pass_rate"] == 0.5
      and s1["per_evaluator"]["traj:retry_loops"]["mean_score"] == 0.5)
check("cost computed only for the gpt-4o-attached trace",
      s1["cost_usd"] is not None and s1["cost_unknown"] is True,
      str(s1["cost_usd"]))

run_a = next(r for r in ex.get_experiment(store, e1["experiment_id"])["runs"]
             if r["status"] == "ok")
check("trace A run priced via pricing registry",
      abs((run_a["cost_usd"] or 0) - 0.0111) < 0.001, str(run_a["cost_usd"]))

e2 = ex.create_experiment(store, "space-bunny run", d["dataset_id"], eval_ids,
                          {"model": "space-bunny"})
s2 = ex.run_experiment(store, e2["experiment_id"])
e2_runs = ex.get_experiment(store, e2["experiment_id"])["runs"]
run_b = next(r for r in e2_runs if r["status"] == "error")
check("trace with unknown model -> run cost unknown, tokens still counted",
      run_b["cost_usd"] is None and (run_b["tokens_in"] or 0) > 0)
check("summary flags partial cost coverage",
      s2["cost_unknown"] is True and s2["tokens"]["input"] > 0)

cmp = ex.compare(store, e1["experiment_id"], e2["experiment_id"])
check("compare produces per-evaluator deltas",
      cmp["deltas"]["det:tests_pass"]["delta"] == 0.0)
check("playback pricing reflects traces, not declarations (same traces -> 0)",
      cmp["cost_delta"] == 0.0)

# --- HTTP routes ----------------------------------------------------------------
from server.main import make_server  # noqa: E402

server = make_server(store, Indexer(store, [traces_dir]), host="127.0.0.1",
                     port=0, token="t", scan_interval=999)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{port}"


def req(path: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path + ("&" if "?" in path else "?")
                               + "token=t", data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


code, body = req("/api/datasets", "POST", {"name": "api-set"})
check("POST /api/datasets", code == 201 and body["name"] == "api-set")
dsid = body["dataset_id"]
code, body = req(f"/api/datasets/{dsid}/from_trace", "POST",
                 {"trace_id": trace_a["trace_id"], "expected": {"tests_pass": True}})
check("POST from_trace", code == 201
      and body["inputs"]["task"] == "fix the failing test")
code, body = req(f"/api/datasets/{dsid}/from_trace", "POST", {"trace_id": "nope"})
check("from_trace unknown trace -> 404", code == 404)
code, body = req("/api/experiments", "POST",
                 {"name": "api-exp", "dataset_id": dsid,
                  "evaluator_ids": ["det:tests_pass"]})
check("POST /api/experiments", code == 201)
expid = body["experiment_id"]
code, body = req(f"/api/experiments/{expid}/run", "POST", {})
check("POST run scores the attached example",
      code == 200 and body["scored"] == 1 and body["pass_rate"] == 1.0)
code, body = req(f"/api/compare?a={e1['experiment_id']}&b={expid}")
check("GET compare", code == 200 and "deltas" in body)
code, body = req("/api/evaluators")
check("GET evaluators lists all three kinds",
      code == 200 and {e["kind"] for e in body["evaluators"]}
      == {"deterministic", "trajectory", "llm_judge"})
# export is ndjson (not JSON) — read raw
with urllib.request.urlopen(BASE + f"/api/datasets/{dsid}/export?token=t",
                            timeout=5) as resp:
    ndjson = resp.read().decode()
check("GET export is ndjson with header + examples",
      '"type": "dataset"' in ndjson and ndjson.count('"type": "example"') == 1)

server.shutdown()
server.server_close()
store.close()

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("evaluation OK")
