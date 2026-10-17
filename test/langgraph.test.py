#!/usr/bin/env python3
"""LangChain / LangGraph callback adapter.

The handler is exercised the way LangChain actually drives it: a run_id-paired
sequence of callbacks, including a nested chain (subgraph), a failing tool,
and a streaming token callback. Asserted properties: the tree comes out right,
tokens map, errors are terminal, and — because the handler must never break
the host — a hostile payload (object whose repr raises) does not raise.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.langgraph import CallbackTraceHandler, LangGraphAdapter  # noqa: E402
from adapter_sdk.capabilities import Capability, SupportLevel  # noqa: E402
from adapter_sdk.event_sink import JsonlEventSink  # noqa: E402
from adapter_sdk.testing import check_stream  # noqa: E402

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


td = Path(tempfile.mkdtemp())
stream = td / "langgraph.jsonl"
sink = JsonlEventSink(stream)
handler = CallbackTraceHandler(sink, session_id="lg-1", agent_name="langgraph")

# --- a realistic LangGraph run ---------------------------------------------
# graph invocation (root chain) -> node chain -> llm -> tool (fails) -> recovery
handler.on_chain_start(run_id="root", name="LangGraph", inputs={"q": "fix tests"})
handler.on_chain_start(run_id="node1", name="agent", parent_run_id="root",
                       inputs={"messages": []})
handler.on_chat_model_start(
    run_id="llm1", parent_run_id="node1",
    serialized={"name": "ChatOpenAI", "kwargs": {"model_name": "gpt-4o"}},
    messages=[[{"role": "user", "content": "fix the tests"}]])


class _Msg:
    content = "I'll run the tests."
    response_metadata = {"model_name": "gpt-4o"}


class _Gen:
    message = _Msg()
    text = "I'll run the tests."


class _Result:
    generations = [[_Gen()]]
    llm_output = {"model_name": "gpt-4o",
                  "token_usage": {"prompt_tokens": 1200,
                                  "completion_tokens": 40,
                                  "prompt_tokens_details": {"cached_tokens": 800}}}


handler.on_llm_end(run_id="llm1", response=_Result(), parent_run_id="node1")
handler.on_tool_start(run_id="tool1", name="bash",
                      inputs={"command": "pytest -x"}, parent_run_id="node1")
handler.on_tool_error(run_id="tool1", error=RuntimeError("exit code 1"),
                      parent_run_id="node1")
handler.on_tool_start(run_id="tool2", name="edit_file",
                      inputs={"path": "test_x.py"}, parent_run_id="node1")
handler.on_tool_end(run_id="tool2", output="patched", parent_run_id="node1")
handler.on_chain_end(run_id="node1", outputs={"ok": True}, parent_run_id="root")
handler.on_chain_end(run_id="root", outputs={"answer": "fixed"}, parent_run_id=None)

report = check_stream(stream, adapter_id="langgraph")
check("callback stream is conformant", report.ok, report.render())
check("stream stats show the tree", report.stats.get("span.task") == 1
      and report.stats.get("span.chain") == 1
      and report.stats.get("span.llm") == 1
      and report.stats.get("span.tool") == 2, str(report.stats))

lines = [__import__("json").loads(l) for l in
         stream.read_text(encoding="utf-8").splitlines()]
starts = {e["span_id"]: e for e in lines if e["type"] == "span_start"}
ends = {e["span_id"]: e for e in lines if e["type"] == "span_end"}
check("outermost chain became the task root",
      starts["root"]["span"]["kind"] == "task"
      and starts["root"]["parent_span_id"] is None)
check("everything shares the root's trace id",
      len({e["trace_id"] for e in lines}) == 1
      and next(iter({e["trace_id"] for e in lines})) == "root")
check("nested chain is a chain, not a task",
      starts["node1"]["span"]["kind"] == "chain"
      and starts["node1"]["parent_span_id"] == "root")
check("llm/tool parented to the node that spawned them",
      starts["llm1"]["parent_span_id"] == "node1"
      and starts["tool1"]["parent_span_id"] == "node1")
check("session id carried on every event",
      all(e["session_id"] == "lg-1" for e in lines))
check("llm tokens mapped, cache read included",
      ends["llm1"]["span"]["attributes"]["usage"] == {
          "input_tokens": 1200, "output_tokens": 40, "cache_read_tokens": 800})
check("llm model captured", ends["llm1"]["span"]["attributes"]["model"] == "gpt-4o")
check("llm content captured",
      "run the tests" in ends["llm1"]["span"]["output"]["content"])
check("failed tool is terminal error with the exception type",
      ends["tool1"]["span"]["status"] == "error"
      and ends["tool1"]["span"]["error"]["type"] == "RuntimeError"
      and ends["tool1"]["span"]["error"]["message"] == "exit code 1")
check("recovered tool is ok", ends["tool2"]["span"]["status"] == "ok")

# --- streaming tokens are opt-in ------------------------------------------
before = len(lines)
handler.on_llm_new_token("chunk", run_id="llm1")
sink2 = JsonlEventSink(td / "stream-on.jsonl")
handler.stream_tokens = True
handler.sink = sink2
handler.on_llm_new_token("chunk", run_id="llm1")
check("token callbacks ignored by default",
      len(stream.read_text(encoding='utf-8').splitlines()) == before)
check("token callbacks emitted as events when enabled",
      sink2.events_written == 1)

# --- closure: an interrupted run leaves an unclosed span (reported, ok) -----
sink3 = JsonlEventSink(td / "interrupted.jsonl")
handler2 = CallbackTraceHandler(sink3, session_id="lg-2")
handler2.on_chain_start(run_id="g", name="graph")
handler2.on_tool_start(run_id="t", name="bash", inputs={},
                       parent_run_id="g")
r2 = check_stream(td / "interrupted.jsonl", adapter_id="langgraph")
check("interrupted run is conformant with an unclosed-span warning",
      r2.ok and any("never closed" in w for w in r2.warnings))

# --- host safety ------------------------------------------------------------
class _Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("repr exploded")


sink4 = JsonlEventSink(td / "hostile.jsonl")
handler3 = CallbackTraceHandler(sink4, session_id="lg-3")
try:
    handler3.on_tool_start(run_id="h", name="bash", inputs={"x": _Hostile()})
    ok = True
except Exception as exc:  # noqa: BLE001
    ok = False
    print(f"      raised: {exc}")
check("hostile payload does not raise into the host", ok)

# --- capability honesty ------------------------------------------------------
caps = LangGraphAdapter().capabilities
check("streaming declared partial (off by default)",
      caps.level(Capability.STREAMING) == SupportLevel.PARTIAL)
check("subagents declared supported (nested chains)",
      caps.level(Capability.SUBAGENTS) == SupportLevel.SUPPORTED)
check("session lifecycle declared partial (caller supplies id)",
      caps.level(Capability.SESSION_LIFECYCLE) == SupportLevel.PARTIAL)

# --- the shipped example must keep working (guards docs/ from rotting) -----
import json as _json  # noqa: E402
import os as _os  # noqa: E402
import subprocess as _sp  # noqa: E402

_ex_dir = td / "example-home"
env = {**_os.environ, "AGENTTRACE_HOME": str(_ex_dir)}
proc = _sp.run([sys.executable, str(ROOT / "examples" / "langgraph_agent.py")],
               capture_output=True, text=True, env=env, timeout=60)
check("examples/langgraph_agent.py runs clean",
      proc.returncode == 0, proc.stderr[-300:])
ex_path = _ex_dir / "langgraph-example.jsonl"
check("the example's stream is conformant",
      ex_path.is_file()
      and check_stream(ex_path, adapter_id="langgraph").ok)

from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402
ex_store = TraceStore(td / "example.db")
ex_report = Indexer(ex_store, [_ex_dir]).scan_once()
check("the example's stream indexes into the store",
      ex_report.events_added > 0 and ex_report.corrupt_lines == 0,
      _json.dumps(ex_report.__dict__)[:200])
ex_store.close()

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("langgraph adapter OK")
