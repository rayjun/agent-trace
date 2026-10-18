#!/usr/bin/env python3
"""Pi backfill importer (pi/pi_import.py).

The importer exists because the live adapter can only see turns that happen
while it is loaded: 53 of 63 sessions on the machine that motivated it had no
traces at all. What is pinned here is what makes the recovery trustworthy:

  * the transcript is read in CONVERSATION order (active parent chain), not file
    order, and the two differ when a session has abandoned branches;
  * usage follows the shared contract — Pi reports `input` EXCLUDING cache
    reads, and dropping the correction re-breaks the >100% cache hit rate;
  * the request side is rebuilt from the transcript AND SAID SO, because Pi
    does not persist the wire payload it sent;
  * re-running is idempotent, and a session that already has live traces is
    skipped rather than double-counted;
  * the shared writer's redaction and metadata mode still apply.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pi"))

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


# --------------------------------------------------------------------------
# a synthetic transcript with the shapes Pi actually writes (session version 3)
# --------------------------------------------------------------------------

def entry(eid: str, parent: str | None, role: str, content, ts: str, **msg) -> dict:
    body = {"role": role, "content": content, "timestamp": 1792226301000}
    body.update(msg)
    return {"id": eid, "parentId": parent, "timestamp": ts, "type": "message",
            "message": body}


USAGE = {"input": 1000, "output": 40, "cacheRead": 800, "cacheWrite": 200,
         "reasoning": 15, "totalTokens": 2040}

CHAIN = [
    {"type": "session", "version": 3, "id": "sess-1",
     "timestamp": "2026-07-07T00:00:00.000Z", "cwd": "/tmp/proj"},
    entry("e0", None, "system", [{"type": "text", "text": "You are Pi."}],
          "2026-07-07T00:00:01.000Z", sections=["tools"]),
    entry("e1", "e0", "user", [{"type": "text", "text": "run the tests"}],
          "2026-07-07T00:00:02.000Z"),
    entry("e2", "e1", "assistant", [
        {"type": "thinking", "thinking": "I should run pytest."},
        {"type": "text", "text": "Running them."},
        {"type": "toolCall", "id": "bash:0", "name": "bash",
         "arguments": {"command": "pytest -x"}},
    ], "2026-07-07T00:00:03.000Z", model="kimi-k3", provider="moonshot",
        api="openai-completions", durationMs=2500, responseId="resp-1",
        stopReason="toolUse", usage=USAGE),
    # A FORK: x1 also descends from e2, but the session continued on e3/e4.
    # Pi appends the branch it is actually on, so the file's LAST entry is the
    # active leaf and x1 is the off-chain one. Reading the file linearly would
    # import x1 and lose the real ending.
    entry("x1", "e2", "user", [{"type": "text", "text": "ABANDONED BRANCH"}],
          "2026-07-07T00:00:04.000Z"),
    entry("e3", "e2", "toolResult", [
        {"type": "text", "text": "3 passed\nProcess exited with code 0"}],
        "2026-07-07T00:00:05.000Z", toolCallId="bash:0", toolName="bash",
        isError=False, durationMs=1200),
    entry("e4", "e3", "assistant", [{"type": "text", "text": "All green."}],
          "2026-07-07T00:00:06.000Z", model="kimi-k3", provider="moonshot",
          api="openai-completions", durationMs=900, responseId="resp-2",
          stopReason="stop", usage=USAGE),
]


def write_session(path: Path, chain: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(c) for c in chain) + "\n", encoding="utf-8")


def run_import(sessions: Path, traces: Path, *args: str,
               capture: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, AGENTTRACE_PI_SESSIONS=str(sessions),
               AGENTTRACE_PI_DIR=str(traces),
               AGENTTRACE_STATE_DIR=str(traces / ".state"))
    if capture:
        env["AGENTTRACE_CAPTURE"] = capture
    return subprocess.run([sys.executable, str(ROOT / "pi" / "pi_import.py"), *args],
                          capture_output=True, text=True, env=env, timeout=120)


def records(traces: Path) -> list[dict]:
    return [json.loads(l) for f in sorted(traces.glob("*.jsonl"))
            for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


tmp = Path(tempfile.mkdtemp(prefix="pi-import-"))
sessions, traces = tmp / "sessions", tmp / "traces"
sess_file = sessions / "--tmp-proj--" / "2026-07-07T00-00-00-000Z_sess-1.jsonl"
write_session(sess_file, CHAIN)

# ---- 1. explicit-file backfill -------------------------------------------
p = run_import(sessions, traces, str(sess_file))
check("importing one session exits 0", p.returncode == 0, p.stderr[-300:])
recs = records(traces)
check("records were written", len(recs) > 0, str(len(recs)))

by = {}
for r in recs:
    by.setdefault(r["event"], []).append(r)

check("session_start and session_end bracket the session",
      len(by.get("session_start", [])) == 1 and len(by.get("session_end", [])) == 1)
check("one llm_request/llm_response pair per assistant message",
      len(by.get("llm_request", [])) == 2 and len(by.get("llm_response", [])) == 2,
      f"{len(by.get('llm_request', []))}/{len(by.get('llm_response', []))}")
check("each pair joins on request_id",
      {r["request_id"] for r in by["llm_request"]}
      == {r["request_id"] for r in by["llm_response"]})
check("request ids are namespaced so they cannot collide with live ones",
      all(r["request_id"].startswith("pi-bf-") for r in by["llm_request"]),
      str([r["request_id"] for r in by["llm_request"]]))
check("backfill provenance is recorded, not implied",
      all("backfill=session" in (r.get("note") or "") for r in recs if r["event"] != "session_start"
          and r["event"] != "session_end"),
      str([r.get("note") for r in recs[:3]]))

# ---- 2. turn inference: one turn per user message -------------------------
# The turn id carries a `bf-` prefix: the live adapter numbers turns by provider
# turn and the backfill by user message, so both start at 0 for one session and
# they met in a single trace_id when unnamespaced.
check("the two assistant replies landed in ONE turn (one user message)",
      {r["turn_id"] for r in by["llm_request"]} == {"bf-0"},
      str([r["turn_id"] for r in by["llm_request"]]))
check("backfilled turns live in their own trace namespace",
      all(str(r["turn_id"]).startswith("bf-") for r in recs
          if r.get("turn_id")),
      str(sorted({str(r.get("turn_id")) for r in recs if r.get("turn_id")})[:5]))
check("...so a partly-live session cannot collide with its own live turns",
      not any(str(r["turn_id"]).isdigit() for r in recs if r.get("turn_id")))
check("the off-chain branch is not imported (the active leaf wins)",
      "ABANDONED BRANCH" not in json.dumps(recs),
      json.dumps(recs)[:200])

# ---- 3. the request side is rebuilt from the transcript, and says so -------
first_req = by["llm_request"][0]["request"]
check("the system prompt is carried on the request",
      first_req.get("system_prompt") == "You are Pi.", str(first_req.get("system_prompt")))
check("the request holds the conversation up to that message",
      [m["role"] for m in first_req["messages"]] == ["user"],
      str([m["role"] for m in first_req["messages"]]))
check("the second request includes the assistant turn before it",
      [m["role"] for m in by["llm_request"][1]["request"]["messages"]]
      == ["user", "assistant", "toolResult"],
      str([m["role"] for m in by["llm_request"][1]["request"]["messages"]]))
check("the request records that it was reconstructed",
      "rebuilt from the transcript" in by["llm_request"][0]["note"])

# ---- 4. usage follows the shared contract --------------------------------
u = by["llm_response"][0]["response"]["usage"]
check("input_tokens includes the cache (the shared convention)",
      u["input_tokens"] == 1000 + 800 + 200, str(u))
check("cache_read_tokens is a subset of input_tokens",
      u["cache_read_tokens"] == 800 and u["cache_read_tokens"] <= u["input_tokens"])
check("cache_write and reasoning survive",
      u["cache_write_tokens"] == 200 and u["reasoning_tokens"] == 15, str(u))
check("duration is milliseconds, unscaled",
      by["llm_response"][0]["duration_ms"] == 2500,
      str(by["llm_response"][0]["duration_ms"]))
check("the provider's own response id is kept for cross-referencing",
      "response_id=resp-1" in by["llm_response"][0]["note"],
      str(by["llm_response"][0]["note"]))
check("the model and wire protocol are carried",
      by["llm_response"][0]["model"] == "kimi-k3"
      and by["llm_response"][0]["api_mode"] == "openai-completions")
check("thinking is kept apart from the answer",
      "pytest" in by["llm_response"][0]["response"]["reasoning"]
      and by["llm_response"][0]["response"]["content"] == "Running them.",
      json.dumps(by["llm_response"][0]["response"])[:200])
check("the tool call the model asked for is on the response",
      by["llm_response"][0]["response"]["tool_calls"][0]["name"] == "bash")

# ---- 5. tool pairing ------------------------------------------------------
check("tool_call and tool_result were both emitted",
      len(by.get("tool_call", [])) == 1 and len(by.get("tool_result", [])) == 1)
# The pairing id is SCOPED by the request that produced the call, because a
# provider's tool-call ids are unique only within one API response: the same id
# appearing twice in a turn gave the v2 tree two spans sharing one id.
scoped = by["tool_call"][0]["tool"]["call_id"]
check("both halves carry the SAME scoped call id",
      scoped == by["tool_result"][0]["tool"]["call_id"] and scoped.endswith(":bash:0"),
      f"call={scoped!r} result={by['tool_result'][0]['tool']['call_id']!r}")
check("...and the provider's own id is preserved beside it",
      by["tool_call"][0]["tool"]["provider_call_id"] == "bash:0")
check("the tool's ARGUMENTS are on the tool_call (what ran, not just that it ran)",
      by["tool_call"][0]["tool"]["args"] == {"command": "pytest -x"},
      str(by["tool_call"][0]["tool"]))
check("the tool result body is captured",
      "3 passed" in by["tool_result"][0]["response"]["content"],
      str(by["tool_result"][0].get("response")))
check("the shell exit code is parsed out of the result text",
      by["tool_result"][0]["tool"]["exit_code"] == 0,
      str(by["tool_result"][0]["tool"]))
check("the tool span starts at the message that requested it",
      by["tool_call"][0]["ts"] < by["tool_result"][0]["ts"],
      f"{by['tool_call'][0]['ts']} vs {by['tool_result'][0]['ts']}")

# ---- 6. timestamps come from the transcript, never from the import --------
from datetime import datetime, timezone  # noqa: E402
today = datetime.now(timezone.utc).strftime("%Y-%m-%dT")
check("every record is stamped from the transcript's own clock",
      all(r["ts"].startswith("2026-07-07") for r in recs),
      str(sorted({r["ts"][:10] for r in recs})))
check("...and none from the import time", not any(r["ts"].startswith(today) for r in recs))

# ---- 7. idempotency -------------------------------------------------------
before = len(records(traces))
p = run_import(sessions, traces, "--force", str(sess_file))
after = len(records(traces))
check("--force does not rewrite records that are already on disk",
      after == before, f"{before} -> {after}")
p = run_import(sessions, traces, "--reimport", str(sess_file))
check("--reimport is the flag that rewrites everything",
      len(records(traces)) > after, f"{after} -> {len(records(traces))}")

# ---- 8. a session that already has live traces is skipped ------------------
# A dedicated session, never backfilled, so the skip and its override are the
# only thing under test — reusing one that the cursor already covers would make
# both runs no-ops and the assertion vacuous.
partial = sessions / "--tmp-proj--" / "2026-07-09T00-00-00-000Z_sess-3.jsonl"
write_session(partial, [
    {"type": "session", "version": 3, "id": "sess-3",
     "timestamp": "2026-07-09T00:00:00.000Z", "cwd": "/tmp/proj"},
    entry("g1", None, "user", [{"type": "text", "text": "hi"}],
          "2026-07-09T00:00:01.000Z"),
    entry("g2", "g1", "assistant", [{"type": "text", "text": "hello"}],
          "2026-07-09T00:00:02.000Z", model="m", usage=USAGE),
])
# ...and the live adapter already wrote one record for it
(traces / "pi-live.jsonl").write_text(
    json.dumps({"v": 1, "ts": "2026-07-09T00:00:01.500Z", "agent": "pi",
                "event": "llm_request",
                # resolved, the way the importer canonicalises it: on macOS a
                # temp path under /var resolves to /private/var, and comparing
                # the raw strings would call one session two
                "session_id": str(partial.resolve()),
                "request_id": "pi-0-1"}) + "\n", encoding="utf-8")

before = len(records(traces))
p = run_import(sessions, traces, str(partial))
check("a session with live traces is skipped, not double-counted",
      "already-traced=1" in p.stderr and len(records(traces)) == before,
      p.stderr[-200:])
p = run_import(sessions, traces, "--force", str(partial))
check("--force overrides the live-traced skip (partly-live sessions)",
      "imported=1" in p.stderr and len(records(traces)) > before,
      p.stderr[-200:])
check("...and the forced import still wrote its records exactly once",
      sum(1 for r in records(traces)
          if r.get("event") == "llm_response"
          and r.get("session_id") == str(partial.resolve())) == 1,
      str([r.get("session_id") for r in records(traces)
           if r.get("event") == "llm_response"][:3]))
p = run_import(sessions, traces, "-x-scan")  # invalid path -> error path
check("a bad explicit path is reported", p.returncode == 1 and "not a file" in p.stderr,
      p.stderr[-160:])

# ---- 9. metadata capture mode still blanks bodies -------------------------
meta_traces = tmp / "traces-metadata"
p = run_import(sessions, meta_traces, "--force", str(sess_file), capture="metadata")
mrecs = records(meta_traces)
mresp = [r for r in mrecs if r.get("event") == "llm_response"]
check("metadata mode keeps the record but drops the bodies",
      bool(mresp) and mresp[0]["response"].get("content") in (None, "")
      and mresp[0]["response"]["usage"],
      json.dumps(mresp[0].get("response"))[:200] if mresp else "no response")

# ---- 10. redaction still applies (shared writer) --------------------------
leak = sessions / "--tmp-proj--" / "2026-07-08T00-00-00-000Z_sess-2.jsonl"
write_session(leak, [
    {"type": "session", "version": 3, "id": "sess-2",
     "timestamp": "2026-07-08T00:00:00.000Z", "cwd": "/tmp/proj"},
    entry("f1", None, "user", [{"type": "text", "text": "use key sk-ABCDEFGHIJKLMNOPQRSTUV"}],
          "2026-07-08T00:00:01.000Z"),
    entry("f2", "f1", "assistant", [{"type": "text", "text": "ok"}],
          "2026-07-08T00:00:02.000Z", model="m", usage=USAGE),
])
red_traces = tmp / "traces-redact"
run_import(sessions, red_traces, str(leak))
dump = json.dumps(records(red_traces))
# The v1 writer's marker is its own (`sk-***`), not the v2 sink's `[REDACTED]`:
# this path goes through common.emit_record, and asserting the wrong marker
# would have made the test fail on a correctly-redacted record.
check("a credential in the transcript is redacted on the way to disk",
      "sk-ABCDEFGHIJKLMNOPQRSTUV" not in dump and "sk-***" in dump,
      dump[max(0, dump.find("sk-") - 40):dump.find("sk-") + 40] if "sk-" in dump else "no sk-")

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("pi backfill OK")
