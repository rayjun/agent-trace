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
