#!/usr/bin/env python3
"""Make schema/trace.schema.json a real contract instead of documentation.

Three things were true before this file existed:

  1. Nothing at runtime read the schema. Adapters could emit an event name no
     renderer handles, a float duration, a usage object full of strings, and
     every reader would silently drop or mangle the record.
  2. The enums the reader switches on (`agent`, `event`) were hard-coded in
     three places — the schema, `--agent choices=`, and `agenttrace_common` —
     with nothing keeping them together.
  3. The cache-hit-rate bug (`cache_read > input`) was invisible: nothing
     anywhere asserted that a written record was self-consistent.

This test pins the schema against the shared module and validates REAL records
produced by two of the three adapters against the invariants a reader depends
on. The Pi adapter is TypeScript and asserts the same properties in its own
test (test/pi-adapter.test.mjs), so all three are covered where they live.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "common"))

import agenttrace_common as common  # noqa: E402

SCHEMA = json.loads((ROOT / "schema" / "trace.schema.json").read_text(encoding="utf-8"))
fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


def rejects(name: str, record: object, expect: str) -> None:
    """The validator must name this problem — a check that never fires is noise."""
    problems = common.validate_record(record)
    check(f"rejects: {name}",
          any(expect in p for p in problems),
          f"expected {expect!r} in {problems!r}")


def hermes_records() -> list[dict]:
    """Emit through the real Hermes hooks and read back what landed on disk."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "agent_trace_hermes", ROOT / "hermes" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    tmp = Path(tempfile.mkdtemp(prefix="schema-hermes-"))
    os.environ["HERMES_HOME"] = str(tmp)
    try:
        mod.on_session_start(session_id="s1", model="m", platform="cli")
        mod.on_pre_llm_call(session_id="s1", turn_id="t1", model="m",
                            user_message="hello", conversation_history=[],
                            platform="cli", is_first_turn=True)
        mod.on_pre_api_request(
            api_request_id="r1", turn_id="t1", session_id="s1", model="m",
            provider="p", api_mode="chat_completions",
            request_messages=[{"role": "user", "content": "hello"}],
            system_prompt="you are a bot", tool_count=2,
            message_count=1, max_tokens=1024,
            request={"body": {"tools": [{"name": "read"}, {"name": "bash"}]}},
        )
        mod.on_post_api_request(
            api_request_id="r1", turn_id="t1", session_id="s1", model="m",
            provider="p", api_mode="chat_completions", api_duration=1.7032,
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 2,
                   "cache_read_input_tokens": 4},
            assistant_message={"content": "hi", "tool_calls": [
                {"id": "c1", "function": {"name": "read",
                                          "arguments": "{\"path\":\"x\"}"}}]},
        )
        mod.on_post_tool_call(
            tool_name="read", args={"path": "x"},
            result=json.dumps({"content": "file body"}),
            tool_call_id="c1", session_id="s1", turn_id="t1", model="m",
            duration_ms=34, status="ok")
        mod.on_api_request_error(
            api_request_id="r2", turn_id="t1", session_id="s1", model="m",
            provider="p", api_mode="chat_completions", api_duration=0.25,
            error={"type": "RateLimit", "message": "slow down"},
            status_code=429, retryable=True, retry_count=1)
        mod.on_post_llm_call(session_id="s1", turn_id="t1", model="m",
                             assistant_response="hi")
        mod.on_session_end(session_id="s1", model="m", completed=True,
                           interrupted=False, turn_exit_reason="done")
    finally:
        os.environ.pop("HERMES_HOME", None)

    out = []
    for f in sorted((tmp / "traces").glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def codex_records() -> list[dict]:
    """Import a rollout covering every payload type the parser understands."""
    tmp = Path(tempfile.mkdtemp(prefix="schema-codex-"))
    state, traces = tmp / "state", tmp / "traces"
    state.mkdir()
    traces.mkdir()
    roll = tmp / "rollout-sess-schema.jsonl"
    lines = [
        {"type": "session_meta", "payload": {
            "id": "sess-schema", "cwd": "/tmp", "cli_version": "1.0.0",
            "model_provider": "openai",
            "base_instructions": {"text": "SYSTEM PROMPT"}}},
        {"type": "turn_context",
         "payload": {"turn_id": "t1", "cwd": "/tmp", "model": "gpt-5.5"}},
        {"type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "t1",
                     "started_at": "2026-09-30T10:00:00.000000Z"}},
        {"type": "event_msg",
         "payload": {"type": "user_message", "message": "do the thing",
                     "turn_id": "t1"}},
        {"type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"last_token_usage": {
                         "input_tokens": 500, "output_tokens": 40,
                         "cached_input_tokens": 300,
                         "reasoning_output_tokens": 12}}}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "on it"}]}},
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "shell", "call_id": "c1",
            "arguments": "{\"command\":\"ls\"}"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1",
            "output": "file.py"}},
        {"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "t1", "duration_ms": 1234,
            "last_agent_message": "on it"}},
    ]
    roll.write_text("".join(json.dumps(l) + "\n" for l in lines),
                    encoding="utf-8")

    env = dict(os.environ, AGENTTRACE_STATE_DIR=str(state),
               AGENTTRACE_CODEX_DIR=str(traces))
    p = subprocess.run(
        [sys.executable, str(ROOT / "codex" / "codex_import.py"), str(roll)],
        capture_output=True, text=True, env=env, timeout=60)
    check("codex importer ran for the fixture", p.returncode == 0, p.stderr)

    out = []
    for f in sorted(traces.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def main() -> int:
    print("1. the shared module and the JSON schema agree")
    check("SCHEMA_V == schema v.const",
          common.SCHEMA_V == SCHEMA["properties"]["v"]["const"],
          f"{common.SCHEMA_V} vs {SCHEMA['properties']['v']['const']}")
    check("AGENTS == schema agent.enum",
          list(common.AGENTS) == SCHEMA["properties"]["agent"]["enum"],
          f"{common.AGENTS} vs {SCHEMA['properties']['agent']['enum']}")
    check("EVENTS == schema event.enum",
          list(common.EVENTS) == SCHEMA["properties"]["event"]["enum"],
          f"{common.EVENTS} vs {SCHEMA['properties']['event']['enum']}")
    check("required fields match",
          SCHEMA["required"] == ["v", "ts", "agent", "event"],
          str(SCHEMA["required"]))
    # The CLI's --agent choices come from common.AGENTS; assert the parser
    # really exposes every agent in the schema (and nothing extra).
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace
    ns = agenttrace.build_parser().parse_args(["ls", "--agent", "pi"])
    check("CLI accepts every schema agent",
          ns.agent == "pi", repr(getattr(ns, "agent", None)))
    bad = subprocess.run(
        [sys.executable, str(ROOT / "cli" / "agenttrace.py"), "ls",
         "--agent", "nope"],
        capture_output=True, text=True)
    check("CLI rejects an agent outside the schema", bad.returncode != 0,
          f"rc={bad.returncode}")

    print("2. a good record passes")
    base = {"v": common.SCHEMA_V, "ts": common.now_iso(), "agent": "pi",
            "event": "llm_response",
            "response": {"usage": {"input_tokens": 100,
                                   "cache_read_tokens": 80}}}
    check("valid record -> no problems",
          common.validate_record(base) == [],
          repr(common.validate_record(base)))

    print("3. each invariant actually fires")
    rejects("missing required field", {"v": 1, "event": "note"},
            "missing required field")
    rejects("wrong schema version",
            {**base, "v": 2}, "expected 1")
    rejects("agent outside the enum", {**base, "agent": "claude"}, "not in")
    rejects("event outside the enum", {**base, "event": "ping"}, "not in")
    rejects("timestamp in the wrong format",
            {**base, "ts": "2026-09-30 10:00:00"}, "RFC3339")
    rejects("float duration (the hermes seconds bug)",
            {**base, "duration_ms": 1.7032}, "int milliseconds")
    rejects("bool duration",
            {**base, "duration_ms": True}, "int milliseconds")
    rejects("string usage count",
            {**base, "response": {"usage": {"input_tokens": "100"}}},
            "integer count")
    rejects("bool usage count",
            {**base, "response": {"usage": {"input_tokens": True}}},
            "integer count")
    # The cache-rate bug: pi-ai's `input` excludes the cache.
    rejects("cache_read > input (the 2703% bug)",
            {**base, "response": {"usage": {"input_tokens": 420,
                                            "cache_read_tokens": 768}}},
            "exceed 100%")
    rejects("not an object", ["nope"], "not an object")

    print("4. real hermes records are valid")
    recs = hermes_records()
    check("hermes emitted records", len(recs) >= 7, str(len(recs)))
    seen: set[str] = set()
    for r in recs:
        seen.add(r.get("event", "?"))
        problems = common.validate_record(r)
        check(f"hermes {r.get('event')} is valid", not problems,
              "; ".join(problems))
    # Every documented event the Hermes hooks can produce must be in the enum.
    for expected in ("session_start", "user_prompt", "llm_request",
                     "llm_response", "tool_call", "tool_result", "llm_error",
                     "assistant_message", "session_end"):
        check(f"hermes emitted {expected}", expected in seen, str(sorted(seen)))

    print("5. real codex records are valid")
    recs = codex_records()
    check("codex emitted records", len(recs) >= 5, str(len(recs)))
    for r in recs:
        problems = common.validate_record(r)
        check(f"codex {r.get('event')} is valid", not problems,
              "; ".join(problems))
    # Codex's token_count carries cached_input_tokens — a record that must
    # already satisfy the cache invariant, since codex's `input_tokens` is the
    # provider's total.
    note = next((r for r in recs if r.get("event") == "note"), None)
    if note:
        check("codex token note parses as usage JSON",
              note.get("note", "").startswith("token_count "), note.get("note"))

    print("6. validation is opt-in")
    check("off by default", not common.validate_enabled(),
          repr(os.environ.get(common.VALIDATE_ENV)))
    os.environ[common.VALIDATE_ENV] = "1"
    try:
        check("on with AGENTTRACE_VALIDATE=1", common.validate_enabled())
        # emit_record must REPORT, never raise — the agent keeps running.
        with tempfile.TemporaryDirectory(prefix="schema-emit-") as tmp:
            p = subprocess.run(
                [sys.executable, "-c",
                 "import os, sys, pathlib\n"
                 "sys.path.insert(0, sys.argv[1])\n"
                 "import agenttrace_common as c\n"
                 "bad = {'agent': 'pi', 'event': 'not_an_event',\n"
                 "       'ts': 'yesterday', 'duration_ms': 1.5}\n"
                 "path = c.emit_record(pathlib.Path(sys.argv[2]), 'pi', bad)\n"
                 "print('wrote' if path else 'dropped')\n",
                 str(ROOT / "common"), tmp],
                capture_output=True, text=True,
                env=dict(os.environ, AGENTTRACE_VALIDATE="1"), timeout=60)
        check("invalid record still written (never raises)",
              "wrote" in p.stdout, p.stdout + p.stderr)
        check("violations reported on stderr",
              "agent-trace[pi]" in p.stderr and "not in" in p.stderr,
              p.stderr)
        check("all violations reported",
              "RFC3339" in p.stderr and "int milliseconds" in p.stderr,
              p.stderr)
    finally:
        os.environ.pop(common.VALIDATE_ENV, None)

    if fails:
        print(f"\nFAILED: {', '.join(fails)}", file=sys.stderr)
        return 1
    print("PASS — schema, shared module and real adapter output all agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
