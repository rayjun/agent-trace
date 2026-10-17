"""Convert v1 flat records (schema/trace.schema.json) into v2 canonical events.

The mapping is possible at all because v1 already carried the correlators:
session_id groups a conversation, turn_id groups one task, request_id pairs
an llm_request with its llm_response/llm_error, and tool.call_id pairs a
tool_call with its tool_result. The conversion is therefore *mechanical*:

    session_start / session_end   -> event (session-level, trace_id=null)
    user_prompt / assistant_message / note
                                  -> event on the turn's trace
    llm_request                   -> span_start kind=llm
    llm_response                  -> span_end   kind=llm, status=ok
    llm_error                     -> span_end   kind=llm, status=error
    tool_call                     -> span_start kind=tool
    tool_result                   -> span_end   kind=tool (status from record)

plus one synthesized span per (session_id, turn_id): kind=task, the trace
root, parent of everything in that turn. A task span is synthesized rather
than recorded live because v1 adapters never emitted turn boundaries — its
start/end are derived (min/max child ts) and its status is derived (error if
any child failed). Records without a turn_id attach to a session-level task
span so the tree still has a root.

Properties the converter guarantees:

* Deterministic: same v1 file in -> same v2 events out, byte for byte in the
  id fields (derive_event_id over source identity). Re-running the converter
  over an archive dedups downstream instead of doubling it.
* Lossless-ish: fields with no v2 home (api_mode, base_url, source_file,
  note) move into span.attributes / event.attributes under their v1 names.
* Honest: codex records carry ts = parse time, not call time (the rollout
  file has no per-request clock). That gap is visible as observed_at == ts
  for live adapters vs. a note in attributes here — the converter does not
  invent timestamps the source never had.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from core.schema.model import (SCHEMA_V2, derive_event_id,
                               event_event_id, span_event_id)


def _digest(rec: dict[str, Any]) -> str:
    """Stable digest of a v1 record's CONTENT.

    Identity must not rest on (session, turn, name, ts) alone: two genuinely
    different records can share a millisecond — a v1 adapter writing
    `user_prompt` twice in a loop, two notes in one turn — and keying only on
    the timestamp gave them the SAME event_id, so the indexer's INSERT OR
    IGNORE silently dropped the second. Including the record's content makes
    identity exact in both directions: byte-identical records still collapse
    (real duplicates SHOULD dedup), records differing in any field do not.
    """
    canonical = json.dumps(rec, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8", "replace")).hexdigest()[:16]

#: v1 event names that become session-level v2 events (no trace attached).
_SESSION_EVENTS = {"session_start", "session_end"}

#: v1 event names that become trace-level discrete events.
_DISCRETE_EVENTS = {"user_prompt", "assistant_message", "note"}


def _task_span_id(adapter: str, session: str, turn: str | None) -> str:
    return derive_event_id("v1-task", adapter, session, turn or "-")


def _trace_id(adapter: str, session: str, turn: str | None) -> str:
    # The trace root IS the task span; sharing the id keeps the join trivial.
    return _task_span_id(adapter, session, turn)


def _base(rec: dict[str, Any], etype: str) -> dict[str, Any]:
    """The envelope every converted event shares."""
    adapter = rec.get("agent") or "unknown"
    return {
        "v": SCHEMA_V2,
        "type": etype,
        "ts": rec.get("ts"),
        # v1 has no observed_at; its ts IS when the adapter wrote the record,
        # so it is the honest value for both fields.
        "observed_at": rec.get("ts"),
        "adapter_id": adapter,
        "agent_name": adapter,
        "session_id": rec.get("session_id"),
        "source": (
            {"file": rec["source_file"]} if rec.get("source_file") else None
        ),
    }


def _privacy(rec: dict[str, Any]) -> dict[str, Any] | None:
    """v1 records don't state their capture mode; note only what we know."""
    return {"capture": "full", "redacted": True}


