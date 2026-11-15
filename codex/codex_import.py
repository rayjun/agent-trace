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
import tempfile
from contextlib import contextmanager
from typing import Any
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


def _emit(rec: dict) -> Path | None:
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
    return common.emit_record(_trace_dir, "codex", out)


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


def parse_rollout(path: Path, since_line: int = 0, *,
                  live: bool = False) -> tuple[int, list[dict], dict]:
    """Parse a rollout jsonl from `since_line`.

    Returns (new_line_count, records, meta) where meta carries session facts
    (id, cwd, model, system prompt) discovered anywhere in the file.
    """
    records: list[dict] = []
    meta: dict = {"session_id": None, "cwd": None, "model": None,
                  "system_prompt": None, "cli_version": None, "provider": None}
    turn_id = None
    pending_tool: dict[str, str] = {}   # call_id -> tool name
    n = 0

    # ---- turn bookkeeping -------------------------------------------------
    # Codex logs no wire request, so the request/response PAIR for a turn has
    # to be reconstructed. It must be per TURN, not per session:
    # `token_count` fires once per API call and carries that call's usage, and
    # hanging every one of them on a single session-level request_id produced
    # whichever came last — 28 same-id/different-payload collisions on one real
    # rollout. A per-turn pair gives each turn a closed llm span with its own
    # usage, which is also what the v2 tree wants.
    turn_seq = 0
    turn_key = None
    turn_usage: dict[str, dict] = {}      # turn key -> merged usage
    #: turn key -> the newest line timestamp seen for it. A turn that never
    #: reported `task_complete` used to be closed at the END OF THE FILE, so a
    #: resumed rollout gave it a duration of days and the waterfall showed an
    #: 18-hour "model call". Closing at the turn's own last event bounds it to
    #: what was actually observed.
    turn_last_ts: dict[str, str | None] = {}
    turn_request: dict[str, str] = {}     # turn key -> request_id
    turn_emitted: set[str] = set()
    closed_turns: set[str] = set()

    def note_turn_ts(key: str | None, ts: str | None) -> None:
        if key and ts:
            turn_last_ts[key] = ts

    def open_turn(key: str | None, model, cwd, ts) -> str:
        """Register a turn and emit its llm_request exactly once."""
        nonlocal turn_seq
        k = key or f"turn{turn_seq}"
        if k in turn_emitted:
            return k
        turn_seq += 1
        turn_emitted.add(k)
        request_id = f"codex-{k}"
        turn_request[k] = request_id
        # The system prompt is a session-level artifact and is stored ONCE, on
        # the first turn's request: repeating a multi-KB prompt in every turn
        # would duplicate it on disk for no gain (see the "stored once" rule in
        # schema/CHANGELOG.md). Later turns therefore carry an empty request
        # body — honest, because Codex does not log one.
        first = len(turn_request) == 1
        records.append({
            "event": "llm_request",
            "session_id": meta["session_id"],
            "turn_id": key,
            "request_id": request_id,
            "model": model or meta["model"],
            "provider": meta["provider"],
            "api_mode": API_MODE,
            "cwd": cwd or meta["cwd"],
            **({"resource": _workspace(cwd or meta["cwd"])} if
               (cwd or meta["cwd"]) else {}),
            **({"ts": ts} if ts else {}),
            "request": {
                "messages": [],
                "system_prompt": _truncate(meta["system_prompt"]) if first else None,
                "message_count": None,
                "tool_count": None,
            },
            "note": ("codex does not log the wire request; the system prompt is "
                     "the only request-side artifact" if first else
                     "codex does not log the wire request"),
            "source_file": str(path),
        })
        return k

    def close_turn(key: str, model, content, ts, duration_ms=None,
                   measured: bool = True) -> bool:
        """Emit the turn's llm_response: usage + whatever text is observable.

        Returns True when the turn carried usage, so the caller knows whether
        there is anything to say about an unclosed turn.
        """
        request_id = turn_request.get(key)
        if request_id is None or key in closed_turns:
            return False
        closed_turns.add(key)
        response: dict = {}
        if content:
            response["content"] = _truncate(content)
        usage = turn_usage.pop(key, None)
        if usage:
            response["usage"] = usage
        records.append({
            "event": "llm_response",
            "session_id": meta["session_id"],
            "turn_id": key,
            "request_id": request_id,
            "model": model or meta["model"],
            **({"ts": ts} if ts else {}),
            # Codex measures the TURN, not the call, so this is NOT written as
            # the span's `duration_ms`: doing that reported one turn's duration
            # as every call's latency, and a turn with two calls then counted
            # twice as much work as it contained. It travels under its own name
            # so a reader can see codex's own number without it being mistaken
            # for a model latency.
            **({"turn_duration_ms": duration_ms} if measured and duration_ms else {}),
            "note": ("usage_only=true" if content is None
                     else ("turn_incomplete=true (no task_complete; duration is "
                           "the log gap, not a measured latency)" if not measured
                           else None)),
            "response": response,
            "source_file": str(path),
        })
        return bool(usage)

    try:
        fh = path.open("rb")
    except OSError:
        if live:
            raise
        return since_line, [], meta

    # Every rollout line carries the REAL event time (`timestamp`, ISO). Using
    # it is what makes a turn's duration meaningful; the previous behaviour
    # stamped each record with the time WE imported it, so a trace held records
    # from both clocks and its task span measured weeks instead of seconds.
    # Stamping happens for the PREVIOUS line at the top of each iteration,
    # because the body uses `continue` extensively and any per-branch stamping
    # would have to be repeated at ten call sites (and missed at the eleventh).
    mark = len(records)
    pending_ts: str | None = None
    source_lines: list[int] = []

    def stamp_records(source_line: int) -> None:
        nonlocal mark
        for rec in records[mark:]:
            if pending_ts and not rec.get("ts"):
                rec["ts"] = pending_ts
        source_lines.extend([source_line] * (len(records) - mark))
        mark = len(records)

    with fh:
        # Reconstruct context from the prefix, but emit only records belonging
        # to new complete lines. Skipping the prefix lost session/model/tool
        # identity whenever a live tail started in the middle of a turn.
        size = os.fstat(fh.fileno()).st_size
        while fh.tell() < size:
            raw = fh.readline(size - fh.tell())
            if not raw or (live and not raw.endswith(b"\n")):
                break
            stamp_records(n)
            n += 1
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw_ts = obj.get("timestamp")
            pending_ts = raw_ts if isinstance(raw_ts, str) and raw_ts.endswith("Z") else None
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
                meta["session_ts"] = pending_ts
                if live:
                    records.append({"event": "session_start", "session_id": meta["session_id"],
                                    "cwd": meta["cwd"], "agent_version": meta["cli_version"],
                                    "source_file": str(path)})
                continue

            if rtype == "turn_context":
                tid = payload.get("turn_id")
                if tid and tid != turn_id:
                    turn_id = tid
                    turn_key = None          # a new turn: open it below
                meta["model"] = payload.get("model") or meta["model"]
                meta["cwd"] = payload.get("cwd") or meta["cwd"]
                if turn_key is None:
                    turn_key = open_turn(turn_id, meta["model"], meta["cwd"], None)
                continue

            if rtype == "event_msg":
                if ptype == "task_started":
                    new_turn = payload.get("turn_id")
                    if new_turn and new_turn != turn_id:
                        turn_id = new_turn
                    turn_key = open_turn(turn_id, meta["model"], meta["cwd"], None)
                elif ptype == "user_message":
                    # Any record naming a NEW turn id opens that turn. A rollout
                    # may log turn_context, task_started, or neither for a given
                    # turn (the incremental append in the test carries only
                    # user_message), and the pair must not depend on which
                    # boundary line this Codex version happened to write.
                    if payload.get("turn_id") and payload["turn_id"] != turn_id:
                        turn_id = payload["turn_id"]
                        turn_key = open_turn(turn_id, meta["model"], meta["cwd"],
                                             None)
                    elif turn_key is None:
                        turn_key = open_turn(turn_id, meta["model"], meta["cwd"],
                                             None)
                    note_turn_ts(turn_key, pending_ts)
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
                        # Accumulated, not emitted. token_count fires once per
                        # API call; each reading belongs to THIS turn's llm
                        # response, which close_turn emits once. Emitting one
                        # record per reading is what produced duplicate
                        # request_ids with different payloads on real data.
                        key = turn_key or (turn_id or "turn0")
                        merged = dict(turn_usage.get(key) or {})
                        merged.update({k: v for k, v in u.items()
                                       if v is not None})
                        turn_usage[key] = merged
                elif ptype == "task_complete":
                    dur = payload.get("duration_ms")
                    last_message = payload.get("last_agent_message")
                    note_turn_ts(turn_key, pending_ts)
                    records.append({
                        "event": "assistant_message",
                        "session_id": meta["session_id"],
                        "turn_id": turn_id,
                        "model": meta["model"],
                        "duration_ms": dur if isinstance(dur, int) else None,
                        "response": {"content": _truncate(last_message)},
                        "source_file": str(path),
                    })
                    # The turn's llm_response is emitted HERE, carrying the
                    # usage collected from its token_count events and the text
                    # Codex reported as this turn's output. Without this the
                    # usage had nowhere to live that a reader could reach.
                    close_turn(turn_key or (turn_id or "turn0"), meta["model"],
                               last_message, None,
                               duration_ms=dur if isinstance(dur, int) else None)
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

    stamp_records(n)

    # Flush any turn that collected usage but never saw task_complete (the
    # rollout ended mid-turn: a crash, `SIGINT`, or the file still being
    # written). Dropping it would lose real token data; pretending the turn
    # completed would be worse. So:
    #   * the llm_response is emitted — the provider DID return, and
    #     `token_count` is the evidence, so closing the llm SPAN is correct;
    #   * a `note` records that the TURN never reported completion, which is
    #     the part that is genuinely unknown.
    for key in ([] if live else sorted(set(turn_usage) | set(turn_request))):
        # close_turn owns the usage pop; popping here too silently emptied the
        # response it then emitted.
        # `measured=False`: this turn's end is the file's end, not the call's
        # return, so no duration is claimed.
        if not close_turn(key, meta["model"], None,
                          turn_last_ts.get(key) or pending_ts, measured=False):
            continue
        records.append({
            "event": "note",
            "session_id": meta["session_id"],
            "turn_id": key,
            "model": meta["model"],
            **({"ts": pending_ts} if pending_ts else {}),
            "note": "turn_incomplete=true (no task_complete in the rollout)",
            "source_file": str(path),
        })

    # A session with no turn boundaries at all still gets one request record so
    # the system prompt is not lost; it is left unclosed, which the
    # materializer reports as `incomplete` rather than pretending the turn
    # finished.
    if not live and since_line == 0 and meta["system_prompt"] and not turn_emitted:
        open_turn(None, meta["model"], meta["cwd"], pending_ts)

    stamp_records(n)
    records = [rec for rec, line in zip(records, source_lines, strict=True) if line > since_line]
    return n, records, meta


