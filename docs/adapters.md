# Adapters

An adapter turns one agent's execution into canonical events. Five ship today:

```bash
agenttrace adapter list              # what exists, where it came from, what it can see
agenttrace adapter list --json       # the same, for tooling
agenttrace adapter test langgraph    # conformance kit over the shipped fixture
agenttrace adapter test ./my.jsonl --adapter=myagent
```

## What ships

| adapter | kind | integration | verified by |
|---|---|---|---|
| `hermes` | native | `pre/post_api_request`, `api_request_error`, `pre/post_llm_call`, `post_tool_call`, session start/end; deployed as a byte copy into `~/.hermes` | `test/hermes-deploy.test.py` |
| `codex` | process/hook | lifecycle hook → incremental importer over `~/.codex/sessions/**/rollout-*.jsonl` | `test/codex-import.test.py` |
| `pi` | native | TypeScript extension inside the Pi process | `test/pi-adapter.test.mjs` |
| `otlp` | protocol | OTLP/HTTP+JSON import **and** export (GenAI semantic conventions) | `test/otlp.test.py` |
| `langgraph` | native | LangChain callback handler (`on_llm_*`, `on_tool_*`, `on_chain_*`) | `test/langgraph.test.py` |

`agenttrace adapter list` prints the same matrix plus each adapter's
capability levels, so the CLI and this table cannot drift.

## Two integration paths for a new agent

**Already instrumented with OTel?** Do nothing. Point the agent's OTel
exporter at a file, then:

```bash
agenttrace otlp import otel-dump.json          # -> trace dirs
agenttrace otlp export --trace <trace_id>      # <- recorded traces
```

We read `gen_ai.*` semantic conventions and write them on export, so any OTel
backend can read our traces and vice versa.

**Python app with callbacks?** Use the handler:

```python
from adapters.langgraph import CallbackTraceHandler
from adapter_sdk.event_sink import JsonlEventSink

handler = CallbackTraceHandler(
    JsonlEventSink("~/.agenttrace/traces/langgraph.jsonl"),
    session_id=session_id, agent_name="langgraph")
graph.invoke(state, config={"callbacks": [handler]})
```

Nothing else is required: the handler accepts LangChain's callback kwargs as
plain dicts (no `langchain` import), pairs runs by `run_id`, and derives the
tree from `parent_run_id`.

**Anything else?** Write an adapter — see `docs/writing-an-adapter.md`.

## Capability levels are the product

Every adapter declares what it can actually see, at four levels:

- `supported` — verified: all events of this kind are captured.
- `partial` — some events, or some fields only.
- `unsupported` — verified impossible via this integration path.
- `unknown` — not yet verified. **The default, and the honest one.**

`codex`, for instance, declares `streaming: unsupported` (the rollout file is
written after completion) and `llm_calls: partial` (no raw request bodies, and
`ts` is parse time). The Integrations page shows those notes next to the
level, and Trace Detail renders "not captured — the adapter did not provide
this field" instead of an empty string. A fabricated complete-looking tree is
worse than a gap you can see.

## Third-party adapters

Two mechanisms, both metadata-first:

```jsonc
// ~/.agenttrace/adapters/myagent.json   (or $AGENTTRACE_ADAPTER_DIR)
{
  "adapter_id": "myagent",
  "module": "my_package.agent_trace_adapter",   // exposes ADAPTER or a class with adapter_id
  "integration": "VSCode extension callback",
  "agent_versions": "myagent >= 2.1",
  "capabilities": {"llm_calls": {"level": "supported"}, "streaming": {"level": "partial", "note": "chunk events dropped on backpressure"}}
}
```

or a distribution entry point in the `agent_trace.adapters` group.

**Security model**: discovery reads metadata only. It does not import the
module, does not run install scripts, and does not execute anything the
manifest declares. `load()` — reached only via an explicit command — is the
first time code runs, and `test/discovery.test.py` pins this with a manifest
whose module raises on import: it must be *listable* and *not* imported.

## When an adapter captures nothing

Two facts that look identical from the dashboard and are not: "installed" and
"capturing". A native adapter is loaded when its host process starts, so
registering it does nothing for a session already running — and a session that
ended before it was installed is gone unless the agent kept a transcript.

```bash
agenttrace adapter coverage          # sessions the agent ran vs. traces we hold
agenttrace adapter coverage --json
```

| adapter | session inventory | recovery |
|---|---|---|
| `pi` | `~/.pi/agent/sessions/**/*.jsonl` (a full transcript per session) | `python3 pi/pi_import.py` |
| `codex` | `~/.codex/sessions/**/rollout-*.jsonl` | `python3 codex/codex_import.py <rollout>` |
| `hermes` | the `sessions` table in `~/.hermes/state.db` | none: Hermes keeps no wire transcript |
| `otlp`, `langgraph` | not inventoried (the producer decides) | n/a |

The check splits untraced sessions by age against the adapter's install marker:
**after** the install is a defect and warns; **before** it is a backfill
candidate; unknown age stays silent, because a check that warns on every fresh
install is one nobody reads.

Backfilled records are marked `note="backfill=session"`, use their own
`request_id` namespace (`pi-bf-*`) and their own turn namespace (`bf-<n>`, so a
partly-live session cannot collide with its own live turns). The request side of
a backfilled turn is *rebuilt from the transcript* and says so — Pi does not
persist the wire payload it sent.

A live adapter needs the agent to be restarted (or reloaded) before it observes
anything, and a reload in some hosts leaves the previous instance attached: Pi
was observed emitting every event twice, once per instance, until the adapter
was fixed to scope its ids per run. If a stream shows doubled turns after a
reload, that is the cause.

## Compatibility

A claim without a test is `unverified`. `adapter_sdk/compatibility.py` holds
the matrix so the CLI and docs render the same truth, and each row names the
test that proves it.
