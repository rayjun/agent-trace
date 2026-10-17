"""The canonical v2 event model — Python mirror of schema/events.schema.json.

Why both exist: the JSON Schema file is the *contract* (language-agnostic —
the Pi adapter is TypeScript and will never import this file); this module is
the *ergonomics* for the Python platform layers (converter, indexer, server).
test/v2_schema.test.py asserts the enums and constants here agree with the
JSON file, the same way test/schema.test.py pins the v1 constants.

Design rules carried over from v1 (schema/CHANGELOG.md):

* Validation is data, not exceptions. `validate_event` returns a list of
  violations so the indexer can quarantine one bad line and keep going —
  a collector that dies on the first corrupt record loses everything after it.
* `incomplete` is never emitted by an adapter. It is assigned by the
  materializer to a span_start that saw no span_end, which is how a crashed
  agent shows up honestly instead of as a span that is still "running" forever.
* ts is RFC3339 UTC with milliseconds, string-comparable, exactly as v1.

Stdlib only — see core/__init__.py.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_V2 = 2

#: The `type` field. span_start + span_end sharing one span_id form a Span;
#: "event" is a discrete occurrence (session lifecycle, prompts, notes).
EVENT_TYPES = ("span_start", "span_end", "event")

#: Span kinds. `task` is the trace root; the rest nest under it. Deliberately
#: small — agent-specific flavour goes in span.attributes, not new kinds.
SPAN_KINDS = ("task", "agent", "llm", "tool", "chain")

#: The only non-terminal status; legal on span_start only.
SPAN_STATUSES = ("running", "ok", "error", "cancelled", "interrupted")

#: What a span_end must report. A start that never saw an end is PRESENTED as
#: "incomplete" by the read layer; the projection stores `running` so a late
#: span_end can self-heal without a rewrite. Never on the wire either way.
TERMINAL_STATUSES = ("ok", "error", "cancelled", "interrupted")

#: Names every reader should render specially. The field itself is an open
#: string — adapters may emit others (e.g. "compaction", "checkpoint").
WELL_KNOWN_EVENTS = (
    "session_start",
    "session_end",
    "user_prompt",
    "assistant_message",
    "note",
)

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_ADAPTER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def now_iso() -> str:
    """RFC3339 UTC, millisecond precision — the only ts shape readers can sort."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_event_id() -> str:
    """Random id for a live-observed event."""
    return uuid.uuid4().hex


def derive_event_id(*parts: object) -> str:
    """Deterministic id for a *re-emittable* event.

    Importers (codex rollout parsing, the v1 converter) must produce the same
    id every time they see the same source record, so re-ingestion dedups on
    event_id instead of doubling the data. 32 hex chars of sha1 is collision-
    safe at local scale and keeps ids visibly shorter than the content they
    point at.
    """
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x1f")  # unit separator: ("ab","c") must not equal ("a","bc")
    return h.hexdigest()[:32]


def span_event_id(adapter_id: str, trace_id: str, span_id: str,
                  etype: str) -> str:
    """Producer-independent identity of one HALF of a span.

    Two producers observing the same span — the adapter that wrote the
    original record, and an OTLP importer re-reading an export of it — must
    mint the same event_id, or re-importing an export duplicates the trace
    instead of deduping it. Identity therefore comes from (adapter, trace,
    span, half) and never from who happened to serialize it. An id derived
    per-producer looked fine in isolation and broke exactly this round trip.
    """
    return derive_event_id("span", adapter_id, trace_id, span_id, etype)


def event_event_id(adapter_id: str, trace_id: str | None, name: str,
                   ts: str | None, content: str = "") -> str:
    """Canonical identity of a discrete event, same reasoning as above."""
    return derive_event_id("event", adapter_id, trace_id or "-", name,
                           ts or "", content)


@dataclass
class CanonicalEvent:
    """Construction helper; the wire form is a plain dict via to_dict().

    Optional fields that are None are OMITTED from the dict, matching the v1
    convention (a missing key and null are both valid; "not known" should not
    be written at all).
    """

    type: str
    adapter_id: str
    ts: str = ""
    observed_at: str = ""
    event_id: str = ""
    session_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    sequence: int | None = None
    span: dict[str, Any] | None = None
    event: dict[str, Any] | None = None
    resource: dict[str, Any] | None = None
    privacy: dict[str, Any] | None = None
    source: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ts = self.ts or now_iso()
        observed = self.observed_at or now_iso()
        d: dict[str, Any] = {
            "v": SCHEMA_V2,
            "event_id": self.event_id or new_event_id(),
            "type": self.type,
            "ts": ts,
            "observed_at": observed,
            "adapter_id": self.adapter_id,
        }
        for key in (
            "session_id", "trace_id", "span_id", "parent_span_id",
            "agent_name", "agent_version", "sequence",
            "span", "event", "resource", "privacy", "source",
        ):
            value = getattr(self, key)
            if value is not None:
                d[key] = value
        d.update(self.extra)
        return d