# --------------------------------------------------------------------------
# hook entry point
# --------------------------------------------------------------------------

_workspace_cache: dict[str, dict[str, Any] | None] = {}


def _workspace(cwd: str | None) -> dict | None:
    """The repository the session ran in, captured once per importer run.

    The rollout records the cwd but nothing about version control, so this is
    read from the directory as it is NOW — which is why the snapshot is labelled
    `at: index` unless the file it came from was written today. A codex rollout
    can be months old and a diff of it would be a fiction.
    """
    if not cwd:
        return None
    if cwd in _workspace_cache:
        return _workspace_cache[cwd]
    # Always `at: index`: a rollout is a transcript we are re-reading, so any
    # repository state read now describes the directory as it is, not as the
    # session found it. Claiming "capture" would let a months-old trace look
    # like it ran against today's branch.
    snap = common.workspace_snapshot(cwd, at="index") if cwd else None
    _workspace_cache[cwd] = snap
    return snap


def _cursor_path(path: Path) -> Path:
    """Cursor key is the rollout path, not the session id.

    The hook payload only carries session_id on some events, so keying on
    `session_id or path.stem` wrote two different cursors for one file and the
    second lookup missed, re-importing the whole transcript. A hash of the
    resolved path is stable and identical on every call.
    """
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    return _state_dir / f"cursor-{digest}.json"


