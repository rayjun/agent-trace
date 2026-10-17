"""OTLP (OpenTelemetry Protocol) import/export for canonical events.

Interoperability without surrender (proposal §9.1): OTLP is a transport and a
vocabulary, not our data model. Two rules make the bridge honest:

1. **We map into OTel's GenAI semantic conventions where they exist.**
   `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.operation.name`,
   `gen_ai.system` — so Grafana/Tempo/Jaeger and any OTel consumer can read
   our spans without knowing we exist.

2. **We keep our own semantics in a namespaced attribute block.** Agent-trace
   fields with no OTel home (span kind, session, event id, privacy state,
   synthetic markers) travel as `agent_trace.*`. OTel's model cannot express
   "this tool span never closed" or "this trace root was inferred", and
   flattening those into generic attributes would be exactly the loss the
   proposal warns against.

IDs: OTLP requires 16-byte trace / 8-byte span hex ids. Our ids are arbitrary
strings, so export DERIVES hex ids deterministically (sha1) and carries the
originals in `agent_trace.*`; import prefers those, making export→import
lossless for our own data. Foreign OTLP (no `agent_trace.*`) is imported by
deriving our ids from the OTel ids — readable, and honestly labelled.

Stdlib only: OTLP/HTTP+JSON is the wire format; no protobuf dependency.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterator

from core.schema.model import (SCHEMA_V2, TERMINAL_STATUSES,
                               event_event_id, span_event_id)

SCOPE_NAME = "agent-trace"
SCOPE_VERSION = "2"

ATTR_PREFIX = "agent_trace."

#: OTLP span kind enum.
_KIND_INTERNAL, _KIND_SERVER, _KIND_CLIENT = 1, 2, 3
_STATUS_UNSET, _STATUS_OK, _STATUS_ERROR = 0, 1, 2

_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


# --------------------------------------------------------------------------
# ids & times
# --------------------------------------------------------------------------

def hex_trace_id(value: str) -> str:
    """Deterministic 32-hex OTLP trace id from a canonical trace id."""
    return hashlib.sha1(f"trace\x1f{value}".encode()).hexdigest()[:32]


def hex_span_id(value: str) -> str:
    """Deterministic 16-hex OTLP span id from a canonical span id."""
    return hashlib.sha1(f"span\x1f{value}".encode()).hexdigest()[:16]


def _nano(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, _TS_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return str(int(dt.timestamp() * 1_000_000_000))


def _iso(nano: str | int | None) -> str | None:
    if nano is None:
        return None
    try:
        seconds = int(nano) / 1_000_000_000
    except (TypeError, ValueError):
        return None
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------
# attribute encoding (OTLP AnyValue)
# --------------------------------------------------------------------------

def _any(value: Any) -> dict[str, Any] | None:
    """Encode one value, or None when it cannot be represented.

    OTLP has no null AnyValue, so a JSON `null` must be OMITTED. Serialising
    it to the string "null" instead — which the old fallback did — made a
    round trip turn `{"system_prompt": null}` into
    `{"system_prompt": "null"}`, i.e. a falsy absence into a truthy string.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}      # int64 is a STRING on the wire
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [
            encoded for encoded in (_any(v) for v in value) if encoded is not None]}}
    if isinstance(value, dict):
        values = []
        for k, v in value.items():
            encoded = _any(v)
            if encoded is not None:
                values.append({"key": str(k), "value": encoded})
        return {"kvlistValue": {"values": values}}
    return {"stringValue": json.dumps(value, default=str)}