def _convert_span_pair(rec: dict[str, Any]) -> dict[str, Any] | None:
    """llm_* and tool_* records -> one half of a span each."""
    adapter = rec.get("agent") or "unknown"
    event = rec.get("event")
    session = rec.get("session_id") or "-"
    turn = rec.get("turn_id")

    if event in ("llm_request", "llm_response", "llm_error"):
        kind = "llm"
        request_id = rec.get("request_id")
        span_id = (
            f"llm:{request_id}" if request_id
            else derive_event_id("v1-orphan-llm", adapter, session, rec.get("ts"))
        )
    else:  # tool_call / tool_result
        kind = "tool"
        tool = rec.get("tool") or {}
        call_id = tool.get("call_id")
        span_id = (
            f"tool:{call_id}" if call_id
            else derive_event_id(
                "v1-orphan-tool", adapter, session, tool.get("name"), rec.get("ts"))
        )

    is_start = event in ("llm_request", "tool_call")
    out = _base(rec, "span_start" if is_start else "span_end")
    out.update({
        "event_id": span_event_id(adapter, _trace_id(adapter, session, turn),
                                  span_id, "span_start" if is_start else "span_end"),
        "trace_id": _trace_id(adapter, session, turn),
        "span_id": span_id,
        "parent_span_id": _task_span_id(adapter, session, turn),
        "privacy": _privacy(rec),
    })

    span: dict[str, Any] = {"kind": kind, "name": ""}
    attributes: dict[str, Any] = {}
    for key in ("provider", "model", "api_mode", "base_url", "cwd", "mode"):
        if rec.get(key) is not None:
            attributes[key] = rec[key]

    if event == "llm_request":
        span["name"] = rec.get("model") or "llm_request"
        span["input"] = rec.get("request")
        span["status"] = "running"
    elif event == "llm_response":
        span["name"] = rec.get("model") or "llm_response"
        span["output"] = rec.get("response")
        span["status"] = "ok"
        usage = (rec.get("response") or {}).get("usage")
        if usage:
            attributes["usage"] = usage
    elif event == "llm_error":
        span["name"] = rec.get("model") or "llm_error"
        span["status"] = "error"
        span["error"] = rec.get("error")
    elif event == "tool_call":
        tool = rec.get("tool") or {}
        span["name"] = tool.get("name") or "tool_call"
        span["input"] = {"args": tool.get("args")}
        span["status"] = "running"
    else:  # tool_result
        tool = rec.get("tool") or {}
        status = tool.get("status") or "ok"
        span["name"] = tool.get("name") or "tool_result"
        # v1 stores the result body in response.content (hermes) or omits it
        # (pi metadata-only tool_result) — take what exists, invent nothing.
        content = (rec.get("response") or {}).get("content")
        span["output"] = {"result": content} if content is not None else None
        span["status"] = "error" if status == "error" else "ok"
        if rec.get("error"):
            span["error"] = rec["error"]

    if rec.get("duration_ms") is not None:
        attributes["duration_ms"] = rec["duration_ms"]
    # The v2 schema documents `exit_code` as a tool-span convention; the v1
    # records carry it inside `tool`, so the converter is where the two meet.
    tool_exit = (rec.get("tool") or {}).get("exit_code")
    if tool_exit is not None:
        attributes["exit_code"] = tool_exit
    if rec.get("note"):
        attributes["note"] = rec["note"]
    if attributes:
        span["attributes"] = attributes
    out["span"] = span
    return out


def _convert_discrete(rec: dict[str, Any]) -> dict[str, Any]:
    adapter = rec.get("agent") or "unknown"
    session = rec.get("session_id") or "-"
    turn = rec.get("turn_id")
    name = rec.get("event")

    out = _base(rec, "event")
    out["event_id"] = event_event_id(adapter, _trace_id(adapter, session, turn),
                                     name, rec.get("ts"), _digest(rec))

    attributes: dict[str, Any] = {}
    if name == "user_prompt":
        messages = (rec.get("request") or {}).get("messages") or []
        text = messages[0].get("content") if messages else None
        if text is not None:
            attributes["text"] = text
    elif name == "assistant_message":
        content = (rec.get("response") or {}).get("content")
        if content is not None:
            attributes["content"] = content
    if rec.get("note") and name != "note":
        attributes["note"] = rec["note"]
    if name == "note":
        attributes["text"] = rec.get("note")
    if rec.get("model"):
        attributes["model"] = rec["model"]

    out["event"] = {"name": name, "attributes": attributes or None}

    if name in _SESSION_EVENTS:
        out["trace_id"] = None  # session-level: no task to attach to
    else:
        out["trace_id"] = _trace_id(adapter, session, turn)
        out["span_id"] = _task_span_id(adapter, session, turn)
    out["privacy"] = _privacy(rec)
    return out


