#!/usr/bin/env python3
"""agent-trace backfill for Pi — recover sessions that were never traced live.

Why this exists: the Pi adapter is a live hook. It sees the exact wire payload
before the provider call and the exact response after, which is why it is the
most complete of the three — and it is also why it can only ever record turns
that happen while it is loaded. Every session that ran before the extension was
registered, or while it was crashed, is invisible. On this machine that was 53
of 63 sessions.

Pi keeps a full transcript, so those sessions can be recovered from it:

    ~/.pi/agent/sessions/<cwd-slug>/<started>_<session-id>.jsonl

What the transcript has, and what is reconstructed:

  real, read verbatim        reconstructed from the transcript
  ---------------------      -----------------------------------
  usage (per assistant msg)  the request side: the conversation chain up to
  model / provider / api       that message. Pi does not persist the wire
  durationMs                   payload it sent, so this is the transcript it
  responseId                   was built from, not the payload itself.
  stopReason                 turn boundaries: a turn starts at each user
  per-entry timestamp          message (Pi's session entries carry no turn
  tool calls + tool results    markers — the live `turn_start` event does).
  system prompt

Provenance is therefore recorded, not implied. Backfilled records carry
`note="backfill=session"` and `request_id="pi-bf-<entry id>"`, so a reader can
tell a reconstructed record from an observed one and the two id namespaces
never collide. A session that already has live traces is SKIPPED by default
(--force overrides), because mixing the two would double-count the turns that
were seen live.

Usage:

    python3 pi/pi_import.py                       # every session, skip traced
    python3 pi/pi_import.py <session.jsonl> ...   # exactly these files
    python3 pi/pi_import.py --force               # include partly-live sessions
    python3 pi/pi_import.py --reimport            # rewrite everything (cursor ignored)
    python3 pi/pi_import.py --list                # what would be imported
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Shared trace rules (schema version, timestamps, truncation, redaction, locked
# append) live one directory up. Like the Codex importer, this file is always
# executed from the repo, so there is no deployed copy to reconcile.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
import agenttrace_common as common  # noqa: E402

AGENT = "pi"
SCHEMA_V = common.SCHEMA_V

#: Where Pi keeps transcripts. PI_CODING_AGENT_DIR moves the whole agent dir.
_agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR")
                  or (Path.home() / ".pi" / "agent"))
_sessions_dir = Path(os.environ.get("AGENTTRACE_PI_SESSIONS") or (_agent_dir / "sessions"))
_trace_dir = Path(os.environ.get("AGENTTRACE_PI_DIR") or (_agent_dir / "traces"))
_state_dir = Path(os.environ.get("AGENTTRACE_STATE_DIR") or (_trace_dir / ".state"))

#: A tool result can be a wall of terminal output; cap it like the live adapter.
MAX_CHARS = common.max_chars()


def _now() -> str:
    return common.now_iso()


def _iso_from_mtime(path: Path) -> str | None:
    """The session file's own mtime, as a last resort for a missing timestamp.

    It is a real observation of the session (when it was last written), and it
    is still the session's clock rather than the importer's — which is the
    property that matters, because a stray import-time record mixes two clocks
    inside one trace and makes its duration meaningless.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    # Clamped at "now": a file whose mtime is in the future (a clock jump, a
    # restore, a hand-touched file) produced a trace spanning decades, and a
    # fabricated multi-year duration is worse than a coarse one.
    mtime = min(mtime, datetime.now(timezone.utc).timestamp())
    return datetime.fromtimestamp(mtime, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ts(value) -> str | None:
    """An entry's own ISO timestamp, if it is one Pi actually wrote."""
    if isinstance(value, str) and value.endswith("Z") and len(value) >= 20:
        return value
    return None


def _truncate(v, limit: int | None = None):
    return common.truncate(v, limit if limit is not None else MAX_CHARS)


def _emit(rec: dict) -> None:
    """Prepend the agent, drop None values, append one JSONL line.

    Serialisation, redaction and the locked append are the shared writer's, and
    `AGENTTRACE_CAPTURE=metadata` is honoured here — at the single exit point,
    so no record shape can forget it.
    """
    out = {"agent": AGENT}
    out.update(rec)
    if not common.capture_content():
        out = common.strip_content(out)
    common.emit_record(_trace_dir, AGENT, out)


# --------------------------------------------------------------------------
# content helpers
# --------------------------------------------------------------------------

def _blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _text_of(content) -> str:
    """The prose of a message: text blocks joined, thinking excluded.

    Thinking is kept separately on the response so a reader can see that the
    model reasoned without the reasoning being mistaken for the answer.
    """
    return "\n".join(b.get("text") or "" for b in _blocks(content)
                     if b.get("type") == "text").strip()


def _thinking_of(content) -> str:
    return "\n".join(b.get("thinking") or "" for b in _blocks(content)
                     if b.get("type") == "thinking").strip()


def _tool_calls(content) -> list[dict]:
    return [{"id": b.get("id"), "name": b.get("name"),
             "arguments": _truncate(b.get("arguments"))}
            for b in _blocks(content) if b.get("type") == "toolCall"]


def _shape_messages(msgs: list[dict]) -> list[dict]:
    """Role-tagged transcript rows, matching what the live adapter stores.

    The live adapter keeps role + content (+ tool ids); the same shape here
    means one renderer reads both, and `agenttrace show` cannot tell which
    provenance a record came from — which is the point.
    """
    out: list[dict] = []
    for m in msgs:
        row = {"role": m.get("role"), "content": _truncate(_text_of(m.get("content")))}
        calls = _tool_calls(m.get("content"))
        if calls:
            row["tool_calls"] = calls
        if m.get("toolCallId"):
            row["tool_call_id"] = m["toolCallId"]
        out.append(row)
    return out


# --------------------------------------------------------------------------
# transcript reading
# --------------------------------------------------------------------------

def load_entries(path: Path) -> tuple[dict | None, list[dict]]:
    """Read a session file into (header, entries-in-conversation-order).

    Sessions are trees (`id`/`parentId`) and a file can hold abandoned
    branches. The conversation a reader cares about is the active chain, which
    is found by walking `parentId` from the LAST entry back to the root — the
    same chain Pi itself resumes. File order is the fallback for v1 sessions
    that predate the tree.
    """
    header: dict | None = None
    entries: list[dict] = []
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None, []

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "session":
                header = obj
                continue
            entries.append(obj)

    by_id = {e.get("id"): e for e in entries if e.get("id")}
    if not by_id or not any(e.get("parentId") for e in entries):
        return header, entries

    # Walk from the last entry that HAS an id. Pi appends entries that carry
    # neither id nor parentId — a trailing `model_change` is enough — and
    # starting from one of those ended the walk immediately, collapsing a whole
    # session to the two bracket records. `_last_entry_id` then stored None, so
    # every later run rewrote the same two records: total, silent, permanent
    # loss of a session that was fully recoverable.
    start = next((e for e in reversed(entries) if e.get("id")), None)
    if start is None:
        return header, entries
    walked: list[dict] = []
    seen: set[str] = set()
    node: dict | None = start
    broken = False
    while isinstance(node, dict):
        node_id = node.get("id")
        if not node_id:
            break
        if node_id in seen:
            # A cycle. Corruption rather than a branch, so the whole file is
            # kept below: dropping the real chain because a tail entry points
            # back into itself would lose the conversation entirely.
            broken = True
            break
        seen.add(node_id)
        walked.append(node)
        if "parentId" not in node:
            # The FIELD is missing (not null). Pi always writes it, so this is a
            # truncated or damaged entry — the same class of damage as a link to
            # a missing id, and reading it as a clean root dropped everything
            # before it.
            broken = True
            break
        parent = node.get("parentId")
        if parent is None:
            break                               # an explicit null: a real root
        if parent not in by_id:
            # A link to an entry that is not in the file: TRUNCATED or damaged,
            # not a branch. Discarding everything before the break would lose the
            # conversation that is still there.
            broken = True
            break
        node = by_id.get(parent)

    # Which entries belong to the conversation:
    #   * the walked chain, always;
    #   * everything, when the chain was severed by a dangling link;
    #   * entries with no id, which can never be on any chain (Pi appends some,
    #     e.g. a trailing `model_change`).
    # An entry WITH an id that the walk did not reach, in an UNBROKEN chain,
    # belongs to another branch: including it would put messages the model never
    # saw back into the reconstructed request.
    #
    # On a DAMAGED transcript the trade-off is made the other way on purpose:
    # completeness wins, because the alternative is losing messages that are
    # demonstrably real. A damaged file may therefore contribute an abandoned
    # branch to the reconstructed request — visible, and better than silence.
    included = entries if broken else [
        e for e in entries if e in walked or not e.get("id")]

    # Order by the file, not by walk order: an id-less entry has no position in
    # the chain, and appending it to the end moved a mid-file entry after its own
    # answer (and turned an abandoned id-less message into a phantom last turn).
    order = {id(e): i for i, e in enumerate(entries)}
    included.sort(key=lambda e: order[id(e)])
    return header, included


def session_id_of(path: Path, header: dict | None) -> str:
    """The id the live adapter would have used: the session FILE path.

    The live hook reports `ctx.sessionManager.getSessionFile()`, and the panel
    scopes sessions by that exact string. Deriving anything else here would put
    a backfilled session under a second identity and split it from its own
    live records.
    """
    # Resolved, because the id IS the identity: importing the same session as
    # `slug/sess.jsonl` and as `/abs/path/slug/sess.jsonl` produced two ids, two
    # traces, and a store that could not tell they were one session. Pi itself
    # reports an absolute path, so resolving also matches live records.
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def build_records(path: Path, header: dict | None,
                  entries: list[dict]) -> list[dict]:
    """One session transcript -> the v1 records the live adapter would emit."""
    session_id = session_id_of(path, header)
    cwd = (header or {}).get("cwd")
    session_ts = _ts((header or {}).get("timestamp"))

    # A record's ts must come from the SESSION's clock. `emit_record` fills an
    # absent ts with the wall clock, so anything left empty lands in the trace
    # stamped with the import time — the defect the codex importer had, and the
    # reason the test asserts "none from the import time". The session file's own
    # mtime is the last resort: still the session's clock, never the importer's.
    fallback_ts = session_ts or _iso_from_mtime(path)

    records: list[dict] = [{
        "event": "session_start",
        "session_id": session_id,
        "cwd": cwd,
        "model": None,
        "ts": fallback_ts,
        "note": "backfill=session reason=recovered-from-transcript",
    }]
    transcript: list[dict] = []          # the conversation, in order
    # The PREVIOUS entry's timestamp: the message that triggered the request
    # being built. Updated in exactly one place below, because setting it inside
    # each branch is how the first turn's request ended up with no trigger at
    # all and, before that, how both halves of a span ended up with one
    # timestamp and a duration of zero.
    prev_ts: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    provider: str | None = None
    api_mode: str | None = None
    turn = -1
    #: tool_call_id -> {name, args, turn, ts}; popped when its result arrives.
    pending_tool: dict[str, dict] = {}

    for entry in entries:
        ts = _ts(entry.get("timestamp")) or fallback_ts
        if ts:
            fallback_ts = ts             # carry forward: same session, same clock
        etype = entry.get("type")
        if etype == "model_change":
            model = entry.get("model") or entry.get("to") or model
            provider = entry.get("provider") or provider
            continue
        if etype != "message":
            # `thinking_level_change` / `context_edit` change how the session
            # behaves but are not conversation. Skipping them keeps the
            # transcript a transcript.
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        # This entry triggers whatever request comes next; the previous one is
        # the trigger of THIS request.
        trigger_ts, prev_ts = prev_ts, ts
        role = msg.get("role")
        if msg.get("model"):
            model = msg["model"]
        if msg.get("provider"):
            provider = msg["provider"]
        if msg.get("api"):
            api_mode = msg["api"]

        if role == "system":
            # Pi stores the system prompt as a message entry; it belongs in the
            # request, not in the conversation.
            system_prompt = _text_of(msg.get("content")) or system_prompt
            continue

        if role == "user":
            turn += 1
            transcript.append(msg)
            records.append({
                "event": "user_prompt",
                "session_id": session_id,
                "turn_id": _turn_id(turn),
                "model": model,
                "cwd": cwd,
                "ts": ts,
                "request": {"messages": _shape_messages([msg])},
                "note": "backfill=session",
            })
            continue

        if role == "assistant":
            if turn < 0:
                # An assistant message before any user message (a resumed
                # transcript can start mid-turn): give it its own turn rather
                # than folding it into a turn it did not belong to.
                turn += 1
            request_id = "pi-bf-" + _entry_key(entry)
            usage = _usage(msg.get("usage"))
            records.append({
                "event": "llm_request",
                "session_id": session_id,
                "turn_id": _turn_id(turn),
                "request_id": request_id,
                "model": model,
                "provider": provider,
                "api_mode": api_mode,
                "cwd": cwd,
                # The REQUEST is stamped with the entry that triggered it, not
                # with the response that answered it. Both halves used to take
                # the assistant entry's timestamp, so every llm span's duration
                # was exactly 0 — and the time a model call took is the number a
                # reader comes to this view for. Pi does not log the call's start,
                # so the triggering message is the best anchor there is; that it
                # is an approximation is recorded below.
                "ts": trigger_ts or ts,
                "request": _truncate({
                    "messages": _shape_messages(transcript),
                    "system_prompt": system_prompt,
                    "message_count": len(transcript),
                }),
                "note": "backfill=session (request rebuilt from the transcript; "
                        "Pi does not persist the wire payload; request_time="
                        + ("trigger-entry" if trigger_ts else "response-entry")
                        + ")",
            })
            calls = _tool_calls(msg.get("content"))
            response: dict = {
                "content": _truncate(_text_of(msg.get("content"))),
                "reasoning": _truncate(_thinking_of(msg.get("content"))) or None,
                "tool_calls": calls or None,
                "finish_reason": msg.get("stopReason") or msg.get("rawStopReason"),
                "usage": usage,
            }
            records.append({
                "event": "llm_response",
                "session_id": session_id,
                "turn_id": _turn_id(turn),
                "request_id": request_id,
                "model": model,
                "provider": provider,
                "api_mode": api_mode,
                "duration_ms": _ms(msg.get("durationMs")),
                "ts": ts,
                "response": response,
                "note": (f"backfill=session response_id={msg['responseId']}"
                         if isinstance(msg.get("responseId"), str) else "backfill=session"),
            })
            transcript.append(msg)
            for call in calls:
                if call.get("id"):
                    # A provider's tool-call ids are unique within ONE API
                    # RESPONSE, not within a session or even a turn: the same
                    # `call_319025` was observed twice in one real turn (two
                    # separate API calls), and keying the span on it alone made
                    # the v2 tree contain one span id twice. Scoping it by the
                    # request that produced it is uniqueness by construction,
                    # and stays deterministic across re-imports because the
                    # request id is derived from the transcript entry.
                    pending_tool[call["id"]] = {
                        "name": call.get("name"), "ts": ts, "turn": _turn_id(turn),
                        # The arguments are the whole point of a tool span: a
                        # record that says `bash` ran and not WHAT it ran cannot
                        # answer "why did this fail".
                        "args": call.get("arguments"),
                        "scoped": f"{request_id}:{call['id']}",
                    }
            continue

        if role == "toolResult":
            transcript.append(msg)
            call_id = msg.get("toolCallId")
            known = pending_tool.pop(call_id, {}) if call_id else {}
            status = "error" if msg.get("isError") else "ok"
            name = msg.get("toolName") or known.get("name") or "?"
            # The scoped id pairs the halves; the provider's own id is kept
            # beside it so the original is still greppable.
            # The fallback needs the RESULT's own identity: a tool call on an
            # off-chain branch has no request to scope by, and two such results
            # sharing a call id collapsed into one span (the store's
            # (trace_id, span_id) key silently dropped the loser).
            scoped_id = known.get("scoped") or (
                f"unscoped:{call_id}@{_entry_key(entry)}" if call_id else None)
            common_fields = {
                "session_id": session_id,
                "turn_id": known.get("turn") or (_turn_id(turn) if turn >= 0 else None),
                "model": model,
                "ts": ts,
            }
            # Both halves are emitted from the one transcript entry, exactly as
            # the live adapter emits them from one hook call. The call's own
            # timestamp is the assistant message that requested it, which is
            # what makes the tool span's duration meaningful.
            records.append({
                "event": "tool_call",
                **common_fields,
                "tool": {"name": name, "call_id": scoped_id,
                         "provider_call_id": call_id,
                         "args": _truncate(known.get("args"))},
                "ts": known.get("ts") or ts,
                "note": "backfill=session",
            })
            records.append({
                "event": "tool_result",
                **common_fields,
                "tool": {"name": name, "call_id": scoped_id,
                         "provider_call_id": call_id, "status": status,
                         "exit_code": _exit_code(msg.get("content"))},
                "response": {"content": _truncate(_text_of(msg.get("content")))},
                "duration_ms": _ms(msg.get("durationMs")),
                "error": ({"type": "tool_error",
                           "message": _truncate(_text_of(msg.get("content")), 4000)}
                          if status == "error" else None),
                "note": "backfill=session",
            })
            continue
        # `custom` (hook-injected) and anything Pi adds later: keep the
        # transcript complete so the NEXT request's reconstruction is right,
        # but do not invent a record for it.
        transcript.append(msg)

    records.append({
        "event": "session_end",
        "session_id": session_id,
        "cwd": cwd,
        "model": model,
        # `fallback_ts` has advanced to the last entry's own timestamp inside the
        # loop, so this is the session's end by the session's clock even when the
        # transcript carries no timestamps at all.
        "ts": fallback_ts,
        "note": "backfill=session reason=transcript-end",
    })
    return records


def _turn_id(turn: int) -> str:
    """A backfilled turn's id, in its own namespace.

    The live adapter numbers turns by provider turn and the backfill numbers
    them by user message, so both start at 0 for the same session. They met in
    one trace_id — the trace key is (adapter, session, turn) — and the v2 tree
    then held two task roots with one id and two spans for one span id. A
    prefix keeps the two observations of the same session apart, which is
    also the truth: they were collected by different means.
    """
    return f"bf-{turn}"


def _entry_key(entry: dict) -> str:
    """A stable key for one transcript entry.

    Pi's own 8-char entry id when it has one, else a digest of the entry. The
    id must be identical on every run, because it is what makes re-importing a
    session idempotent rather than a source of duplicates.
    """
    if entry.get("id"):
        return str(entry["id"])
    canonical = json.dumps(entry, sort_keys=True, default=str)
    return hashlib.sha1(canonical.encode("utf-8", "replace")).hexdigest()[:12]


def _usage(raw) -> dict | None:
    """Pi's usage block -> the shared token fields.

    Pi reports `input` EXCLUDING cache reads, while the shared contract defines
    `input_tokens` as the total (see schema/CHANGELOG.md). Summing the cache
    back in is the same correction the live adapter makes — without it the
    dashboard's cache hit rate exceeds 100% again.
    """
    if not isinstance(raw, dict):
        return None
    cache_read = common.pick_number(raw, "cacheRead", "cache_read")
    cache_write = common.pick_number(raw, "cacheWrite", "cache_write")
    base_in = common.pick_number(raw, "input", "input_tokens", "prompt_tokens")
    output = common.pick_number(raw, "output", "output_tokens", "completion_tokens")
    reasoning = common.pick_number(raw, "reasoning", "reasoning_tokens")
    if all(v is None for v in (base_in, cache_read, cache_write, output, reasoning)):
        return None

    def positive(value: int | None) -> int | None:
        # A provider reporting a negative count is a data error, and a negative
        # input would break the invariant the whole platform relies on:
        # cache_read_tokens <= input_tokens, i.e. a hit rate of at most 100%.
        return None if value is None else max(0, value)

    base_in, cache_read, cache_write = (positive(base_in), positive(cache_read),
                                        positive(cache_write))
    # `input` absent but a cache figure present is not "no usage": the cache
    # portion IS part of the prompt total, so it is the floor we can stand on.
    # Returning None here silently dropped the only token data a provider gave.
    usage = {
        "input_tokens": None if base_in is None and not (cache_read or cache_write)
        else (base_in or 0) + (cache_read or 0) + (cache_write or 0),
        "output_tokens": positive(output),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": positive(reasoning),
    }
    return {k: v for k, v in usage.items() if v is not None}


def _ms(value) -> int | None:
    """Pi reports milliseconds; the field name says milliseconds. No scaling."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


#: Pi's bash tool reports its status as TEXT, and the wording it uses is
#: "Process exited with code 0" — not "exit code 0". Matching only the latter
#: silently produced no exit code at all, which is the field that separates
#: "the command ran" from "the command succeeded".
_EXIT_RE = re.compile(
    r"(?:exit(?:ed)?(?:\s+with)?\s+(?:code|status)|exit_code|returncode|exit status)"
    r"\s*[:=]?\s*(-?\d+)", re.IGNORECASE)


def _exit_code(content) -> int | None:
    match = _EXIT_RE.search(_text_of(content))
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------
# coverage bookkeeping
# --------------------------------------------------------------------------

def _cursor_path(session: Path) -> Path:
    digest = hashlib.sha256(str(session.resolve()).encode("utf-8")).hexdigest()[:16]
    return _state_dir / f"pi-import-{digest}.json"


def _canon(session_id: str) -> str:
    """A session id in canonical form for comparison.

    The importer writes the resolved path, and the live adapter writes whatever
    Pi reported. On macOS those differ for anything under /var (a symlink to
    /private/var), so comparing the raw strings would call one session two and
    duplicate its trace.
    """
    return os.path.realpath(session_id) if "/" in session_id else session_id


def traced_sessions() -> set[str]:
    """Every session id the live adapter has already written."""
    out: set[str] = set()
    for path in sorted(_trace_dir.glob("*.jsonl")):
        try:
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"session_id"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("session_id"):
                    out.add(rec["session_id"])
    return out


def all_sessions() -> list[Path]:
    if not _sessions_dir.is_dir():
        return []
    return sorted(_sessions_dir.rglob("*.jsonl"))


def import_session(path: Path, *, force: bool = False, reimport: bool = False,
                   already: set[str] | None = None) -> tuple[int, str]:
    """Import one transcript. Returns (records written, outcome).

    The two flags are separate on purpose:
      * `force`    — backfill even though the session has live traces (for a
                     session that was only partly observed live). It must NOT
                     rewrite what is already on disk, or the append-only file
                     grows a second copy of every record and the v1 readers
                     count them twice.
      * `reimport` — ignore the cursor and write everything again, for when the
                     trace file was deleted or damaged.
    """
    already = traced_sessions() if already is None else already
    session_id = session_id_of(path, None)
    if _canon(session_id) in {_canon(a) for a in already} and not force:
        return 0, "already-traced"

    header, entries = load_entries(path)
    if not entries:
        return 0, "empty"

    cursor = _cursor_path(path)
    last_id: str | None = None
    if cursor.exists() and not reimport:
        try:
            state = json.loads(cursor.read_text(encoding="utf-8"))
            if state.get("path") == str(path):
                last_id = state.get("last_entry_id")
        except (OSError, json.JSONDecodeError):
            last_id = None
    if last_id:
        # Re-importing is already idempotent (deterministic ids in the v2
        # conversion), but skipping the entries we have seen avoids rewriting
        # the whole session on every run.
        for i, entry in enumerate(entries):
            if entry.get("id") == last_id:
                entries = entries[i + 1:]
                break
        if not entries:
            return 0, "up-to-date"

    records = build_records(path, header, entries)
    for rec in records:
        _emit(rec)

    _state_dir.mkdir(parents=True, exist_ok=True)
    try:
        cursor.write_text(json.dumps(
            {"path": str(path), "last_entry_id": _last_entry_id(path, entries),
             "session_id": session_id, "last": _now()}), encoding="utf-8")
    except OSError:
        pass
    return len(records), "imported"


def _last_entry_id(path: Path, imported: list[dict]) -> str | None:
    """The id of the last entry WITH an id, in the full chain.

    Storing the tail's last id is what makes the next run resumable. Storing
    None (which a trailing id-less entry caused) made every later run re-append
    the same two records and never recover the messages.
    """
    if imported and imported[-1].get("id"):
        return imported[-1]["id"]
    _, entries = load_entries(path)
    for entry in reversed(entries):
        if entry.get("id"):
            return entry["id"]
    return None


def main(argv: list[str]) -> int:
    force = "--force" in argv or "--reimport" in argv
    reimport = "--reimport" in argv
    listing = "--list" in argv
    explicit = [a for a in argv if not a.startswith("--")]

    if "--help" in argv or "-h" in argv:
        print(__doc__.strip())
        return 0

    if explicit:
        targets = [Path(a).expanduser() for a in explicit]
        missing = [p for p in targets if not p.is_file()]
        if missing:
            print(f"not a file: {', '.join(str(p) for p in missing)}", file=sys.stderr)
            return 1
    else:
        targets = all_sessions()
    if not targets:
        print(f"no sessions under {_sessions_dir}", file=sys.stderr)
        return 1

    already = traced_sessions()
    if listing:
        print(f"{'records':>8}  {'outcome':<14} session")
        for path in targets:
            _, entries = load_entries(path)
            outcome = ("already-traced"
                       if (_canon(session_id_of(path, None))
                           in {_canon(a) for a in already}) and not force
                       else "would-import")
            n = len(build_records(path, None, entries)) if entries else 0
            print(f"{n:>8}  {outcome:<14} {path.name}")
        return 0

    total = 0
    outcomes: dict[str, int] = {}
    for path in targets:
        n, outcome = import_session(path, force=force, reimport=reimport,
                                    already=already)
        total += n
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if n:
            print(f"{n:>5} records  {path.name}", file=sys.stderr)
    print(f"pi backfill: {total} records from {len(targets)} session(s); "
          + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())), file=sys.stderr)
    return 0 if total else (0 if outcomes.get("already-traced") else 1)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
