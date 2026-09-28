#!/usr/bin/env python3
"""Regression tests for the Codex adapter's incremental import.

Covers two bugs that made a live Codex session write duplicate records:
  1. The cursor was keyed on `session_id or path.stem`. Hook payloads only
     carry session_id on some events, so the same rollout got two cursor files
     and the second lookup missed.
  2. The synthetic llm_request was guarded by "not already in records", which
     is always true on an incremental import because the header was skipped.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPORTER = ROOT / "codex" / "codex_import.py"

ROLLOUT = [
    {"type": "session_meta", "payload": {
        "id": "sess-test-1", "cwd": "/tmp", "cli_version": "1.2.3",
        "model_provider": "openai",
        "base_instructions": {"text": "SYSTEM " * 200}}},
    {"type": "turn_context", "payload": {
        "turn_id": "t1", "cwd": "/tmp", "model": "gpt-5.5"}},
    {"type": "event_msg", "payload": {
        "type": "user_message", "message": "hello there", "turn_id": "t1"}},
    {"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "hi back"}]}},
    {"type": "event_msg", "payload": {
        "type": "task_complete", "turn_id": "t1", "duration_ms": 1234,
        "last_agent_message": "hi back"}},
]


def run(state, traces, payload):
    env = dict(os.environ, AGENTTRACE_STATE_DIR=str(state), AGENTTRACE_CODEX_DIR=str(traces))
    p = subprocess.run([sys.executable, str(IMPORTER)], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, f"importer failed: {p.stderr}"
    return p.stderr


def records(traces):
    out = []
    for f in sorted(Path(traces).glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def write_rollout(path, lines):
    with open(path, "a", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="codex-dedupe-"))
    state, traces = tmp / "state", tmp / "traces"
    state.mkdir()
    traces.mkdir()
    roll = tmp / "rollout-2026-07-07T00-00-00-sess-test-1.jsonl"
    write_rollout(roll, ROLLOUT)
    sid = "sess-test-1"

    # 1. First import, WITH session_id.
    # 4 records: the synthetic llm_request, the user prompt, and the assistant
    # reply — which Codex logs twice (as a response_item message and again in
    # task_complete.last_agent_message), so both are kept.
    run(state, traces, {"hook_event_name": "Stop", "session_id": sid,
                        "transcript_path": str(roll)})
    n1 = len(records(traces))
    assert n1 == 4, f"expected 4 records on first import, got {n1}"

    # 2. Same file, NO session_id — used to miss the cursor entirely.
    run(state, traces, {"hook_event_name": "Stop", "transcript_path": str(roll)})
    n2 = len(records(traces))
    assert n2 == n1, f"re-imported without session_id: {n1} -> {n2}"

    # 3. Repeated Stop events must never grow the trace.
    for ev in ("Stop", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
        run(state, traces, {"hook_event_name": ev, "session_id": sid,
                            "transcript_path": str(roll)})
    n3 = len(records(traces))
    assert n3 == n1, f"repeat events duplicated: {n1} -> {n3}"

    # 4. Exactly one cursor file for the rollout.
    cursors = list(state.glob("cursor-*.json"))
    assert len(cursors) == 1, f"expected 1 cursor file, got {[c.name for c in cursors]}"

    # 5. Exactly one synthetic llm_request.
    reqs = [r for r in records(traces) if r["event"] == "llm_request"]
    assert len(reqs) == 1, f"expected 1 llm_request, got {len(reqs)}"

    # 6. New turn appended -> only the new lines are imported.
    write_rollout(roll, [
        {"type": "event_msg", "payload": {
            "type": "user_message", "message": "second question", "turn_id": "t2"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "second answer"}]}},
    ])
    run(state, traces, {"hook_event_name": "Stop", "session_id": sid,
                        "transcript_path": str(roll)})
    recs = records(traces)
    prompts = [r for r in recs if r["event"] == "user_prompt"]
    assert len(prompts) == 2, f"incremental turn not imported: {[p['request'] for p in prompts]}"
    # The synthetic request must still appear exactly once after the incremental pass.
    reqs = [r for r in recs if r["event"] == "llm_request"]
    assert len(reqs) == 1, f"llm_request duplicated on incremental import: {len(reqs)}"
    assert "second question" in json.dumps(recs), "new turn content missing"

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"PASS — codex incremental import: no duplicate on repeat/session-less/"
          f"incremental ({n1} -> {len(recs)} records, 1 cursor, 1 llm_request)")


if __name__ == "__main__":
    main()
