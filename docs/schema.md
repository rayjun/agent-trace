# Canonical event schema (v2)

Authoritative definition: `schema/events.schema.json`.
Python mirror: `core/schema/model.py` (pinned by `test/v2_schema.test.py`).

One event = one JSONL line. Three event types:

| `type` | meaning | required beyond envelope |
|---|---|---|
| `span_start` | an operation began | `trace_id`, `span_id`, `span.kind`, `span.name` |
| `span_end` | it reached a terminal state | same, plus `span.status` in `ok/error/cancelled/interrupted` |
| `event` | discrete occurrence | `event.name` |

A Span is the pair of `span_start`+`span_end` sharing one `span_id`. A Trace
is the set of spans sharing one `trace_id`, rooted at a `task`-kind span
(`parent_span_id: null`). A Session (`session_id`) groups traces.

## Envelope fields

| field | type | notes |
|---|---|---|
| `v` | const 2 | readers skip unknown versions, same rule as v1 |
| `event_id` | string ≥8 | unique; deterministic for re-emittable sources |
| `ts` | RFC3339 ms UTC | when it happened (adapter's best knowledge) |
| `observed_at` | RFC3339 ms UTC | when the adapter wrote it; gap = derived data |
| `sequence` | int? | source ordering hint when the agent has one |
| `adapter_id` | slug | OPEN string — adding an adapter never edits the schema |
| `agent_name` / `agent_version` | string? | the observed product |
| `resource` | object? | pid, cwd, runtime, host |
| `privacy` | object? | `capture: full/metadata`, `redacted: bool` |
| `source` | object? | pointer back: source event id, file, line |

## Span payload

`kind`: `task` (trace root) · `agent` (subagent delegation) · `llm` (one
model call) · `tool` (one tool execution) · `chain` (generic grouping).

`input` on start, `output`+`error`+terminal `status` on end. Everything
agent-specific goes in `attributes` — the controlled extension point:

| span kind | conventional attributes |
|---|---|
| llm | `provider`, `model`, `api_mode`, `base_url`, `usage{input_tokens,output_tokens,cache_read_tokens,...}`, `duration_ms` |
| tool | `exit_code`, `duration_ms`, retry metadata |
| agent | child task id, delegation mode |
| task | `turn_id`, `synthetic: true` when inferred rather than observed |

## States

* `running` — legal on `span_start` only.
* `ok` / `error` / `cancelled` / `interrupted` — the terminal set.
* `incomplete` — **never on the wire, and never stored.** An adapter emitting
  it fails validation. A span with no end is stored as `running`; the read
  layer (`core/query.py`) presents it as `incomplete`. Deriving it at read
  time is what lets a late `span_end` self-heal the display with no rewrite —
  so a consumer reading the `spans` table directly sees `running`, and must
  not distinguish "still live" from "crashed" without checking `ended`.

## Lifecycle cases the model admits

| case | representation |
|---|---|
| crash mid-span | unclosed `span_start` → presented `incomplete` (stored `running`) |
| duplicate hook fire | same `event_id`, identical payload → deduped |
| end arrives before start | reconciled by `span_id`; warned, not rejected |
| concurrent subagents | sibling spans, ordered by `ts` |
| retry | one span per attempt; retry count in `error.retry_count` |

## v1 → v2 mapping (what core/convert/v1_to_v2.py does)

| v1 record | v2 |
|---|---|
| `session_start` / `session_end` | `event`, session-level (`trace_id: null`) |
| `user_prompt` / `assistant_message` / `note` | `event` on the turn's trace |
| `llm_request` + `llm_response`/`llm_error` (via `request_id`) | one `llm` span |
| `tool_call` + `tool_result` (via `tool.call_id`) | one `tool` span |
| each `(session_id, turn_id)` group | synthesized `task` root span |

Conversion is deterministic (`derive_event_id` over source identity) so
re-running it dedups downstream. Fields with no v2 home keep their v1 names
inside `attributes`. Timestamps the source never had are not invented — the
codex adapter's parse-time `ts` shows up as `ts == observed_at`, which is
honest and visible.