def _today_trace_file() -> Path:
    """The daily file the next emit would append to (matches emit_record)."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _trace_dir / f"codex-{day}.jsonl"


@contextmanager
def _import_lock(path: Path):
    """Serialize the hook and TUI poller before either reads its cursor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            import fcntl
        except ImportError:
            yield
            return
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def import_rollout(path: Path, session_id: str | None = None, *,
                   live: bool = False) -> int:
    """Import new lines of one rollout file; returns how many records were written."""
    path = path.expanduser().resolve()
    cur = _cursor_path(path)
    with _import_lock(cur.with_suffix(".lock")):
        return _import_rollout(path, cur, live=live)


def _import_rollout(path: Path, cur: Path, *, live: bool = False) -> int:
    since = 0
    stat = path.stat()
    output_file = _today_trace_file()
    session_started = False
    if cur.exists():
        try:
            st = json.loads(cur.read_text(encoding="utf-8"))
            if st.get("path") == str(path):
                since = int(st.get("line", 0))
                output_file = Path(st.get("output_file") or _today_trace_file())
                session_started = bool(st.get("session_started"))
                if stat.st_size < st.get("source_size", 0) or (
                        st.get("source_inode") is not None and st["source_inode"] != stat.st_ino):
                    since = 0
        except (json.JSONDecodeError, ValueError, OSError):
            since = 0
    else:
        since = 0

    # The cursor claims "lines up to N are already in the trace file". That
    # claim is only true while the file it wrote to still exists: delete the
    # traces to start over and the cursor would keep answering "nothing new"
    # forever, with no new records and no error. Verify the output, not the
    # bookkeeping.
    if since and not output_file.exists():
        since = 0

    n, records, meta = parse_rollout(path, since, live=live)
    if n < since:
        n, records, meta = parse_rollout(path, 0, live=live)
    if live and meta.get("session_id") and not session_started:
        # Older cursors can already be at EOF even though their session header
        # was never captured. Publish that fact once; metadata-only sessions
        # should be selectable with zero turns rather than silently disappearing.
        if not any(r["event"] == "session_start" for r in records):
            records.insert(0, {"event": "session_start", "session_id": meta["session_id"],
                               "cwd": meta["cwd"], "agent_version": meta["cli_version"],
                               "ts": meta.get("session_ts") or _iso(stat.st_mtime),
                               "source_file": str(path)})
        session_started = True
    for r in records:
        if _emit(r) is None:
            raise OSError("Codex trace write failed; import cursor was not advanced")
    if records:
        output_file = _today_trace_file()

    _state_dir.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=cur.parent, prefix=".cursor-",
                                         encoding="utf-8", delete=False) as handle:
            temporary = handle.name
            json.dump({"path": str(path), "line": n, "last": _now(),
                       "session_id": meta.get("session_id"), "source_size": stat.st_size,
                       "source_inode": stat.st_ino, "output_file": str(output_file),
                       "session_started": session_started}, handle)
        os.replace(temporary, cur)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()
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
    live = "--live" in args
    args = [arg for arg in args if arg != "--live"]
    if live and not args:
        print("--live requires explicit rollout paths", file=sys.stderr)
        return 2

    if args:
        total = 0
        missing = []
        for arg in args:
            p = Path(arg).expanduser()
            if p.is_file():
                n = import_rollout(p, live=live)
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

    try:
        written = import_rollout(path, session_id or None, live=True)
    except OSError as exc:
        # Tracing failures must not interrupt an agent action. Leave the cursor
        # unchanged so the next hook/poll can retry the records.
        print(f"agent-trace: capture failed ({type(exc).__name__}); will retry", file=sys.stderr)
        written = 0
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