def validate_event(rec: dict[str, Any]) -> list[str]:
    """Check one v2 event dict; return violation strings (empty = valid).

    Covers the invariants a consumer depends on. Field-shape rules live in
    schema/events.schema.json; this repeats them in cheap Python so every
    ingest path can validate without a jsonschema dependency.
    """
    problems: list[str] = []

    if rec.get("v") != SCHEMA_V2:
        return [f"v: expected {SCHEMA_V2}, got {rec.get('v')!r}"]

    event_id = rec.get("event_id")
    if not isinstance(event_id, str) or len(event_id) < 8:
        problems.append("event_id: missing or shorter than 8 chars")

    etype = rec.get("type")
    if etype not in EVENT_TYPES:
        problems.append(f"type: {etype!r} not in {EVENT_TYPES}")

    for key in ("ts", "observed_at"):
        value = rec.get(key)
        if not isinstance(value, str) or not _TS_RE.match(value):
            problems.append(f"{key}: must be RFC3339 UTC ms (YYYY-MM-DDTHH:MM:SS.mmmZ)")

    adapter_id = rec.get("adapter_id")
    if not isinstance(adapter_id, str) or not _ADAPTER_ID_RE.match(adapter_id):
        problems.append(f"adapter_id: must match ^[a-z][a-z0-9_-]*$, got {adapter_id!r}")

    for key in ("session_id", "trace_id", "span_id", "parent_span_id"):
        value = rec.get(key)
        if value is not None and not isinstance(value, str):
            problems.append(f"{key}: must be a string or absent")

    if rec.get("parent_span_id") and rec["parent_span_id"] == rec.get("span_id"):
        problems.append("parent_span_id: a span cannot be its own parent")

    if etype in ("span_start", "span_end"):
        if not rec.get("span_id"):
            problems.append("span_id: required on span markers")
        if not rec.get("trace_id"):
            problems.append("trace_id: required on span markers")
        span = rec.get("span")
        if not isinstance(span, dict):
            problems.append("span: required on span markers")
        else:
            if span.get("kind") not in SPAN_KINDS:
                problems.append(f"span.kind: {span.get('kind')!r} not in {SPAN_KINDS}")
            if not span.get("name"):
                problems.append("span.name: required")
            status = span.get("status")
            if etype == "span_start" and status not in (None, "running"):
                problems.append(f"span.status: span_start cannot open as {status!r}")
            if etype == "span_end" and status not in TERMINAL_STATUSES:
                problems.append(
                    f"span.status: span_end must be one of {TERMINAL_STATUSES}")
            if status == "incomplete":
                problems.append(
                    "span.status: 'incomplete' is materializer-assigned, never emitted")
            if span.get("error") and status not in ("error", "interrupted", "cancelled"):
                problems.append("span.error present but status is not a failure state")

    if etype == "event":
        event = rec.get("event")
        if not isinstance(event, dict) or not event.get("name"):
            problems.append("event.name: required on type=event")

    # privacy.capture=metadata means bodies were withheld at capture time;
    # finding one anyway means an adapter is leaking past its own policy.
    # The check must cover EVERY place a body can hide — an event's
    # attributes and a span's extension attributes are as much a leak as
    # span.input, and only checking input/output made the rule decorative.
    privacy = rec.get("privacy")
    if isinstance(privacy, dict) and privacy.get("capture") == "metadata":
        for where, body in (("span", rec.get("span")),
                            ("event", rec.get("event"))):
            if not isinstance(body, dict):
                continue
            for field in ("input", "output"):
                if body.get(field):
                    problems.append(
                        f"privacy.capture=metadata but {where}.{field} carries a body")
            attrs = body.get("attributes")
            if isinstance(attrs, dict):
                leaked = [k for k in ("text", "content", "messages", "args",
                                      "result", "prompt", "input", "output")
                          if attrs.get(k)]
                if leaked:
                    problems.append(
                        f"privacy.capture=metadata but {where}.attributes carries "
                        f"{', '.join(sorted(leaked))}")

    return problems
