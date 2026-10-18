# Record format changelog

`schema/trace.schema.json` is the contract every adapter writes to and every
reader consumes. This file records *why* each rule exists, so a future change
does not quietly undo one.

## How versions work

Each record carries a `v` field (`SCHEMA_V` in `common/agenttrace_common.py`,
`"v"` in the schema).

* Adapters stamp every record with the current `v`.
* Readers skip records whose `v` they do not know (`iter_records` checks
  `rec.get("v") == SCHEMA_V`), so an old reader never misparses a newer file.
* **Bump `v` only for an incompatible change** — removing a field, changing a
  field's type, or changing what a field *means* in a way old records cannot
  satisfy. Adding an optional field does not need a bump: the schema allows
  optional properties, and readers must already ignore unknown keys.

`test/schema.test.py` asserts that the Python constants and the JSON schema
agree, so the two definitions of `v`/`agent`/`event` cannot drift.

---

## v2 — canonical events (parallel introduction)

`schema/events.schema.json` defines the v2 canonical event: Session > Trace >
Span > Event, emitted as `span_start` / `span_end` / `event` lines. It runs IN
PARALLEL with v1 — the three adapters still write v1, and
`core/convert/v1_to_v2.py` is the read-side bridge. v1 stays the adapter write
format until the adapters migrate in a later milestone; nothing about v1
above is weakened.

Design decisions that differ from v1, and why:

* `adapter_id` is an open string (`^[a-z][a-z0-9_-]*$`), not an enum. v1's
  `agent` enum meant adding an agent required editing the schema — the exact
  coupling the platform is meant to remove.
* A Span is two events sharing one `span_id`, not one record with both sides.
  v1's single-record shape cannot represent a call that started and never
  returned (the crash case); the pair can, and a start without an end is
  materialized as `incomplete` — a state adapters never emit themselves.
* `event_id` is mandatory and, for re-emittable sources, deterministic
  (`derive_event_id` over source identity), so re-ingestion dedups.
* `ts` (when it happened) and `observed_at` (when the adapter wrote it) are
  separate fields. File-parsing adapters (codex) only have the second; v1
  conflated them and made parse-time look like call-time.
* Agent-specific fields go in `span.attributes` / `event.attributes`, which
  are `additionalProperties: true`. The core envelope stays
  `additionalProperties: false`, same strictness as v1.

`test/v2_schema.test.py` pins `core/schema/model.py` against
`schema/events.schema.json`, the same mechanism `test/schema.test.py` uses
for v1.

---

## v1 — current

The initial (and only) version. Field reference lives in
`schema/trace.schema.json`; the invariants enforced at runtime live in
`common/agenttrace_common.py::validate_record`.

### v1 clarifications

These do not change the schema — they pin down what fields already meant,
after each reading turned out to be ambiguous in practice.

#### `input_tokens` is the TOTAL prompt, cache included

Two conventions existed in the wild:

| writer | value written | source |
|---|---|---|
| hermes | `prompt_tokens` — includes cached tokens | OpenAI/OpenRouter `usage` |
| pi (before the fix) | `promptTokens - cacheRead - cacheWrite` — excludes them | pi-ai's `usage.input` |

`cache_read_tokens` is defined as a **subset** of `input_tokens`. With the
second reading it was not: 157 of 2050 records on one machine (7.7%, all from
`pi`) had `cache_read > input`, the worst single record 420 vs 768 (182.9%),
and the per-session cache hit rate rendered at 2703.86%.

Fixed at the writer (`pi/agent-trace.ts` sums the cache back in) **and** at the
reader (`total_input_tokens` repairs records already on disk, which cannot be
rewritten; `cache_hit_rate` clamps the rendered rate to 0-100).

`validate_record` reports `cache_read_tokens > input_tokens` on any *new*
record, so reintroducing the exclusive reading fails loudly instead of
producing another 2703% figure.

#### The system prompt is stored once

Where the system prompt lives was never agreed across adapters:

| adapter | before | after |
|---|---|---|
| hermes | `messages[0]` **and** a separate `system_prompt` field | `messages[0]` only; the field is `null` |
| codex | `system_prompt` **and** `instructions`, always the same string | `system_prompt` only |
| pi | `messages` only | unchanged (0/351 records duplicated) |

1812/1813 hermes records carried two byte-identical copies — 2.4 MB of
duplicate prompt inside one profile's 105 MB of traces — and `agenttrace show`
printed the same multi-KB block twice under two headings, which reads as two
different prompts.

Both fields stay in the schema and stay optional: a record writes
`system_prompt` **when `messages` does not already carry it**, which is the
codex shape and the shape any future adapter may produce. Readers must
therefore keep their fallback chain (`system_prompt` -> `instructions` ->
scan `messages` for `role: system`), because records written before this change
and records written after it both exist on disk. `instructions` remains valid
for an adapter that only knows the wire name; codex stopped writing it purely
because it was assigned the identical string.