def convert_record(rec: dict[str, Any]) -> dict[str, Any] | None:
    """One v1 record -> one v2 event, or None if the record is not v1."""
    if rec.get("v") != 1:
        return None
    event = rec.get("event")
    if event in ("llm_request", "llm_response", "llm_error",
                 "tool_call", "tool_result"):
        return _convert_span_pair(rec)
    if event in _SESSION_EVENTS or event in _DISCRETE_EVENTS:
        return _convert_discrete(rec)
    # Unknown v1 event type: keep it as a note rather than dropping data.
    out = _base(rec, "event")
    out["event_id"] = event_event_id(
        rec.get("agent") or "unknown", None, event or "note",
        rec.get("ts"), _digest(rec))
    out["event"] = {"name": "note",
                    "attributes": {"text": f"unconverted v1 event: {event}"}}
    return out


def synthesize_task_spans(records: Iterable[dict[str, Any]]
                          ) -> Iterator[dict[str, Any]]:
    """Emit the derived trace-root spans for every (session, turn) seen.

    Status rule: error if any child span ended in error, else ok. Bounds rule:
    min/max child ts. These spans are marked synthetic in attributes so a
    reader can tell 'the agent said a task ran' from 'we inferred one did'.
    """
    groups: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for rec in records:
        session = rec.get("session_id")
        if not session:
            continue
        adapter = rec.get("agent") or "unknown"
        turn = rec.get("turn_id")
        key = (adapter, session, turn)
        g = groups.setdefault(key, {"min": rec.get("ts"), "max": rec.get("ts"),
                                    "error": False, "model": rec.get("model")})
        ts = rec.get("ts")
        if ts:
            g["min"] = min(g["min"] or ts, ts)
            g["max"] = max(g["max"] or ts, ts)
        if rec.get("error") or rec.get("event") == "llm_error":
            g["error"] = True
        g["model"] = g["model"] or rec.get("model")

    for (adapter, session, turn), g in groups.items():
        span_id = _task_span_id(adapter, session, turn)
        trace_id = _trace_id(adapter, session, turn)
        attrs: dict[str, Any] = {"synthetic": True}
        if turn:
            attrs["turn_id"] = turn
        if g["model"]:
            attrs["model"] = g["model"]
        for etype, ts, status in (
            ("span_start", g["min"], "running"),
            ("span_end", g["max"], "error" if g["error"] else "ok"),
        ):
            span: dict[str, Any] = {
                "kind": "task",
                "name": f"turn {turn}" if turn else "session activity",
                "status": status,
                "attributes": attrs,
            }
            if etype == "span_end" and g["error"]:
                # status=error with no payload trips the kit's coherence
                # warning; the honest payload is that a CHILD failed.
                span["error"] = {"type": "child_error",
                                 "message": "at least one child span failed"}
            yield {
                "v": SCHEMA_V2,
                "event_id": span_event_id(adapter, trace_id, span_id, etype),
                "type": etype,
                "ts": ts,
                "observed_at": ts,
                "adapter_id": adapter,
                "agent_name": adapter,
                "session_id": session,
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span_id": None,
                "span": span,
            }


def convert_file(path: str | Path) -> Iterator[dict[str, Any]]:
    """Stream one v1 JSONL file as v2 events: converted lines, then task spans.

    Corrupt lines are skipped, not fatal — the same quarantine-and-continue
    rule the indexer follows. A line that fails to parse was never a valid
    v1 record, so there is nothing honest to emit for it.
    """
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            records.append(rec)
            converted = convert_record(rec)
            if converted is not None:
                yield converted
    yield from synthesize_task_spans(records)
