# Architecture decisions

Record of the decisions that shape the platform upgrade, with the reasoning
frozen at the time they were made. Newest first. Format: decision — context —
consequence.

## D1..D4 — the four pre-implementation decisions (from the proposal, confirmed)

**D1: Hybrid collector.** Adapters keep writing JSONL directly to disk
(capture-time path: validate → redact → locked append); a separate indexer
tails JSONL → dedup → order → materialize SQLite (index-time path).
*Context:* the Codex hook is a fresh ~50 ms process per event — a mandatory
central service would put a daemon on the critical path of every tool call,
violating the non-invasive principle. *Consequence:* the collector is never a
single point of failure; SQLite is a rebuildable projection, JSONL is truth.

**D2: Adapters built-in first, SDK opened after.** M1–M3 keep hermes/codex/pi
in-tree; the conformance kit and registry are designed so third-party adapters
can come later without core changes. *Context:* a plugin discovery mechanism
is an attack surface (auto-executing third-party install scripts) that earns
its complexity only once the contract has consumers. *Consequence:* registry
is a static Python dict in M1, a table in M2, a discovery mechanism maybe never.

**D3: JSONL + SQLite, nothing heavier.** *Context:* single-machine workload;
the existing `cli/trace_index.py` already proves the append-only-projection
pattern at ~1 GB scale. ClickHouse/Kafka/Redis buy nothing here and cost the
local-first promise. *Consequence:* storage layer is two files and a WAL flag.

**D4: Trace + Web UI before Evaluation.** *Context:* Evaluation reads exactly
the same canonical events; building it before the trace tree is trustworthy
would bake distrust into every score. *Consequence:* M2 = collector + API +
dashboard; M3 = datasets/evaluators/experiments.

## D5 — The contract is the wire format, not a Python interface (amendment)

`schema/events.schema.json` + the stream invariants in `adapter_sdk/testing.py`
ARE the adapter contract. The `AgentAdapter` Python protocol is a convenience
for Python-native adapters only.
*Context:* the Pi adapter is TypeScript inside the Pi process; the Hermes
plugin is deployed as a byte copy; the Codex hook is a short-lived process.
None can or should implement a Python interface — but all three can emit
JSONL. *Consequence:* conformance is checked against emitted streams, which
every language can produce; `python3 -m adapter_sdk.testing stream.jsonl`
is the certification entry point.

## D6 — stdlib-only below the server layer (amendment)

`core/` and `adapter_sdk/` import the standard library only. Pydantic/FastAPI
enter at `server/` in M2, which runs in our own process, not the host's.
*Context:* adapters execute inside host processes we don't control; the
deployed Hermes plugin is a single copied file. Any third-party import in the
shared layers is a version-conflict bomb inside someone else's runtime.
*Consequence:* validation is hand-rolled (validation-as-data, returning
violation lists), matching the existing `validate_record` style; the JSON
Schema file remains the authoritative definition, pinned by tests.

## D7 — Redaction happens at capture time only (amendment)

Redaction is the adapter's job, before the event touches disk. The index-time
pipeline may re-scan defensively, but the architecture never routes raw
secrets through a queue or collector process.
*Context:* the proposal's pipeline diagram placed redaction in the collector;
that would create a raw-secret copy on whatever transport feeds the collector.
The existing v1 design (redact in `agenttrace_common` before append) is
correct and is kept. *Consequence:* `privacy.capture` / `privacy.redacted`
on each event record what was done, so readers distinguish "empty" from
"withheld".

## D8 — Web UI is an optional layer (amendment)

The dashboard is served by the M2 server as a static bundle; node is a
build-time dependency, never a runtime one. CLI/TUI remain the
zero-dependency default path. *Context:* today's users run `agenttrace ls`
over SSH; requiring a browser build to read traces would be a regression.
*Consequence:* `agenttrace` CLI works with no web assets installed.

## D9 — The M2 server is stdlib-only; the dashboard is zero-build (amendment)

`server/` uses `http.server.ThreadingHTTPServer`, and `server/static/` is
three hand-written files (HTML/CSS/vanilla JS) — no FastAPI, no React, no
build step. *Context:* the repo installs by symlink from a bare clone with no
packaging step (see the pyproject.toml header comment). `pip install fastapi
uvicorn` or `npm run build` would make the dashboard the first component that
cannot run from a checkout. The proposal's FastAPI+React choice assumed a
packaging story that does not exist yet. The API surface (routes + JSON
shapes, pinned by test/server.test.py) is the durable part: FastAPI or React
can re-expose/re-implement it later without breaking clients — the query
semantics live in core/query.py, not in the transport. *Consequence:* the
dashboard works offline, from a clone, forever; a React rewrite is a drop-in
static-bundle replacement against the same API.