No `v` bump: neither field is required, and removing a duplicate cannot make a
new record unreadable to an old reader.

#### `duration_ms` is an integer count of milliseconds

Hermes hands its hooks `api_duration` as a **float number of seconds**. Writing
it through made every real record violate the schema's integer type and made
the panel show `2ms` for a 2-second call.

The unit is part of the field name and is not negotiable: convert at the edge
(`_ms` for seconds, `_ms_int` for values already in milliseconds — running one
through the other inflates tool timings by 1000× or truncates them to 0).

#### A trace split across files gets ONE task span

`convert_file` synthesizes a trace's task span from its children, per file. An
agent's daily files split one session across days, so the same trace produced
one task span per file — same `span_id`, different bounds, which the
conformance kit correctly rejected as a duplicate. `convert_stream(paths)`
converts many files as one stream and synthesizes each trace once; that is the
form any stream-level consumer should use. `convert_file` keeps its per-file
behaviour and documents the difference.

#### Backfilled turns are namespaced `bf-<n>`

The live adapter numbers turns by provider turn and a backfill numbers them by
user message, so both start at 0 for the same session. The trace key is
(adapter, session, turn), so an unnamespaced backfill met the live records in
one `trace_id` — two task roots and one span id twice. Backfilled turns carry a
`bf-` prefix, which also makes the provenance legible in a trace name.

#### `tool.provider_call_id` is added, and `call_id` is scoped

A provider's tool-call ids are unique within ONE API response, not within a
session or even a turn: one real Pi turn contained `call_319025` twice (two
separate provider calls), so two tool spans derived from `tool:<call_id>`
shared an id and the v2 conformance kit rejected the stream. Writers now scope
the pairing id by the request that produced it (`pi-0-1:call_319025`), which is
uniqueness by construction, and keep the provider's own id in the new optional
`tool.provider_call_id` so it stays greppable.

#### `tool.exit_code` is populated (it used to be declared and empty)

`span.attributes.exit_code` is documented as the tool convention in
`events.schema.json`, and `adapters/otlp/convert.py` reads it on export — but
no adapter ever SET it, so the field was permanently absent and the OTLP
attribute never appeared. `tool.exit_code` is now an optional integer in this
schema, written by hermes (from the terminal tool's `{"exit_code": ...}`) and
by pi (from `structuredContent.exit_code`), and moved into
`span.attributes.exit_code` by the v1 -> v2 converter.

#### pi records carry `turn_id` and `request_id` (they had neither)

`request_id` is documented as "stable id correlating an llm_request with its
llm_response/llm_error". The pi adapter emitted neither that nor `turn_id`, so
`agenttrace show <request_id>` could never match a pi call, the live panel had
nothing to pair on, and the v2 converter produced an orphan `span_end`
alongside an unclosed `span_start` — which the conformance kit rejected.
Pi's extension API does not supply either value (`turn_start` has an index;
the provider's response id only exists after the call), so the adapter
synthesises `request_id = pi-<turnIndex>-<seq>` and sets
`turn_id = <turnIndex>`. A session therefore also splits into one trace per
turn, which is what `Trace: one task execution` was always supposed to mean.

#### `ts` is RFC3339 UTC with millisecond precision

`2026-09-30T10:00:00.000Z`, produced by `now_iso()`. Readers string-compare
`ts` for `--since`, for newest-record selection and for session ordering — a
local-time or variable-precision stamp silently breaks all three.

---

## Adding a field

1. Add it to `schema/trace.schema.json` as an **optional** property with an
   explicit type (and `null` in the union if it can be absent-but-present).
2. Emit it from the adapters that can know it; leave it out otherwise. A
   missing key and `null` are both valid — `emit_record` drops `null`, so a
   field that is "not known" should simply not be set.
3. If a reader aggregates it, add it to `PROJECTED` in `cli/trace_index.py`,
   otherwise the index will not carry it and the query falls back to a body
   scan. Bump `INDEX_VERSION` in the same change — a projection change with an
   unchanged version replays old rows that lack the new field.
4. Cover it in `test/schema.test.py`.

## Removing or retyping a field

1. Bump `v` in `common/agenttrace_common.py` **and** `"v"` in the schema
   together (`test/schema.test.py` fails if they disagree).
2. Readers must skip the old `v` rather than guess — that check is already in
   `iter_records`; do not weaken it.
3. There is no migration tooling. Traces are a local debugging artefact, not a
   database: the honest answer to "read old files" is a version bump plus a
   reader that understands both, or a note in the release that old files are
   best read with the old CLI.
