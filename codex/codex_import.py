#!/usr/bin/env python3
"""agent-trace adapter for OpenAI Codex CLI.

Codex writes a complete session transcript to
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl and nothing else — there is no
per-request API surface to hook. So this adapter works two ways:

1. As a Codex lifecycle hook (`~/.codex/hooks.json` -> Stop / UserPromptSubmit /
   PreToolUse / PostToolUse). On each event it imports the *new* lines of the
   active rollout file since a per-session cursor, so the capture is complete
   but never sits on Codex's critical path.

2. As a standalone importer: `codex_import.py <rollout.jsonl> [...]` to backfill
   traces from sessions that already happened.

Rollout line shapes (verified against real files on this machine):
    {"type":"session_meta",   "payload":{id,cwd,cli_version,model_provider,base_instructions:{text}}}
    {"type":"turn_context",   "payload":{turn_id,cwd,model,approval_policy,...}}
    {"type":"event_msg",      "payload":{type:"task_started",turn_id,started_at,model_context_window}}
    {"type":"event_msg",      "payload":{type:"user_message",message,images}}
    {"type":"event_msg",      "payload":{type:"token_count",info:{...}}}
    {"type":"event_msg",      "payload":{type:"task_complete",turn_id,duration_ms,last_agent_message}}
    {"type":"response_item",  "payload":{type:"message",role,content:[{type,text}]}}
    {"type":"response_item",  "payload":{type:"function_call",name,arguments,call_id}}
    {"type":"response_item",  "payload":{type:"function_call_output",call_id,output}}
    {"type":"compacted",      "payload":{...}}
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Shared trace rules (schema version, timestamps, content flattening, usage
# mapping, truncation, redaction, locked append) live one directory up. This
# file is always executed from the repo — the Codex hook points straight at it
# — so there is no deployed copy to reconcile.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
import agenttrace_common as common  # noqa: E402

AGENT = "codex"
SCHEMA_V = common.SCHEMA_V

# Codex's own names for the shared token fields, in preference order.
CODEX_USAGE_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "cache_read_tokens": ("cached_input_tokens", "cache_read_input_tokens"),
    "reasoning_tokens": ("reasoning_output_tokens", "reasoning_tokens"),
}
API_MODE = "responses"  # Codex dropped wire_api="chat"; this is the only wire format

_state_dir = Path(os.environ.get("AGENTTRACE_STATE_DIR") or (Path.home() / ".codex" / "traces" / ".state"))
_trace_dir = Path(os.environ.get("AGENTTRACE_CODEX_DIR") or (Path.home() / ".codex" / "traces"))


# --------------------------------------------------------------------------
# io helpers
# --------------------------------------------------------------------------

def _now() -> str:
    return common.now_iso()


def _iso(epoch) -> str | None:
    if not isinstance(epoch, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except (ValueError, OSError, OverflowError):
        return None


def _max_chars() -> int:
    return common.max_chars()


def _truncate(v, limit: int | None = None):
    return common.truncate(v, limit)


def _content_to_text(content) -> str:
    return common.flatten_content(content)


def _emit(rec: dict) -> None:
    """Prepend agent, drop None values, append one JSONL line.

    Serialisation, redaction and the locked append are the shared writer's.
    `AGENTTRACE_CAPTURE=metadata` is honoured here rather than sprinkled
    through `parse_rollout`: stripping bodies at the single exit point means a
    future record shape cannot forget to honour it.
    """
    out = {"agent": AGENT}
    out.update(rec)
    if not common.capture_content():
        out = common.strip_content(out)
    common.emit_record(_trace_dir, "codex", out)


# --------------------------------------------------------------------------
# rollout parsing
# --------------------------------------------------------------------------

def _token_info(payload: dict) -> dict | None:
    """Pull usage out of event_msg/token_count (shape varies by codex version)."""
    info = payload.get("info")
    if not isinstance(info, dict):
        info = payload
    last = info.get("last_token_usage") or info.get("total_token_usage") or info
    return common.normalize_usage(last, CODEX_USAGE_ALIASES)


def parse_rollout(path: Path, since_line: int = 0) -> tuple[int, list[dict], dict]:
    """Parse a rollout jsonl from `since_line`.

    Returns (new_line_count, records, meta) where meta carries session facts
    (id, cwd, model, system prompt) discovered anywhere in the file.
    """
    records: list[dict] = []
    meta: dict = {"session_id": None, "cwd": None, "model": None,
                  "system_prompt": None, "cli_version": None, "provider": None}
    turn_id = None
    turn_started_at = None
    pending_tool: dict[str, str] = {}   # call_id -> tool name
    n = 0

    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return since_line, [], meta

    with fh:
        for n, line in enumerate(fh, 1):
            if n <= since_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = obj.get("type")
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")

            if rtype == "session_meta":
                meta["session_id"] = meta["session_id"] or payload.get("id")
                meta["cwd"] = meta["cwd"] or payload.get("cwd")
                meta["cli_version"] = payload.get("cli_version")
                meta["provider"] = payload.get("model_provider")
                bi = payload.get("base_instructions")
                if isinstance(bi, dict) and bi.get("text"):
                    meta["system_prompt"] = bi["text"]
                elif isinstance(bi, str):
                    meta["system_prompt"] = bi
                continue

            if rtype == "turn_context":
                turn_id = payload.get("turn_id") or turn_id
                meta["model"] = payload.get("model") or meta["model"]
                meta["cwd"] = payload.get("cwd") or meta["cwd"]
                continue

            if rtype == "event_msg":
                if ptype == "task_started":
                    turn_id = payload.get("turn_id") or turn_id
                    turn_started_at = payload.get("started_at")
                elif ptype == "user_message":
                    records.append({
                        "event": "user_prompt",
                        "session_id": meta["session_id"],
                        "turn_id": turn_id,
                        "cwd": meta["cwd"],
                        "request": {"messages": [{"role": "user",
                                                  "content": _truncate(payload.get("message"))}]},
                        "source_file": str(path),
                    })
                elif ptype == "token_count":
                    u = _token_info(payload)
                    if u:
                        records.append({
                            "event": "note",
                            "session_id": meta["session_id"],
                            "turn_id": turn_id,
                            "model": meta["model"],
                            "note": "token_count " + json.dumps(u, ensure_ascii=False),
                        })
                elif ptype == "task_complete":
                    dur = payload.get("duration_ms")
                    records.append({
                        "event": "assistant_message",
                        "session_id": meta["session_id"],
                        "turn_id": turn_id,
                        "model": meta["model"],
                        "duration_ms": dur if isinstance(dur, int) else None,
                        "response": {"content": _truncate(payload.get("last_agent_message"))},
                        "source_file": str(path),
                    })
                continue

            if rtype == "response_item":
                if ptype == "message":
                    role = payload.get("role")
                    if role == "developer":
                        # first developer message carries the effective system prompt
                        if not meta["system_prompt"]:
                            meta["system_prompt"] = _content_to_text(payload.get("content"))
                        continue
                    if role == "assistant":
                        records.append({
                            "event": "assistant_message",
                            "session_id": meta["session_id"],
                            "turn_id": turn_id,
                            "model": meta["model"],
                            "ts": _iso(turn_started_at) or None,
                            "response": {"content": _truncate(_content_to_text(payload.get("content"))),
                                         "role": role},
                            "source_file": str(path),
                        })
                elif ptype == "function_call":
                    name = payload.get("name") or "?"
                    cid = payload.get("call_id") or payload.get("id") or ""
                    if cid:
                        pending_tool[cid] = name
                    records.append({
                        "event": "tool_call",
                        "session_id": meta["session_id"],
                        "turn_id": turn_id,
                        "model": meta["model"],
                        "tool": {"name": name, "call_id": cid,
                                 "args": _truncate(payload.get("arguments"))},
                        "source_file": str(path),
                    })
                elif ptype == "function_call_output":
                    cid = payload.get("call_id") or ""
                    records.append({
                        "event": "tool_result",
                        "session_id": meta["session_id"],
                        "turn_id": turn_id,
                        "tool": {"name": pending_tool.get(cid, "?"), "call_id": cid,
                                 "status": "ok"},
                        "response": {"content": _truncate(_content_to_text(payload.get("output")))},
                        "source_file": str(path),
                    })
                continue

    # Codex never logs a wire request, so on the FIRST import of a rollout we
    # synthesise one llm_request carrying the system prompt, which lets the CLI
    # pair request/response. This must happen exactly once per file: on
    # incremental imports the header lines were already skipped, so no
    # llm_request appears in `records` and a "not already present" check would
    # re-emit it on every hook event.
    if since_line == 0 and meta["system_prompt"] and not any(
        r.get("event") == "llm_request" for r in records
    ):
        records.insert(0, {
            "event": "llm_request",
            "session_id": meta["session_id"],
            "turn_id": None,
            "model": meta["model"],
            "provider": meta["provider"],
            "api_mode": API_MODE,
            "cwd": meta["cwd"],
            "request": {
                "messages": [],
                "system_prompt": _truncate(meta["system_prompt"]),
                # No `instructions` here: it was assigned
                # `meta["system_prompt"]` too, so it was a definitionally
                # identical second copy of a multi-KB prompt in every codex
                # record. `api_mode: "responses"` already records that Codex
                # sends it as `instructions`, and every reader checks
                # `system_prompt` first (see canonical_request / the panel's
                # fallback chain), so dropping it loses nothing but bytes.
                "message_count": None,
                "tool_count": None,
            },
            "note": "codex does not log the wire request; the system prompt is the only request-side artifact",
            "source_file": str(path),
        })

    return n, records, meta


# --------------------------------------------------------------------------
# hook entry point
# --------------------------------------------------------------------------

def _cursor_path(path: Path) -> Path:
    """Cursor key is the rollout path, not the session id.

    The hook payload only carries session_id on some events, so keying on
    `session_id or path.stem` wrote two different cursors for one file and the
    second lookup missed, re-importing the whole transcript. A hash of the
    resolved path is stable and identical on every call.
    """
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    return _state_dir / f"cursor-{digest}.json"


def import_rollout(path: Path, session_id: str | None = None) -> int:
    """Import new lines of one rollout file; returns how many records were written."""
    cur = _cursor_path(path)
    since = 0
    if cur.exists():
        try:
            st = json.loads(cur.read_text(encoding="utf-8"))
            if st.get("path") == str(path):
                since = int(st.get("line", 0))
        except (json.JSONDecodeError, ValueError, OSError):
            since = 0
    else:
        since = 0

    n, records, meta = parse_rollout(path, since)
    for r in records:
        _emit(r)

    _state_dir.mkdir(parents=True, exist_ok=True)
    try:
        cur.write_text(json.dumps({"path": str(path), "line": n,
                                   "last": _now(), "session_id": meta.get("session_id")}),
                       encoding="utf-8")
    except OSError:
        pass
    return len(records)


def _newest_rollout(session_id: str | None = None, cwd: str | None = None) -> Path | None:
    root = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "sessions"
    if not root.is_dir():
        return None
    files = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    if session_id:
        for f in files:
            if session_id in f.name:
                return f
    return files[0]


def main() -> int:
    """Two entry modes.

    argv = rollout files  -> explicit backfill; import exactly those files.
    no argv              -> hook mode; read the payload on stdin, import the tail.

    The argv mode used to be gated behind an AGENTTRACE_CODEX_IMPORT env var, so
    the documented backfill command silently did nothing useful: it fell
    through to hook mode and imported whatever rollout happened to be newest.
    """
    args = sys.argv[1:]

    if args:
        total = 0
        missing = []
        for arg in args:
            p = Path(arg).expanduser()
            if p.is_file():
                n = import_rollout(p)
                total += n
                print(f"{n} records from {p}", file=sys.stderr)
            else:
                missing.append(arg)
        if missing:
            print(f"not a file: {', '.join(missing)}", file=sys.stderr)
        if total == 0 and missing:
            return 1
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    session_id = payload.get("session_id") or ""
    transcript = payload.get("transcript_path") or payload.get("rollout_path")
    path = Path(transcript) if transcript and Path(transcript).is_file() else _newest_rollout(session_id)
    if not path:
        return 0

    written = import_rollout(path, session_id or None)
    event = payload.get("hook_event_name") or "?"
    print(f"agent-trace: {event} -> {written} records from {path.name}", file=sys.stderr)

    # Codex hook response contract: Stop is the only event that may return JSON,
    # and it expects an empty/pass-through object. Other events stay silent.
    if event == "Stop":
        json.dump({"continue": True}, sys.stdout)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
