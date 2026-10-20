# Agent Trace — target architecture

Local-first observability & evaluation for AI agents. Trace, debug, replay
and evaluate agents; data stays on the machine.

Six layers, each with a hard boundary (what it does NOT do matters as much):

```
L1  AGENT INTEGRATION   hermes · codex · pi · langgraph · custom · OTLP
L2  ADAPTER FRAMEWORK   contract: schema/events.schema.json + conformance kit
                        sdk:      capabilities · EventSink · registry (Python-native)
L3  DATA PIPELINE       capture-time: validate → redact → locked append (in adapter)
                        index-time:   tail → dedup → order → materialize SQLite
L4  STORAGE             JSONL = truth (append-only) · SQLite = rebuildable projection
L5  PLATFORM API        FastAPI · SSE · trace/session query · dataset · evaluation
L6  SURFACES            Web UI (optional) · CLI/TUI (default) · SDK
```

## Principles

Each is enforced in code, not merely intended. The column on the right is where
to look when a change would violate one.

| principle | enforced by |
|---|---|
| **Local-first** — no cloud, no account, no telemetry | loopback bind, `Host` allowlist, no CORS, `Origin` check, bearer token 0600 (`server/main.py`) |
| **The contract is the wire format** — not a language interface | `schema/events.schema.json` + `adapter_sdk/testing.py`; the Pi adapter is TypeScript and conforms without importing Python |
| **JSONL is truth, the index is derived** — rebuild, never repair | `core/storage/` (offset ingest, shrink/inode invalidation, `clear_archive`), `agenttrace index --full` |
| **Redact before disk** — no write-then-scrub path | `core/privacy.py`, `common/agenttrace_common.py` |
| **Capture fails open** — a lost event is acceptable, a blocked agent is not | `adapter_sdk/event_sink.py` (I/O errors counted, never raised), `append_line` |
| **Capability honesty** — `supported`/`partial`/`unsupported`/`unknown` | `adapter_sdk/capabilities.py`; a gap renders as "not captured", never as empty |
| **Evidence, not scores** — signals reported separately | `evaluation/model.py` (`Score.evidence`, `Score.confidence`), `evaluation/report.py` |
| **Fail visibly** — an empty view must explain itself | `core/coverage.py`, `/api/health.building`, the dashboard's error panel naming its route |
| **Non-invasive by construction** — no write-back into the trace stream | datasets and scores are separate tables; a recorded execution is immutable |

Two of these were reversed after review rather than followed from the start:
the OTLP export used to be "lossless" only in prose (D13/D16), and the coverage
warning used to fire on every fresh install (D22). The reasoning is in
decisions.md.

## The boundaries

| component | owns | does NOT own |
|---|---|---|
| Adapter | understanding one agent's events & lifecycle | database queries, UI logic |
| Contract | the canonical event schema + stream invariants | parsing each agent's private formats |
| Capture path | validation, redaction, reliable append | interpreting agent semantics |
| Indexer | dedup, ordering, materialization | redacting raw secrets (already done) |
| Storage | raw archive + queryable projection | agent-specific meaning |
| API | one interface for UI/CLI/SDK | binding to any agent's internals |
| Evaluation | scores, experiments | mutating historical traces |

The test of the boundary: adding a fourth agent touches L1/L2 only. If a new
adapter requires editing the dashboard, the boundary has failed.

## Two pipelines, not one

**Capture-time (inside the adapter).** Validate → redact → locked JSONL
append. Redaction lives here and only here: this is the last moment the raw
secret exists, and any design that moves it downstream creates a plaintext
copy on a queue. This path must be non-invasive — I/O failure degrades to a
dropped event, never a blocked agent.

**Index-time (a separate process).** Tail JSONL → validate → dedup on
`event_id` → order by `ts`/`sequence` → materialize SQLite → serve queries.
Crash-safe because JSONL is truth: a corrupted index is rebuilt, never
repaired. Span lifecycle edge cases are handled here, not assumed away:

* process died mid-span → start without end → materialized `incomplete`
* duplicated hook event → dedup by `event_id`
* end before start → reconciled by `span_id`, order noted, not fatal
* concurrent subagents → siblings under one parent, ordered by `ts`

## Data model in one paragraph

Session (a continuous conversation) contains Traces (one task each). A Trace
is a tree of Spans rooted at a synthetic or explicit `task` span; each Span
is a `span_start`/`span_end` pair sharing one `span_id`, with kind
`task|agent|llm|tool|chain`. Discrete occurrences (prompts, session
lifecycle, notes) are `event` lines. Everything carries `event_id`, `ts` +
`observed_at`, `adapter_id` (open string), `privacy`, and `source` pointers
back to the raw artefact. Field reference: docs/schema.md.

## What exists today (post-M4, three review rounds applied)

* `schema/events.schema.json` + `core/schema/model.py` — the v2 contract
* `core/convert/v1_to_v2.py` — the read-side bridge over the installed base
* `adapter_sdk/` — capabilities, interfaces, JSONL sink, registry, kit
* `core/storage/` — discovery, indexer (JSONL tail → dedup → materialize),
  SQLite store (events archive + spans/sessions projections, WAL,
  shrink-invalidation, synthetic task maintenance)
* `core/query.py` — the semantic layer every surface shares
* `server/` — stdlib HTTP API + SSE + the four loopback gates (D10)
* `server/static/` — the zero-build dashboard: Overview, Trace Explorer,
  Trace Detail (execution tree + waterfall + span detail), Sessions,
  Adapters (declared capabilities vs observed data, side by side)
* `evaluation/` — datasets (from-trace import, JSONL interchange), three
  evaluator kinds (deterministic / trajectory / llm-judge), experiments with
  pinned evaluator versions, playback scoring, per-evaluator evidence +
  confidence, cost via a dated pricing registry (unknown stays unknown)
* `agenttrace serve` / `agenttrace index` — the entry points

* `adapters/otlp/` — OTLP/HTTP+JSON import + export; GenAI semantic
  conventions out, `agent_trace.*` namespace for what OTel cannot express
  (span kind, session, "never closed"), exact round-trip for our own data
* `adapters/langgraph/` — LangChain/LangGraph callback handler; kwargs in,
  canonical events out, no `langchain` import
* `adapter_sdk/discovery.py` + `compatibility.py` — metadata-first discovery
  (builtin, entry points, manifests) and the verified-by test matrix
* `agenttrace adapter list|test`, `agenttrace otlp import|export`
* `test/fixtures/adapters/*.jsonl` — a conformance fixture per adapter, so
  `agenttrace adapter test <id>` works from a fresh clone

Three adversarial review rounds ran against this code (docs/decisions.md
D16–D19). Every finding they produced is a regression test in
`test/review-fixes.test.py` — including the ones the FIXES introduced, and one
the reviews missed that the end-to-end smoke test caught (event identity was
per-producer, so an OTLP round trip duplicated instead of deduping).

M5 is the coding-agent deep analysis: git diff ↔ trace association, command
and test-result analysis, safe Trace Replay behind a sandbox, efficiency
reports. See docs/decisions.md for the reasoning frozen per decision; the
adapter surface is documented in docs/adapters.md; runnable starting points
are in docs/examples.md.