def _attrs(pairs: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, value in pairs:
        encoded = _any(value)
        if encoded is not None:
            out.append({"key": key, "value": encoded})
    return out


def _unwrap(any_value: dict[str, Any]) -> Any:
    if "stringValue" in any_value:
        return any_value["stringValue"]
    if "boolValue" in any_value:
        return any_value["boolValue"]
    if "intValue" in any_value:
        try:
            return int(any_value["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in any_value:
        return any_value["doubleValue"]
    if "arrayValue" in any_value:
        return [_unwrap(v) for v in (any_value["arrayValue"] or {}).get("values", [])]
    if "kvlistValue" in any_value:
        return {kv["key"]: _unwrap(kv.get("value") or {})
                for kv in (any_value["kvlistValue"] or {}).get("values", [])}
    return None


def _read_attrs(raw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in raw or []:
        if isinstance(item, dict) and "key" in item:
            out[item["key"]] = _unwrap(item.get("value") or {})
    return out


# --------------------------------------------------------------------------
# export: canonical trace -> OTLP
# --------------------------------------------------------------------------

def _span_attributes(span: dict[str, Any], adapter_id: str,
                     container: bool = False,
                     event: dict[str, Any] | None = None,
                     halves: dict[str, Any] | None = None
                     ) -> list[tuple[str, Any]]:
    halves = halves or {}
    attrs = span.get("attributes") or {}
    model = span.get("model") or attrs.get("model")
    tokens = span.get("tokens") or {}
    envelope = event or {}
    pairs: list[tuple[str, Any]] = [
        ("gen_ai.system", adapter_id),
        ("gen_ai.operation.name", {
            "llm": "chat", "tool": "execute_tool", "agent": "invoke_agent",
        }.get(span.get("kind") or "", "invoke_workflow")),
        ("gen_ai.request.model", model),
        ("gen_ai.usage.input_tokens", tokens.get("input")),
        ("gen_ai.usage.output_tokens", tokens.get("output")),
        ("gen_ai.usage.cached_input_tokens", tokens.get("cache_read")),
        # --- our semantics, namespaced ---
        (f"{ATTR_PREFIX}span_kind", span.get("kind")),
        # OTLP has ONE error code for every failure, so the exact canonical
        # status travels separately: without it, cancelled/interrupted come
        # back as 'error' and the distinction the schema defines is lost.
        (f"{ATTR_PREFIX}status", span.get("status")),
        # When the ORIGINAL adapter wrote this event. Import is a transport of
        # an existing observation, not a new one, so this must survive —
        # otherwise every re-import produces a byte-different payload for the
        # same event_id and the round trip is lossy in a way only the store's
        # conflict counter can see.
        (f"{ATTR_PREFIX}observed_at", envelope.get("observed_at")),
        (f"{ATTR_PREFIX}ts", span.get("start") or span.get("end")),
        # Per SPAN, not per trace. Reading these off the first span that had
        # them stamped every span in the trace with that one span's values —
        # which mislabels privacy (a capture=full span came back as
        # capture=metadata) rather than merely dropping a field.
        (f"{ATTR_PREFIX}agent_version", span.get("agent_version")
         or envelope.get("agent_version")),
        (f"{ATTR_PREFIX}privacy", span.get("privacy")),
        (f"{ATTR_PREFIX}source", span.get("source")),
        # The projection MERGES a span's two halves into one attributes bag
        # (the UI wants one view), but OTLP has one attribute set per span and
        # the canonical model has one per half. Carrying each half's own bag
        # separately is what makes the round trip exact; the flat `gen_ai.*`
        # keys above are still emitted so a foreign backend needs none of this.
        (f"{ATTR_PREFIX}start_attributes", halves.get("start")),
        (f"{ATTR_PREFIX}end_attributes", halves.get("end")),
        # Fallback for an export built from the projection alone (no raw
        # events): one merged bag. Less precise, still not lossy for anything
        # the projection itself holds.
        (f"{ATTR_PREFIX}span_attributes",
         attrs or None if not halves else None),
        (f"{ATTR_PREFIX}synthetic", True if span.get("synthetic") else None),
        # An orphan END (a tool_result whose tool_call was never seen) has no
        # start. OTLP has no way to express that, and the import path would
        # otherwise synthesise a span_start from the end's timestamp — turning
        # one recorded event into two on every round trip.
        (f"{ATTR_PREFIX}start_missing", True if not span.get("start") else None),
        (f"{ATTR_PREFIX}ended", False if span.get("ended") is False else None),
        (f"{ATTR_PREFIX}container", True if container else None),
    ]
    for key in ("provider", "api_mode", "base_url", "cwd", "duration_ms",
                "turn_id", "note", "exit_code"):
        if attrs.get(key) is not None:
            pairs.append((f"{ATTR_PREFIX}attr.{key}", attrs[key]))
    if span.get("input") is not None:
        pairs.append((f"{ATTR_PREFIX}input", span["input"]))
    if span.get("output") is not None:
        pairs.append((f"{ATTR_PREFIX}output", span["output"]))
    if span.get("error") is not None:
        pairs.append((f"{ATTR_PREFIX}error", span["error"]))
    return pairs


def _otlp_span(span: dict[str, Any], adapter_id: str, session_id: str | None,
               events: list[dict[str, Any]],
               container: bool = False,
               envelope: dict[str, Any] | None = None,
               halves: dict[str, Any] | None = None) -> dict[str, Any]:
    status = span.get("status")
    code = (_STATUS_ERROR if status in ("error", "interrupted", "cancelled")
            else _STATUS_OK if status == "ok" else _STATUS_UNSET)
    error = span.get("error") or {}
    parent = span.get("parent_span_id")
    out: dict[str, Any] = {
        "traceId": hex_trace_id(span["trace_id"]),
        "spanId": hex_span_id(span["span_id"]),
        "name": span.get("name") or span["span_id"],
        "kind": (_KIND_SERVER if span.get("kind") == "tool" else _KIND_INTERNAL),
        "startTimeUnixNano": _nano(span.get("start")) or _nano(span.get("end")),
        "attributes": _attrs(_span_attributes(span, adapter_id, container,
                                             envelope, halves) + [
            (f"{ATTR_PREFIX}adapter_id", adapter_id),
            (f"{ATTR_PREFIX}session_id", session_id),
            (f"{ATTR_PREFIX}trace_id", span["trace_id"]),
            (f"{ATTR_PREFIX}span_id", span["span_id"]),
            (f"{ATTR_PREFIX}parent_span_id", parent),
        ]),
        "status": {"code": code,
                   **({"message": str(error.get("message", ""))[:1000]}
                      if code == _STATUS_ERROR and error else {})},
    }
    end = _nano(span.get("end"))
    if end and span.get("ended") is not False:
        out["endTimeUnixNano"] = end
    if parent:
        out["parentSpanId"] = hex_span_id(parent)
    if events:
        out["events"] = [{
            "timeUnixNano": _nano(e.get("ts")) or out["startTimeUnixNano"],
            "name": e.get("name") or "event",
            "attributes": _attrs([
                (f"{ATTR_PREFIX}event_name", e.get("name")),
                (f"{ATTR_PREFIX}event_id", e.get("event_id")),
                (f"{ATTR_PREFIX}attributes", e.get("attributes")),
                (f"{ATTR_PREFIX}span_id", e.get("span_id")),
                (f"{ATTR_PREFIX}ts", e.get("ts")),
                (f"{ATTR_PREFIX}observed_at", e.get("observed_at")),
                # A discrete event carries its OWN privacy state; taking the
                # enclosing span's would mislabel it (a capture=metadata event
                # sitting on a capture=full span, or the reverse).
                (f"{ATTR_PREFIX}privacy", e.get("privacy")),
                (f"{ATTR_PREFIX}source", e.get("source")),
                (f"{ATTR_PREFIX}agent_version", e.get("agent_version")),
            ]),
        } for e in events]
    return out


def _halves_from_events(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """span_id -> {"start": attrs, "end": attrs} from the RAW canonical events.

    The truth for per-half attribution lives here, not in the projection: the
    projection is one merged row per span, which is the right shape for a UI
    and the wrong shape for a lossless export.
    """
    out: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("type") not in ("span_start", "span_end"):
            continue
        span_id = event.get("span_id")
        if not span_id:
            continue
        half = "start" if event["type"] == "span_start" else "end"
        out.setdefault(span_id, {})[half] = (event.get("span") or {}).get("attributes")
    return out


def export_trace(trace: dict[str, Any],
                 raw_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One canonical trace (get_trace dict) -> one OTLP resourceSpans payload.

    `raw_events` (from `store.iter_events`) makes the export exact: without
    them the two halves' attributes are read from the merged projection, which
    cannot say which half each key came from.
    """
    halves_by_span = _halves_from_events(raw_events or [])
    adapter_id = trace.get("adapter_id") or "unknown"
    session_id = trace.get("session_id")
    trace_id = trace.get("trace_id") or "trace"
    # get_trace() lists trace_id once at the top; spans need it per row.
    spans: list[dict[str, Any]] = [
        {**s, "trace_id": s.get("trace_id") or trace_id}
        for s in (trace.get("spans") or [])]

    events = list(trace.get("events") or [])
    root: dict[str, Any] | None = None
    if events:
        root = next((s for s in spans if s.get("kind") == "task"), None)
        if root is None:
            # Events for a trace with no span to hang them on: OTLP has no
            # event-only concept, so an explicitly-marked CONTAINER span
            # carries them. Import drops the container and keeps the events.
            root = {
                "span_id": f"{trace_id}#events",
                "trace_id": trace_id,
                "parent_span_id": None,
                "kind": "chain", "name": "agenttrace/events",
                "status": "ok", "start": trace.get("start"),
                "end": trace.get("end"), "ended": True,
                "attributes": {"container": True},
                "input": None, "output": None, "error": None,
                "tokens": {}, "model": None, "synthetic": True,
            }
            spans.append(root)
    root_id = root["span_id"] if root else None

    otlp_spans = []
    for span in spans:
        # `.get('attributes') or {}` — the key can EXIST with value None
        # (an unclosed, attribute-less span, i.e. exactly a crashed run), and
        # `or {}` is the difference between exporting it and crashing on it.
        is_container = bool((span.get("attributes") or {}).get("container")) or \
            span.get("name") == "agenttrace/events"
        span_events = ([e for e in events]
                       if root_id and span["span_id"] == root_id else [])
        envelope = {
            "agent_version": next(
                (s.get("agent_version") for s in spans
                 if s.get("agent_version")), None),
            "observed_at": next(
                (s.get("observed_at") for s in spans
                 if s.get("observed_at")), None),
        }
        otlp_spans.append(_otlp_span(
            span, adapter_id, session_id, span_events, container=is_container,
            envelope=envelope, halves=halves_by_span.get(span["span_id"])))

    return {"resourceSpans": [{
        "resource": {"attributes": _attrs([
            ("service.name", f"agent-trace/{adapter_id}"),
            (f"{ATTR_PREFIX}adapter_id", adapter_id),
            (f"{ATTR_PREFIX}session_id", session_id),
        ])},
        "scopeSpans": [{
            "scope": {"name": SCOPE_NAME, "version": SCOPE_VERSION},
            "spans": otlp_spans,
        }],
    }]}


def export_traces(traces: list[dict[str, Any]],
                  events_by_trace: dict[str, list[dict[str, Any]]] | None = None
                  ) -> dict[str, Any]:
    """Merge several traces into one OTLP payload (shared resource)."""
    merged: dict[str, Any] | None = None
    events_by_trace = events_by_trace or {}
    for trace in traces:
        payload = export_trace(trace, events_by_trace.get(trace.get("trace_id")))
        if merged is None:
            merged = payload
        else:
            merged["resourceSpans"][0]["scopeSpans"][0]["spans"].extend(
                payload["resourceSpans"][0]["scopeSpans"][0]["spans"])
    return merged or {"resourceSpans": []}


# --------------------------------------------------------------------------
# import: OTLP -> canonical events
# --------------------------------------------------------------------------

def _iter_otlp_spans(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for resource_span in payload.get("resourceSpans") or []:
        resource_attrs = _read_attrs(
            (resource_span.get("resource") or {}).get("attributes"))
        for scope_span in resource_span.get("scopeSpans") or []:
            for span in scope_span.get("spans") or []:
                yield {"span": span, "resource": resource_attrs}


def _canonical_ids(attrs: dict[str, Any], span: dict[str, Any],
                   fallback_trace: str) -> dict[str, str | None]:
    """Prefer our original ids; derive from OTel ids for foreign data."""
    return {
        "trace_id": attrs.get(f"{ATTR_PREFIX}trace_id")
        or f"otlp:{span.get('traceId') or fallback_trace}",
        "span_id": attrs.get(f"{ATTR_PREFIX}span_id")
        or f"otlp:{span.get('spanId')}",
        "parent_span_id": (attrs.get(f"{ATTR_PREFIX}parent_span_id")
                           or (f"otlp:{span['parentSpanId']}"
                               if span.get("parentSpanId") else None)),
        "session_id": attrs.get(f"{ATTR_PREFIX}session_id"),
    }


def _canonical_span(attrs: dict[str, Any], span: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the canonical span body from OTel attributes + status."""
    kind = attrs.get(f"{ATTR_PREFIX}span_kind")
    if not kind:
        op = attrs.get("gen_ai.operation.name") or ""
        kind = {"chat": "llm", "execute_tool": "tool",
                "invoke_agent": "agent"}.get(op, "chain")
    code = (span.get("status") or {}).get("code", _STATUS_UNSET)
    terminal = code == _STATUS_ERROR
    # Our own exact status wins over OTLP's single error code, so
    # cancelled/interrupted survive the round trip; foreign payloads (which
    # have no agent_trace.status) fall back to the OTLP semantics.
    declared = attrs.get(f"{ATTR_PREFIX}status")
    status = (declared if declared in (*TERMINAL_STATUSES, "running") else
              "error" if terminal else
              "ok" if span.get("endTimeUnixNano") else "running")
    if attrs.get(f"{ATTR_PREFIX}ended") is False:
        status = "running"
    error = attrs.get(f"{ATTR_PREFIX}error")
    if terminal and not error:
        error = {"type": "otlp_status",
                 "message": (span.get("status") or {}).get("message") or "error"}
    attributes: dict[str, Any] = {}
    duration = attrs.get(f"{ATTR_PREFIX}attr.duration_ms")
    if duration is not None:
        attributes["duration_ms"] = duration
    for key in ("provider", "api_mode", "base_url", "cwd", "turn_id", "note",
                "exit_code"):
        value = attrs.get(f"{ATTR_PREFIX}attr.{key}")
        if value is not None:
            attributes[key] = value
    model = attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model")
    if model:
        attributes["model"] = model
    usage = {k: v for k, v in (
        ("input_tokens", attrs.get("gen_ai.usage.input_tokens")),
        ("output_tokens", attrs.get("gen_ai.usage.output_tokens")),
        ("cache_read_tokens", attrs.get("gen_ai.usage.cached_input_tokens")),
    ) if v is not None}
    if usage:
        attributes["usage"] = usage
    if attrs.get(f"{ATTR_PREFIX}synthetic"):
        attributes["synthetic"] = True
    # The flat keys above are derived (interop); a per-half bag, when present,
    # is the original and therefore wins.
    for source_key in (f"{ATTR_PREFIX}span_attributes",
                       f"{ATTR_PREFIX}attributes"):
        blob = attrs.get(source_key)
        if isinstance(blob, dict):
            attributes = {**blob, **attributes}

    return {
        "kind": kind,
        "name": span.get("name") or attrs.get("gen_ai.operation.name") or "span",
        "status": status,
        "input": attrs.get(f"{ATTR_PREFIX}input"),
        "output": attrs.get(f"{ATTR_PREFIX}output"),
        "error": error,
        "attributes": attributes or None,
    }


def import_payload(payload: dict[str, Any],
                   adapter_id: str | None = None) -> list[dict[str, Any]]:
    """OTLP JSON -> canonical v2 events (span_start/span_end/event).

    Foreign data (no `agent_trace.*`) is imported, not rejected: ids are
    derived from the OTel ids and adapter_id falls back to the resource's
    `agent_trace.adapter_id` or the caller's default.
    """
    events: list[dict[str, Any]] = []
    for item in _iter_otlp_spans(payload):
        span = item["span"]
        resource = item["resource"]
        attrs = _read_attrs(span.get("attributes"))
        resolved_adapter = (attrs.get(f"{ATTR_PREFIX}adapter_id")
                            or resource.get(f"{ATTR_PREFIX}adapter_id")
                            or adapter_id or "otlp")
        ids = _canonical_ids(attrs, span, fallback_trace=span.get("traceId") or "otlp")
        session_id = ids["session_id"] or resource.get(f"{ATTR_PREFIX}session_id")
        body = _canonical_span(attrs, span)
        common: dict[str, Any] = {
            "v": SCHEMA_V2,
            "adapter_id": resolved_adapter,
            "agent_name": resolved_adapter,
            "session_id": session_id,
            "trace_id": ids["trace_id"],
            "span_id": ids["span_id"],
            "agent_version": attrs.get(f"{ATTR_PREFIX}agent_version"),
            "privacy": attrs.get(f"{ATTR_PREFIX}privacy"),
            "source": attrs.get(f"{ATTR_PREFIX}source"),
        }
        start = _iso(span.get("startTimeUnixNano"))
        end = _iso(span.get("endTimeUnixNano"))
        # Restored verbatim for our own exports; for foreign OTLP there is no
        # such attribute and the import time is the honest answer. Each half
        # has its OWN timestamp: one OTLP span carries both.
        original_observed = attrs.get(f"{ATTR_PREFIX}observed_at")
        original_start = attrs.get(f"{ATTR_PREFIX}start_ts")
        original_end = attrs.get(f"{ATTR_PREFIX}end_ts")

        # container spans exist only to carry discrete events (see export)
        container = bool(attrs.get(f"{ATTR_PREFIX}container"))

        def _eid(etype: str, ts: str | None) -> str:
            # Canonical span identity, so importing an export of a trace we
            # already hold DEDUPS instead of duplicating it. The producer's
            # own id wins when it survives (it is exact by construction).
            return span_event_id(resolved_adapter, ids["trace_id"],
                                 ids["span_id"], etype)

        # A synthetic span is DERIVED data (the materializer invented the task
        # root to give a v1 turn a parent), not an observation. Importing it as
        # a canonical event would turn a materialization into a recorded fact
        # and make a re-import of our own export grow the archive with rows no
        # adapter ever produced. Skip the span; refresh_projections re-derives
        # it from the children, which is where it came from.
        synthetic = bool(attrs.get(f"{ATTR_PREFIX}synthetic"))

        start_missing = bool(attrs.get(f"{ATTR_PREFIX}start_missing"))

        start_attributes = attrs.get(f"{ATTR_PREFIX}start_attributes")
        end_attributes = attrs.get(f"{ATTR_PREFIX}end_attributes")

        if not container and not synthetic and not start_missing:
            start_event = {
                **common,
                "event_id": (attrs.get(f"{ATTR_PREFIX}start_event_id")
                             or _eid("span_start", start)),
                "type": "span_start",
                "ts": original_start or start,
                "observed_at": original_observed or start,
                "parent_span_id": ids["parent_span_id"],
                "span": {k: v for k, v in body.items()
                         if k in ("kind", "name", "input")}
                | {"status": "running",
                   **({"attributes": start_attributes}
                      if isinstance(start_attributes, dict)
                      else ({"attributes": body["attributes"]}
                            if body.get("attributes") else {}))},
            }
            events.append(start_event)
            if end or body["status"] in TERMINAL_STATUSES:
                end_event = {
                    **common,
                    "event_id": (attrs.get(f"{ATTR_PREFIX}end_event_id")
                                 or _eid("span_end", end or start)),
                    "type": "span_end",
                    # The parent is a property of the SPAN, so both halves
                    # report it. Omitting it on the end half made the two
                    # halves of one span differ after a round trip.
                    "parent_span_id": ids["parent_span_id"],
                    "ts": original_end or end or start,
                    "observed_at": original_observed or end or start,
                    "span": {k: v for k, v in body.items()
                             if k in ("kind", "name", "output", "error")}
                    | {"status": body["status"],
                       **({"attributes": end_attributes}
                          if isinstance(end_attributes, dict)
                          else ({"attributes": body["attributes"]}
                                if body.get("attributes") else {}))},
                }
                events.append(end_event)

        for otlp_event in span.get("events") or []:
            event_attrs = _read_attrs(otlp_event.get("attributes"))
            name = (event_attrs.get(f"{ATTR_PREFIX}event_name")
                    or otlp_event.get("name") or "event")
            ts = (event_attrs.get(f"{ATTR_PREFIX}ts")
                  or _iso(otlp_event.get("timeUnixNano")) or start)
            # The event's OWNING span was written on export; without reading it
            # back every imported event collapses onto the trace root and the
            # event->span relationship (which the UI groups by) is lost.
            owner = event_attrs.get(f"{ATTR_PREFIX}span_id")
            event: dict[str, Any] = {
                **common,
                "event_id": (event_attrs.get(f"{ATTR_PREFIX}event_id")
                             or event_event_id(resolved_adapter, ids["trace_id"],
                                               name, ts,
                                               json.dumps(event_attrs.get(
                                                   f"{ATTR_PREFIX}attributes"),
                                                   sort_keys=True,
                                                   default=str))),
                "type": "event",
                "ts": ts,
                "observed_at": (event_attrs.get(f"{ATTR_PREFIX}observed_at")
                                or ts),
                "span_id": owner or common["span_id"],
                # A discrete event carries its OWN privacy/lineage; taking the
                # enclosing span's would mislabel it.
                "privacy": event_attrs.get(f"{ATTR_PREFIX}privacy"),
                "source": event_attrs.get(f"{ATTR_PREFIX}source"),
                "agent_version": event_attrs.get(f"{ATTR_PREFIX}agent_version"),
                "event": {"name": name,
                          "attributes": event_attrs.get(f"{ATTR_PREFIX}attributes")},
            }
            events.append(event)
    return events
