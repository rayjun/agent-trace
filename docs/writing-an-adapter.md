# Writing an adapter

An adapter turns one agent's execution into canonical events
(`schema/events.schema.json`) and appends them to a JSONL stream. That stream
IS the contract — your adapter can be written in any language, because the
conformance kit checks the output, not the code.

## The five rules

1. **Emit canonical events.** One JSON object per line, passing
   `validate_event` (Python) / the JSON Schema (any language).
2. **Never break the host.** I/O failures degrade to dropped events. An
   adapter that can take down the agent it observes will be uninstalled once
   and never trusted again.
3. **Redact before disk.** Your process is the last place raw prompts and
   tool arguments exist. Truncate and scrub there; record what you did in
   `privacy`.
4. **Make re-emission safe.** If your source can be re-read (log files,
   rollouts), derive `event_id` deterministically from source identity so
   re-ingestion dedups instead of doubling.
5. **Declare capabilities honestly.** `partial` and `unknown` are respected
   states; a fabricated complete tree is a bug. The UI renders "not captured"
   for what you didn't see — leave fields absent, don't write empty strings.

## Pick your shape

| shape | when | lifecycle |
|---|---|---|
| Native (plugin/extension/callback) | the agent has an internal hook API | long-lived; implement start/stop |
| Process/hook | the agent fires external commands or writes logs | short-lived; convert + append + exit |
| Protocol (OTLP/SDK) | the app already speaks a tracing protocol | receiver side is the platform's |

Python-native adapters can use the SDK directly:

```python
from adapter_sdk import BaseAdapter, Capability, SupportLevel
from adapter_sdk.event_sink import JsonlEventSink
from core.schema.model import CanonicalEvent

class MyAdapter(BaseAdapter):
    adapter_id = "myagent"
    def __init__(self, sink): self.sink = sink

    def on_llm_start(self, req):          # whatever the host calls
        self.sink.emit(CanonicalEvent(
            type="span_start", adapter_id=self.adapter_id,
            session_id=req.session, trace_id=req.trace, span_id=f"llm:{req.id}",
            span={"kind": "llm", "name": req.model, "status": "running",
                  "input": {"messages": redact(req.messages)}},
        ).to_dict())
```

`JsonlEventSink` validates on emit (a contract violation is refused and
counted, not persisted), locks across processes (flock), and never raises
into your host.

## Conformance

```bash
python3 -m adapter_sdk.testing ./your-stream.jsonl --adapter=myagent
```

The kit checks: schema validity, span lifecycle (every end has a start),
parent-child integrity, dedup safety, ordering tolerance, status/error
coherence, metadata-mode privacy, and — with `--adapter` — that your registry
declaration matches what the stream actually contains. (M2 turns this into
`agenttrace adapter test`; the checks are already final.)

Produce the test stream by pointing your adapter at a temp file and running a
small scripted scenario: one successful task, one failed tool call, one
crash. The crash matters: an unclosed `span_start` is the only honest record
of it.

## Publish it

Two ways, neither of which requires a PR to this repo:

**Manifest** (works immediately, no packaging):

```jsonc
// ~/.agenttrace/adapters/myagent.json
{
  "adapter_id": "myagent",
  "module": "my_package.agent_trace_adapter",  // exposes ADAPTER, or a class with adapter_id
  "version": "0.3.0",
  "kind": "native",
  "integration": "one line: how you hook in",
  "agent_versions": "myagent >= 2.1",
  "capabilities": {
    "llm_calls": {"level": "supported"},
    "tool_calls": {"level": "partial", "note": "args captured, exit code is not"},
    "streaming": {"level": "unsupported"}
  }
}
```

**Entry point** (for a distributed package):

```toml
[project.entry-points."agent_trace.adapters"]
myagent = "my_package.agent_trace_adapter:ADAPTER"
```

Both are **metadata-first**: discovery lists you without importing your module
or running anything. Code executes only when a user runs a command that
loads you. That is a deliberate safety property (docs/decisions.md D14), and
test/discovery.test.py enforces it.

Check your work:

```bash
agenttrace adapter list                 # you should be listed, with source=manifest
agenttrace adapter test myagent         # runs your test/fixtures-style stream
agenttrace adapter list --json          # the machine-readable form
```

## Registry

Add a static entry in `adapter_sdk/registry.py` (id, kind, integration,
verified agent versions, capabilities with notes) when the adapter lives in
this repo. For third-party adapters the manifest carries the same fields.
Either way, the note is the valuable part — "rollout files carry no
per-request clock; ts is parse time" tells the UI what to show instead of
pretending.

If your adapter claims a capability, `adapter_sdk/compatibility.py` should
name the test that proves it; an unproven claim renders as `unverified`.
