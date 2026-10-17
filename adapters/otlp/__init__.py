"""OTLP adapter — the protocol path (proposal §4.1 type 3).

Two jobs:

* **Import**: read OTLP/HTTP+JSON trace payloads (from LangGraph, the OTel
  SDK, or anything else already instrumented) and turn them into canonical
  events. `import_payload` is pure; the `otlp_import` CLI/indexer path uses it.
* **Export**: hand canonical traces to any OTel backend (Tempo, Jaeger,
  Grafana, Honeycomb) without those backends knowing about agent-trace.

No receiver socket in M4: OTLP *export* is push, so a user points their OTel
exporter at `agenttrace otlp-server` (a thin wrapper over the local HTTP
server) or simply writes JSON to a file we import. A long-running OTLP
receiver adds a listening socket and a per-request decode path for a workflow
that JSON-as-a-file already covers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from adapters.otlp.convert import (
    export_trace,
    export_traces,
    hex_span_id,
    hex_trace_id,
    import_payload,
)
from adapter_sdk.capabilities import AdapterCapabilities, Capability, SupportLevel
from adapter_sdk.interfaces import AdapterKind, BaseAdapter

__all__ = ["OtplAdapter", "export_trace", "export_traces", "hex_span_id",
           "hex_trace_id", "import_payload", "import_file"]


class OtplAdapter(BaseAdapter):
    adapter_id = "otlp"
    kind = AdapterKind.PROTOCOL

    @property
    def capabilities(self) -> AdapterCapabilities:
        caps = AdapterCapabilities()
        caps.set(Capability.LLM_CALLS, SupportLevel.PARTIAL,
                 "depends entirely on the producer's instrumentation; GenAI "
                 "semantic conventions are read when present")
        caps.set(Capability.TOOL_CALLS, SupportLevel.PARTIAL,
                 "tool spans imported when the producer emits "
                 "gen_ai.operation.name=execute_tool or agent_trace.span_kind")
        caps.set(Capability.TOKEN_USAGE, SupportLevel.PARTIAL,
                 "gen_ai.usage.* read; absent when the producer omits usage")
        caps.set(Capability.ERRORS, SupportLevel.PARTIAL,
                 "OTLP status code maps to error; error messages only when the "
                 "producer sets status.message or agent_trace.error")
        caps.set(Capability.SESSION_LIFECYCLE, SupportLevel.PARTIAL,
                 "no session concept in OTLP; reconstructed only from "
                 "agent_trace.session_id (agent-trace exports carry it)")
        caps.set(Capability.STREAMING, SupportLevel.UNSUPPORTED,
                 "OTLP batches spans; per-chunk emission is not representable")
        caps.set(Capability.REPLAY_INPUTS, SupportLevel.PARTIAL,
                 "only if the producer put request bodies in attributes "
                 "(agent_trace.input) — off by default for privacy")
        return caps


def import_file(path: str | Path, adapter_id: str | None = None
                ) -> Iterator[dict[str, Any]]:
    """Import one OTLP JSON file, a JSON array, or a JSONL stream of payloads.

    Shape detection is by parse attempt, not by prefix guessing: whole-file
    JSON first (the common case), then a JSON array, then line-by-line — a
    one-line JSONL file is valid whole-file JSON and must not be mistaken for
    a stream of one-character lines.
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        for line in text.splitlines():
            if line.strip():
                yield from import_payload(json.loads(line), adapter_id)
        return
    if isinstance(parsed, list):
        for payload in parsed:
            yield from import_payload(payload, adapter_id)
        return
    yield from import_payload(parsed, adapter_id)
