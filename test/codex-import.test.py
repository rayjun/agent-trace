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
    # Two token_count readings in ONE turn. Codex really does this (one per API
    # call), and emitting a record per reading is what collided: same
    # request_id, different payloads.
    {"type": "event_msg", "payload": {
        "type": "token_count",
        "info": {"last_token_usage": {"input_tokens": 1200, "output_tokens": 40,
                                      "cached_input_tokens": 900}}}},
    {"type": "event_msg", "payload": {
        "type": "token_count",
        "info": {"last_token_usage": {"input_tokens": 1400, "output_tokens": 55,
                                      "cached_input_tokens": 1000}}}},
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


#: Real rollouts stamp every line with the session's own clock, and the
#: importer uses it as the record's `ts`. Fixtures must therefore carry one too
#: — without it the importer falls back to the import time and the test cannot
#: tell a correct implementation from one that loses the clock.
_ROLLOUT_DAY = "2026-07-07"


def write_rollout(path, lines, base_second=0):
    with open(path, "a", encoding="utf-8") as f:
        for i, l in enumerate(lines):
            line = dict(l)
            line.setdefault(
                "timestamp",
                f"{_ROLLOUT_DAY}T00:00:{base_second + i:02d}.000Z")
            f.write(json.dumps(line) + "\n")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="codex-dedupe-"))
    state, traces = tmp / "state", tmp / "traces"
    state.mkdir()
    traces.mkdir()
    roll = tmp / "rollout-2026-07-07T00-00-00-sess-test-1.jsonl"
    write_rollout(roll, ROLLOUT)
    sid = "sess-test-1"

    # 1. First import, WITH session_id.
    # 5 records: the turn's llm_request, the user prompt, the turn's
    # llm_response (which carries the token_count usage), and the assistant
    # reply — which Codex logs twice (as a response_item message and again in
    # task_complete.last_agent_message), so both are kept.
    #
    # The per-turn llm_response is new. It exists because `token_count` fires
    # once per API call: hanging every reading on one session-level request_id
    # collided (28 same-id/different-payload events on a real rollout), and
    # emitting nothing lost the usage entirely.
    run(state, traces, {"hook_event_name": "Stop", "session_id": sid,
                        "transcript_path": str(roll)})
    n1 = len(records(traces))
    assert n1 == 5, f"expected 5 records on first import, got {n1}"

    # The pair is closed and carries the usage: this is what makes codex's
    # `token_usage` capability true instead of declared-but-empty.
    t1_req = [r for r in records(traces) if r["event"] == "llm_request"]
    t1_res = [r for r in records(traces) if r["event"] == "llm_response"]
    assert len(t1_req) == 1 and len(t1_res) == 1, (len(t1_req), len(t1_res))
    assert t1_req[0]["request_id"] == t1_res[0]["request_id"], "the pair must join"
    usage = (t1_res[0].get("response") or {}).get("usage") or {}
    assert usage, f"usage missing from the turn's response: {t1_res[0]}"
    # Two readings, ONE record, last value wins — not two colliding records.
    assert usage.get("input_tokens") == 1400, usage
    assert usage.get("cache_read_tokens") == 1000, usage
    assert len(t1_res) == 1, f"one response per turn, got {len(t1_res)}"

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

    # 5. Exactly one llm_request — one per TURN, and t2 has not been logged yet.
    reqs = [r for r in records(traces) if r["event"] == "llm_request"]
    assert len(reqs) == 1, f"expected 1 llm_request, got {len(reqs)}"
    # The system prompt is stored once, on the first turn only.
    assert reqs[0]["request"].get("system_prompt"), "first turn carries the prompt"

    # 6. New turn appended -> only the new lines are imported.
    write_rollout(roll, [
        {"type": "event_msg", "payload": {
            "type": "user_message", "message": "second question", "turn_id": "t2"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "second answer"}]}},
    ], base_second=30)
    run(state, traces, {"hook_event_name": "Stop", "session_id": sid,
                        "transcript_path": str(roll)})
    recs = records(traces)
    prompts = [r for r in recs if r["event"] == "user_prompt"]
    assert len(prompts) == 2, f"incremental turn not imported: {[p['request'] for p in prompts]}"
    # The incremental pass must add exactly ONE new llm_request (for t2) and
    # must not re-emit t1's.
    reqs = [r for r in recs if r["event"] == "llm_request"]
    assert len(reqs) == 2, f"expected one request per turn, got {len(reqs)}"
    assert len({r["request_id"] for r in reqs}) == 2, \
        f"turn requests must have distinct ids: {[r['request_id'] for r in reqs]}"
    assert not reqs[1]["request"].get("system_prompt"), \
        "the system prompt must not be repeated into later turns"
    assert "second question" in json.dumps(recs), "new turn content missing"
    # No id may carry two different payloads: the collision this fixed.
    by_id = {}
    for r in recs:
        rid = r.get("request_id")
        if rid:
            by_id.setdefault(rid, []).append(r)
    assert all(len(v) <= 2 for v in by_id.values()), by_id

    # 7. Deleting the traces must let them be imported again. The cursor
    # claims "lines up to N are already written"; that claim is only true while
    # the file it wrote to exists, and an unvalidated cursor answered "nothing
    # new" forever — deleting your traces was a one-way door.
    for f in traces.glob("*.jsonl"):
        f.unlink()
    assert len(records(traces)) == 0, "traces were not actually deleted"
    run(state, traces, {"hook_event_name": "Stop", "session_id": sid,
                        "transcript_path": str(roll)})
    restored = records(traces)
    assert len(restored) == len(recs), \
        f"cursor did not reset after the trace file was removed: " \
        f"{len(restored)} != {len(recs)}"

    # 8. Every record must carry the ROLLOUT's clock, never the import's.
    # A per-turn llm_request/llm_response pair is created by the importer, and
    # writing `"ts": None` for it made the key EXIST — so the stamping pass
    # skipped it and emit() filled in `now`, scattering import-time records
    # through a trace whose real events were months old (one trace measured 75
    # days).
    all_recs = [json.loads(l) for f in traces.glob("*.jsonl")
                for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    import datetime as _dt
    now_prefix = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT")
    stamped_now = [r for r in all_recs if str(r.get("ts", "")).startswith(now_prefix)]
    assert not stamped_now, \
        f"{len(stamped_now)} record(s) stamped with the import time: " \
        f"{[(r['event'], r['ts']) for r in stamped_now[:3]]}"
    # ...and the rollout's own clock is what they carry instead
    assert all(r["ts"].startswith(_ROLLOUT_DAY) for r in all_recs), \
        f"ts should come from the rollout lines: {sorted({r['ts'][:10] for r in all_recs})}"
    # the incremental append must be stamped from the SECOND batch's lines,
    # which start where the first batch ended
    tail_ts = sorted(r["ts"] for r in all_recs)[-1]
    assert tail_ts >= f"{_ROLLOUT_DAY}T00:00:05", \
        f"later lines must carry later timestamps, got {tail_ts}"

    shutil.rmtree(tmp, ignore_errors=True)
    n_req = len([r for r in recs if r["event"] == "llm_request"])
    print(f"PASS — codex incremental import: no duplicate on repeat/session-less/"
          f"incremental ({n1} -> {len(recs)} records, 1 cursor, "
          f"{n_req} llm_request (one per turn))")


if __name__ == "__main__":
    main()
