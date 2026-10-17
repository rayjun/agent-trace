#!/usr/bin/env python3
"""Trace a LangGraph-shaped agent run — no framework install required.

This drives `CallbackTraceHandler` with the exact callback sequence LangChain
and LangGraph emit. In a real app you would replace the fake callbacks with:

    graph.invoke(state, config={"callbacks": [handler]})

and keep everything else identical. The fake sequence is kept runnable so the
example is testable and so the callback contract is visible:

    on_chain_start(root)                -> task span (the graph invocation)
      on_chain_start(node)              -> chain span (a node / subgraph)
        on_chat_model_start / on_llm_end
        on_tool_start / on_tool_error   -> a failing tool
        on_tool_start / on_tool_end     -> the recovery
      on_chain_end(node)
    on_chain_end(root)

Run it, then look at the result:

    python3 examples/langgraph_agent.py
    agenttrace index && agenttrace serve
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.langgraph import CallbackTraceHandler  # noqa: E402
from adapter_sdk.event_sink import JsonlEventSink  # noqa: E402


def trace_path() -> Path:
    """~/.agent-trace is one of the dirs `agenttrace index` scans by default."""
    override = os.environ.get("AGENTTRACE_HOME")
    base = Path(override) if override else Path.home() / ".agent-trace"
    return base / "langgraph-example.jsonl"


# --------------------------------------------------------------------------
# Stand-ins for what LangChain hands the callbacks. In a real run these are
# LLMResult / BaseMessage / Document objects; the handler reads only the
# attributes below, defensively.
# --------------------------------------------------------------------------

class _Message:
    content = "The failing test expects 3 items but got 2."
    response_metadata = {"model_name": "gpt-4o"}


class _Generation:
    message = _Message()
    text = _Message.content


class _LLMResult:
    generations = [[_Generation()]]
    llm_output = {
        "model_name": "gpt-4o",
        "token_usage": {"prompt_tokens": 1850, "completion_tokens": 62,
                        "prompt_tokens_details": {"cached_tokens": 1200}},
    }


def main() -> int:
    path = trace_path()
    sink = JsonlEventSink(path)
    handler = CallbackTraceHandler(sink, session_id="example-session",
                                   agent_name="langgraph")

    # root chain == the graph invocation == the trace's task span
    handler.on_chain_start(run_id="root", name="LangGraph",
                           inputs={"task": "fix the failing test"})

    # node 1: the model plans, then runs the tests
    handler.on_chain_start(run_id="node1", name="agent", parent_run_id="root",
                           inputs={"messages": []})
    handler.on_chat_model_start(
        run_id="llm1", parent_run_id="node1",
        serialized={"name": "ChatOpenAI", "kwargs": {"model_name": "gpt-4o"}},
        messages=[[{"role": "user", "content": "fix the failing test"}]])
    handler.on_llm_end(run_id="llm1", response=_LLMResult(), parent_run_id="node1")

    handler.on_tool_start(run_id="tool1", name="bash",
                          inputs={"command": "pytest -x"}, parent_run_id="node1")
    handler.on_tool_error(run_id="tool1", error=RuntimeError("exit code 1"),
                          parent_run_id="node1")

    # recovery: a different tool, which succeeds
    handler.on_tool_start(run_id="tool2", name="edit_file",
                          inputs={"path": "tests/test_cart.py"}, parent_run_id="node1")
    handler.on_tool_end(run_id="tool2", output="patched 1 file",
                        parent_run_id="node1")

    handler.on_chain_start(run_id="node2", name="verify",
                           parent_run_id="root", inputs={})
    handler.on_tool_start(run_id="tool3", name="bash",
                          inputs={"command": "pytest -x"}, parent_run_id="node2")
    handler.on_tool_end(run_id="tool3", output="3 passed in 0.4s",
                        parent_run_id="node2")
    handler.on_chain_end(run_id="node2", outputs={"passed": True},
                         parent_run_id="root")

    handler.on_chain_end(run_id="node1", outputs={"ok": True},
                         parent_run_id="root")
    handler.on_chain_end(run_id="root", outputs={"answer": "Fixed."},
                         parent_run_id=None)

    print(f"wrote {sink.events_written} events to {path}")
    if sink.write_errors:
        print(f"{sink.write_errors} event(s) rejected by the sink:")
        for v in sink.violations[:5]:
            print(f"  {v}")
        return 1
    print("\nNext:")
    print("  agenttrace index")
    print("  agenttrace serve")
    print(f"  agenttrace adapter test {path} --adapter=langgraph")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
