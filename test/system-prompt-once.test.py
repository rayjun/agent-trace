#!/usr/bin/env python3
"""The system prompt is stored exactly once per record.

Why this exists: three adapters disagreed about where the system prompt lives,
and hermes answered "both places".

  hermes  `messages[0]` (role=system) AND a separate `system_prompt` field
  pi      `messages` only                      -> 0/351 duplicated on disk
  codex   `system_prompt` AND `instructions`, always the same string

Measured on this machine: 1812/1813 hermes records carried two byte-identical
copies — 2.4 MB of duplicate prompt inside one profile's 105 MB of traces, and
`agenttrace show` printed the same multi-KB block twice under two headings,
which reads as two different prompts.

The adapters now write one copy. This test pins all three shapes AND, more
importantly, that every reader still finds the prompt whichever shape it
receives — the whole reason the readers have a fallback chain in the first
place is that old records (with the duplicate) and new records (without) must
both render.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli"))
sys.path.insert(0, str(ROOT / "common"))

import agenttrace_common as common  # noqa: E402
import agenttrace  # noqa: E402
import agenttrace_watch  # noqa: E402

SYS = "# Identity\n\nYou are a helpful coding assistant.\n" * 20
fails: list[str] = []


def times(haystack: str, text: str) -> int:
    """How often `text` occurs, matching its JSON-escaped form.

    `SYS` contains real newlines; inside a record or a rendered block it is
    written by json.dumps as the two characters `\n`. Counting the raw string
    would return 0 for a prompt that is plainly present.
    """
    return haystack.count(json.dumps(text, ensure_ascii=False)[1:-1])


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


def load_hermes():
    spec = importlib.util.spec_from_file_location(
        "agent_trace_hermes", ROOT / "hermes" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_records(tmp: Path) -> list[dict]:
    out = []
    for f in sorted((tmp / "traces").glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def show(rec: dict) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        agenttrace.print_full(rec)
    return buf.getvalue()


def panel_lines(rec: dict) -> str:
    """What `agenttrace watch` would draw for this record."""
    return "\n".join(agenttrace_watch.Formatter(width=100).render(rec))


def main() -> int:
    mod = load_hermes()

    print("1. hermes: prompt already in `messages`")
    tmp = Path(tempfile.mkdtemp(prefix="sysprompt-"))
    os.environ["HERMES_HOME"] = str(tmp)
    mod.on_pre_api_request(
        api_request_id="r1", session_id="s1", model="m", provider="p",
        api_mode="chat_completions",
        request_messages=[{"role": "system", "content": SYS},
                          {"role": "user", "content": "hello"}],
        system_prompt=SYS, tool_count=1, message_count=2, max_tokens=1024,
        request={"body": {"tools": [{"name": "read"}]}},
    )
    os.environ.pop("HERMES_HOME", None)
    recs = read_records(tmp)
    check("one record written", len(recs) == 1, str(len(recs)))
    rec = recs[0]
    blob = json.dumps(rec, ensure_ascii=False)
    check("system_prompt field is not a second copy",
          rec["request"].get("system_prompt") is None,
          repr(rec["request"].get("system_prompt"))[:80])
    check("prompt text appears exactly ONCE in the record", times(blob, SYS) == 1,
          f"count={times(blob, SYS)}")
    check("record still validates against the contract",
          common.validate_record(rec) == [], str(common.validate_record(rec)))
    check("the system message itself is intact",
          rec["request"]["messages"][0]["role"] == "system"
          and rec["request"]["messages"][0]["content"] == SYS,
          str(rec["request"]["messages"][0])[:80])

    print("2. hermes: prompt NOT in `messages` — the field is still written")
    tmp2 = Path(tempfile.mkdtemp(prefix="sysprompt2-"))
    os.environ["HERMES_HOME"] = str(tmp2)
    mod.on_pre_api_request(
        api_request_id="r2", session_id="s2", model="m", provider="p",
        api_mode="chat_completions",
        request_messages=[{"role": "user", "content": "hello"}],
        system_prompt=SYS, message_count=1,
    )
    os.environ.pop("HERMES_HOME", None)
    rec2 = read_records(tmp2)[0]
    check("field carries the prompt when messages does not",
          rec2["request"].get("system_prompt") == SYS,
          repr(rec2["request"].get("system_prompt"))[:80])
    check("prompt appears exactly ONCE in the record",
          times(json.dumps(rec2), SYS) == 1,
          f"count={times(json.dumps(rec2), SYS)}")

    print("3. metadata mode drops the prompt entirely")
    tmp3 = Path(tempfile.mkdtemp(prefix="sysprompt3-"))
    os.environ["HERMES_HOME"] = str(tmp3)
    os.environ["AGENTTRACE_CAPTURE"] = "metadata"
    mod.on_pre_api_request(
        api_request_id="r3", session_id="s3", model="m", provider="p",
        api_mode="chat_completions",
        request_messages=[{"role": "system", "content": SYS},
                          {"role": "user", "content": "hello"}],
        system_prompt=SYS, message_count=2,
    )
    os.environ.pop("AGENTTRACE_CAPTURE", None)
    os.environ.pop("HERMES_HOME", None)
    rec3 = read_records(tmp3)[0]
    check("no prompt text anywhere in a metadata record",
          times(json.dumps(rec3), SYS) == 0, repr(rec3.get("request"))[:120])

    print("4. readers still find the prompt in BOTH shapes")
    # Old records (duplicate) and new records (single copy) must both render —
    # that is exactly what the readers' fallback chain is for.
    old_shape = {"v": 1, "ts": "2026-09-30T10:00:00.000Z", "agent": "hermes",
                 "event": "llm_request", "session_id": "s", "model": "m",
                 "request": {"messages": [{"role": "system", "content": SYS},
                                          {"role": "user", "content": "hi"}],
                             "system_prompt": SYS}}
    for label, record in (("old (duplicated)", old_shape),
                          ("new (single copy)", rec),
                          ("field only (codex-like)", rec2)):
        panel = panel_lines(record)
        check(f"panel shows the system prompt — {label}",
              "# Identity" in panel, panel[:300])
        out = show(record)
        check(f"`show` prints it once — {label}", times(out, SYS) <= 1,
              f"count={times(out, SYS)}")
        check(f"`show` prints it at all — {label}", times(out, SYS) == 1,
              f"count={times(out, SYS)}")

    print("5. codex: no more `instructions` clone")
    tmp4 = Path(tempfile.mkdtemp(prefix="sysprompt4-"))
    state, traces = tmp4 / "state", tmp4 / "traces"
    state.mkdir()
    traces.mkdir()
    roll = tmp4 / "rollout-sess-x.jsonl"
    roll.write_text("".join([
        json.dumps({"type": "session_meta", "payload": {
            "id": "sess-x", "cwd": "/tmp", "cli_version": "1.0.0",
            "model_provider": "openai",
            "base_instructions": {"text": SYS}}}) + "\n",
        json.dumps({"type": "turn_context",
                    "payload": {"turn_id": "t1", "model": "gpt-5.5"}}) + "\n",
        json.dumps({"type": "event_msg",
                    "payload": {"type": "user_message", "message": "hi",
                                "turn_id": "t1"}}) + "\n",
    ]), encoding="utf-8")
    env = dict(os.environ, AGENTTRACE_STATE_DIR=str(state),
               AGENTTRACE_CODEX_DIR=str(traces))
    p = subprocess.run(
        [sys.executable, str(ROOT / "codex" / "codex_import.py"), str(roll)],
        capture_output=True, text=True, env=env, timeout=60)
    check("codex importer ran", p.returncode == 0, p.stderr)
    codex_recs = []
    for f in sorted(traces.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                codex_recs.append(json.loads(line))
    codex_req = next((r for r in codex_recs if r.get("event") == "llm_request"),
                     None)
    check("codex emitted a synthetic llm_request", codex_req is not None,
          str([r.get("event") for r in codex_recs]))
    if codex_req:
        blob = json.dumps(codex_req, ensure_ascii=False)
        check("codex prompt appears exactly ONCE", times(blob, SYS) == 1,
              f"count={times(blob, SYS)}")
        check("`instructions` clone removed",
              "instructions" not in codex_req["request"],
              str(list(codex_req["request"])))
        check("prompt still in system_prompt",
              codex_req["request"].get("system_prompt") == SYS, "missing")
        check("codex record validates", common.validate_record(codex_req) == [],
              str(common.validate_record(codex_req)))
        # The readers must still surface it after the field was dropped.
        panel = panel_lines(codex_req)
        check("panel shows the codex system prompt", "# Identity" in panel,
              panel[:300])

    print("6. disk saving, measured")
    single = len(json.dumps(rec, ensure_ascii=False).encode())
    duplicated = len(json.dumps(old_shape, ensure_ascii=False).encode())
    check("new record is smaller than the duplicated shape",
          single < duplicated, f"{single} vs {duplicated}")
    print(f"       one record: {duplicated:,} -> {single:,} bytes "
          f"({duplicated - single:,} saved, ×1812 on disk)")

    if fails:
        print(f"\nFAILED: {', '.join(fails)}", file=sys.stderr)
        return 1
    print("PASS — the system prompt is stored once, and every reader still "
          "finds it in all four shapes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