## D10 — Loopback does not mean trusted: the server's four gates

1. **Host allowlist** {localhost, 127.0.0.1, [::1]} — DNS-rebinding defense.
2. **No CORS headers, ever** — a cross-origin page can fire requests but
   cannot read responses.
3. **Origin check** when present — must be loopback.
4. **Bearer token** on /api/* — generated to ~/.agenttrace/server.token
   (0600), printed at startup, injected into the served index.html.
*Context:* the dashboard exposes prompts, tool arguments and command output;
"it only binds localhost" is exactly the assumption that makes local services
readable by any website via rebinding or CORS mistakes. *Consequence:*
test/server.test.py exercises each gate as first-class behavior.

## D11 — Playback scoring; the trace's model is the billing truth

M3 experiments score RECORDED traces (Trace Playback), never re-run the agent
— real Replay needs sandboxes + mock tools (M5). Cost is computed from the
model the TRACE recorded, with the experiment's declared model only as
fallback; otherwise two experiments with different declarations over the same
traces would produce different "bills" for identical work. Models without a
configured price report tokens and `cost_unknown` — an estimated price
masquerading as a bill is worse than none (proposal §6.4). The judge's own
LLM call is deliberately not traced (recording it would recurse).

## D12 — Evaluator scores carry evidence + confidence, never a composite

Every evaluator returns `score + passed + evidence[] + confidence`; there is
no merged "agent quality score" (proposal §8.2: show raw signals and trends
first). Trajectory readings are explicitly defeasible — a retry loop CAN be
the correct recovery — so `passed=false` from `traj:*` means "go look at this
evidence", not "this execution was wrong". An unconfigured/unreachable judge
returns confidence=0 with `error` set: absence of signal, not a fake verdict.

## D13 — OTLP is a bridge, not the data model

`schema/events.schema.json` stays authoritative; OTLP is an import/export
format. Mapping rules: standard `gen_ai.*` semantic conventions go out (so any
OTel backend reads us), and agent-trace semantics OTel cannot express — span
kind, session, "started but never ended", synthetic roots, privacy state —
travel in an `agent_trace.*` attribute namespace. Export is therefore not
lossy for the fields the canonical model defines AND the projection carries:
ids, kind, name, exact status (a separate `agent_trace.status` attribute,
because OTLP has one error code and would otherwise turn `cancelled` into
`error`), tokens, model, input/output/error, privacy, source, agent_version,
and event-to-span ownership. `test/otlp.test.py` round-trips a trace and
asserts each of those survives; re-importing the same payload dedups by
`event_id`.
Foreign OTLP (no `agent_trace.*`) is imported rather than rejected, with ids
derived from the OTel ids — readable, and visibly missing what OTLP has no
concept for. No protobuf dependency: OTLP/HTTP+JSON is the wire format, and
stdlib-only still holds below the server layer (D6).

## D14 — Discovery is metadata-first; loading is a separate, explicit act

`adapter_sdk/discovery.py` reads three sources — the in-tree registry,
`agent_trace.adapters` entry-point metadata, and `~/.agenttrace/adapters/*.json`
manifests — and imports nothing. `load()` is a distinct call, reachable only
from a command the user typed. The alternative (auto-import anything
discoverable) makes "I installed a package" silently equivalent to "I ran
their code". `test/discovery.test.py` pins this with a manifest whose module
raises on import: listing must succeed, loading must fail loudly, and the
module must not appear in `sys.modules` after discovery.

## D15 — `partial` beats a fabricated tree (capability levels, enforced)

M4 adds otlp and langgraph, and with them the first adapters whose limits are
structural rather than temporary: OTLP cannot represent streaming, langgraph
cannot know a session it was not told about. Both declare `partial`/
`unsupported` with a note, and the conformance kit cross-checks declared
levels against the emitted stream — a stream containing llm spans while
`llm_calls: unsupported` is a VIOLATION. `adapter_sdk/compatibility.py` holds
the verified-by-test matrix so a capability claim cannot exist without the
test that proves it.

## D16 — The M4 reviews' findings, and what changed

Two opencode agents reviewed M1–M4 (one architectural, one adversarial against
five specific claims). Four of the five claims were BROKEN. Every finding is
now a regression test in `test/review-fixes.test.py`, because each was a
silent falsehood rather than a crash:

| finding | change |
|---|---|
| OTLP export crashed on `span.attributes = None` — i.e. on exactly a crashed/unclosed span | `(span.get("attributes") or {})` |
| `cancelled`/`interrupted` came back as `error` (OTLP has one error code) | exact status travels in `agent_trace.status` |
| an event's owning span was lost on round trip | read `agent_trace.span_id` back |
| `privacy`/`source`/`agent_version` were dropped: the projection never stored them | new span columns + additive migration (DB v3) |
| an unconfigured judge (confidence=0) flipped runs to `error` and `pass_rate` to 0 | `_counts_toward_verdict`: no signal, no vote |
| the v2 sink never redacted — D7 was true only of the legacy path | `core/privacy.py`; `JsonlEventSink` redacts by default |
| a shrink orphaned deduped events forever | rewind every surviving file's offset and re-read |
| two v1 records in one millisecond shared an `event_id` and one was dropped | identity includes a content digest |
| `overview.per_agent` counted synthetic roots, contradicting its own totals | `AND synthetic = 0` |
| `since` was spliced into `WHERE`, clamping a matched trace's duration/tokens | filter the aggregate, not the rows |
| a synthetic task row (stored `ended=1`) swallowed the real span_end's error | a terminal incoming status always wins |
| a native task `span_start` could not displace the synthetic row | the update adopts kind/name and clears `synthetic` |
| event-only turns were unreachable in every surface | `refresh_projections` roots also come from events |
| the conformance kit said PASS on 4 violation classes | half-vs-half identity, cross-trace parents, metadata privacy in event/span **attributes** |
| capability declarations overstated codex `token_usage`/`session_lifecycle`, pi `errors`, langgraph `subagents` | declarations corrected; codex now actually projects `token_count` into `usage` |
| a mixed-model trace was billed entirely at the first model's price ($5.00 vs $3.75) | per-model cost summed; one unpriced model → the total is unknown |
| discovery hung forever on a FIFO manifest | regular-file + size guard |
| IPv6 loopback `Host` rejected; token compared with `==` | proper `Host` parsing; `hmac.compare_digest` |

The half-vs-half check immediately caught the langgraph adapter emitting
`kind=task`/`name="bash"` on start and `kind=chain`/`name="tool"` on end — a
real defect the reviews had not found, now fixed by remembering the start's
identity per `run_id`.

Two claims HELD and are now pinned too: discovery never imports discovered
code (only an explicit `load()` does), and the four loopback gates cover every
data route including SSE and static files.

## D17 — The re-review's findings: my fixes had introduced new bugs

Both reviewers were asked to also attack the FIXES, and both found real
defects that the first round of fixes had created. Recorded because the
pattern matters more than the individual bugs: a fix applied under review
pressure is as likely to be wrong as the original code, and only a second
pass over the *fix* catches it.

| new finding | change |
|---|---|
| `spans` was keyed on `span_id` alone, but span ids are only unique within a trace (`llm:<request_id>`) — two sessions' spans merged and one trace's error overwrote the other | key is `(trace_id, span_id)`; SQLite cannot alter a PK, so a versioned table rebuild carries existing rows |
| OTLP collapsed per-span `privacy`/`source`/`agent_version` into one trace-level envelope → a `capture=full` span came back labelled `metadata` (a mislabel, not just a loss) | emitted per span; the full attribute bag also round-trips now, so custom keys survive |
| a terminal `span_end` with an EARLIER ts than the end already stored overwrote it, leaving `end_ts=10:00:05` with an error from `10:00:02` | a terminal status is accepted only when it is at least as recent |
| the shrink-rewind skipped files already visited earlier in the same pass, so a deduped event stayed missing for one full run | second pass over the file set when anything was invalidated |
| `all()` over an empty filtered selection is True → a run where NO evaluator had signal reported `ok` with `pass_rate` 1.0 | `no_signal`/`unscored` run states; `pass_rate` is `None` when nothing was measured; per-evaluator table covers every run and reports `no_signal` |
| a span that spent tokens but recorded no model made the per-model sum under-report instead of unknown | any token-bearing span without a model → cost unknown |
| trace-level tokens were `None`-safe but summed with `or 0`, so the trace-token budget fix never fired | `get_trace` reports `None` when no span captured usage |
| "no failed spans" / "no retry loops" passed confidently on a trace with nothing in it | `TraceView.has_activity`; absent data scores confidence 0 |
| `refresh_projections`'s `trace_id NOT IN (SELECT span_id FROM spans)` was global, hiding an event-only trace whose id collided | correlated `NOT EXISTS`, scoped to the trace |
| the new half-vs-half check only ran when `span_start` came first — order-dependent, so it PASSed reversed streams | moved to the post-pass |
| key-based redaction was applied to `input`/`output` but not `attributes`, so `attributes.api_key` leaked while `input.api_key` was scrubbed | attributes go through the key-aware path; `token` is treated as ambiguous (counter vs credential) |
| a non-dict `privacy` crashed `redact_event` outside the sink's error handling — in the host's callback path | `isinstance` guard + whole-function fallback that returns the event unchanged |
| `agenttrace otlp import` wrote with raw `json.dumps`, bypassing the sink entirely (D16 claimed otherwise) | goes through `JsonlEventSink` |
| same `event_id` with a different payload was dropped silently | counted as `duplicate_conflicts` |

Also fixed: the `spans` table's `WHERE span_id = ?` lookups now carry `trace_id`,
`span_end` on a synthetic row no longer swallows the real failure, `localhost.`
is accepted, the sink's docstring matches its actual redact→validate order, and
dead code was removed.

## D18 — Round 3: the second round of fixes also had bugs

Asked to attack the D17 fixes, the reviewer broke them again. The list is
short and the lesson is the same one D17 records: **a fix needs its own
review, and a review that only says "confirm the fix" is not a review.**

| new finding | change |
|---|---|
| the conformance kit still keyed spans by `span_id` alone, so a start in one trace paired with an end in another and reported PASS — with the mismatch reported twice (an inline check plus the post-pass) | keys are `(trace_id, span_id)`; the inline duplicate check is gone |
| `invalidate_file` rewinds every offset, so a SECOND file's shrink in the same pass compared its new size against 0, looked unchanged, and kept its stale events forever | compare against the file's last observed size (`ingest_files.size`), not the rewound offset |
| `cost_usd` computed `cached*(cached_input-input)` unconditionally → `cache_read > input` produced a NEGATIVE bill; and no tokens at all produced a confident `$0.00` | only apply the cached delta when the subset relation holds; clamp tokens at ≥0; no token figures ⇒ `None` |
| the spans migration crashed on `trace_id IS NULL` (NOT NULL in the new table), making the database unopenable | `COALESCE(trace_id, span_id)` in the rebuild |
| the migration silently dropped three indexes (`CREATE INDEX IF NOT EXISTS` saw the old names, then `DROP TABLE` took them) | drop the old index names before rebuilding |
| `summary` summed cost over all runs but judged `cost_unknown` over verdict-only runs, so an experiment whose only scored run was unpriced reported a known cost | coverage judged over the same set as the total |
| the dashboard rendered `no_signal` as the literal word "error" (a regex rewrote the class but not the text) — the renderer negated the fix | `badge(status)`; neutral styling for no_signal/unscored/no_trace |
| key-aware redaction destroyed `key: customer_profile_42` (a KV tool's operand) and `auth: oauth` (a scheme name) | `key`/`auth` are value-shape decisions; genuine token shapes (`sk-`, `ghp_`, `AKIA…`) are matched by their own patterns regardless of key |
| an all-alphabetic secret under `token` was not matched (the heuristic demanded a digit) | credentialish = separator-free alnum, ≥12 chars, alphabetic and single-case or mixed |
| `get_trace`'s new `has_usage` test ignored `cache_read`, so a cache-only trace reported all-None tokens while holding the number | `cache_read` included |
| `core/privacy.py`'s public `redact()` was the value-only walker while the sink used a private key-aware one — the split that let `input.api_key` be scrubbed and `attributes.api_key` leak | one key-aware path; the public name is the safe one |

Final state: `test/review-fixes.test.py` holds 77 assertions, every one of them
a counterexample a reviewer actually produced across the three rounds. The
full suite (20 files) passes.

## D19 — Event identity is canonical, not per-producer

Found by the end-to-end smoke test, not by a reviewer: exporting a trace to
OTLP and importing it back into the same trace directory DUPLICATED it. The v1
converter minted ids as `f("v1", …)` and the OTLP importer as `f("otlp", …)`,
so the same span had two identities depending on who serialized it.

`core/schema/model.py` now owns both:
`span_event_id(adapter, trace_id, span_id, half)` and
`event_event_id(adapter, trace_id, name, ts, content)`. Every producer uses
them, so importing a trace we already hold dedups to zero new events —
verified across paths, not just OTLP→OTLP as the earlier test did.

The same run surfaced a second, subtler problem: `refresh_projections` derives
the `task` root for a v1 turn, and export faithfully wrote it out — so an
import turned DERIVED data into a recorded observation, and the archive grew
with rows no adapter ever produced. The importer now skips spans marked
`agent_trace.synthetic`; the materializer re-derives the root from the
children, which is where it came from. Export still includes it, so an OTel
backend still renders a complete trace.

Lesson recorded: **the smoke test is part of the review.** Three adversarial
rounds did not find this, because each round tested its own claim rather than
the round trip through the real CLI.

## D20 — Round 4: running the pipeline over REAL traces from all three agents

The three adversarial rounds all worked on fixtures. Running the full pipeline
over the machine's real traces (8 codex rollouts, real pi sessions, real hermes
sessions — 123 traces / 3677 events) found eleven more defects. None of them
were reachable from synthetic input, because each needed a shape that only a
real agent produces.

| finding | change |
|---|---|
| **pi never wrote `request_id` or `turn_id`.** `agenttrace show <request_id>` could not match a pi call at all, the panel had nothing to pair, and the v2 converter minted two unrelated spans (an unclosed start + an orphan end) that the conformance kit rejected | the adapter synthesises `request_id = pi-<turnIndex>-<seq>` and threads `turn_id` through every record; a session now splits into per-turn traces, which is what "Trace: one task execution" always meant |
| **pi dropped tool results entirely** — `tool_execution_end.result` was never read, so the panel showed `⚙ bash ok` and nothing about what the command printed | result body captured (content blocks + `{output}` fallbacks) |
| **`exit_code` was declared in the v2 schema, read by the OTLP exporter, and populated by nobody** | hermes and pi now emit `tool.exit_code`; the converter moves it to `span.attributes.exit_code` |
| **codex `token_count` produced 28 same-id/different-payload collisions** on one real rollout: every reading in a turn shared `codex-tokens-<turn>` | each turn now gets a real request/response PAIR; readings accumulate onto that turn's response (last wins), and a turn that never reported `task_complete` still gets its usage plus an explicit `turn_incomplete` note |
| codex's llm spans never closed (no request to pair with) and its usage reached nothing | 62 of 66 real codex llm spans now close with tokens projected |
| **the task root said `ok` while its own trace said `incomplete`** | a root whose child never closed is `interrupted` (a terminal state the schema already had) |
| `_TRACE_SELECT` let a synthetic root vote in the trace's own error aggregate — the root being `interrupted` flipped the trace from `incomplete` to `error` | synthetic rows excluded, same rule as `overview`/`per_agent` |
| **an end-only span round-tripped into a start+end pair**, inventing an event on every import | `agent_trace.start_missing`; the importer skips the start |
| **a nested JSON `null` came back as the string `"null"`** — a falsy absence turned into a truthy string | OTLP cannot carry null, so the key is omitted |
| **per-half attributes, per-half timestamps and event-level `privacy`/`source` were dropped on export** (the projection merges a span's two halves into one row — right for a UI, lossy for an interchange) | export reads the RAW events; `agent_trace.start_attributes`/`end_attributes` and per-event envelope fields round-trip exactly |
| the indexer called routine dedup "rejected": 28 dedups on real data read as 28 broken events, and a genuine id conflict was invisible | `events_added` / `events_deduped` / `event_id_conflicts` / `events_invalid`, plus `ScanReport.problems`; the conflict rule compares IDENTITY, not payload bytes |

Also fixed, found along the way: `timeout` is GNU coreutils and absent on stock
macOS, so `install.sh` silently skipped enabling the hermes plugin and
`e2e-pi.sh` reported "pi exited non-zero" for a pi that never ran; and both e2e
scripts' record-count gate printed "0\n0" on an empty directory, so the
comparison erred out and stopped gating anything.

**The lesson, now recorded twice:** a test suite that passes on fixtures proves
the fixtures. The e2e tests were SKIPPED on this machine for their entire
existence, which is why three review rounds never saw any of this.
