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

## D21 — Recovering what a live-only adapter cannot have seen

The Pi adapter is a live hook: it sees the exact wire payload and the exact
response, which is why it is the most complete of the three — and it can only
record turns that happen while it is loaded. It is loaded at PROCESS START, so
registering it does nothing for an agent that is already running. On this
machine that meant 53 of 63 sessions (1,877 assistant messages, 16.6M tokens)
had no traces at all, and the only signal was an empty dashboard.

Two additions, because "why is it empty" and "what did I lose" are different
problems:

**`pi/pi_import.py` — backfill from the transcript.** Pi keeps a full session
transcript, so the missing turns are recoverable: real usage, model, provider,
`durationMs`, `responseId`, tool results and per-entry timestamps are all read
verbatim. Two things are reconstructed and *said to be* reconstructed: the
request side (Pi does not persist the wire payload, so the conversation chain
up to that message is rebuilt) and turn boundaries (session entries carry no
turn markers). Backfilled records carry `note="backfill=session"` and
`request_id="pi-bf-<entry id>"`, so a reader can tell them from observed
records and the two id namespaces cannot collide. A session that already has
live traces is skipped unless `--force`; the cursor is separate
(`--reimport` rewrites).

**`core/coverage.py` — the gap reports itself.** Each adapter declares where its
agent leaves session artefacts (Pi and Codex: files; Hermes: a row in its own
`state.db`, read-only), and that inventory is compared against the sessions we
hold traces for. The comparison is split by age relative to the adapter's
install marker, because the two halves mean opposite things:

* a session AFTER the install with no traces is a defect — something is wrong
  now, and the warning names the likely cause and the recovery command;
* a session BEFORE it is expected for a new install, and is a backfill
  candidate.

Collapsing those into one number is what makes a warning get skimmed past, and
treating "age unknown" (no install marker) as a defect would make every fresh
install warn about its own history — so unknown age is its own silent bucket.
Surfaced by `agenttrace adapter list` / `adapter coverage`, in the dashboard's
Adapters page, and once at `serve` startup.

Two things this exposed that were worth fixing on their own:

* **Tool-call ids are unique within one API response, not within a session.**
  The same `call_319025` occurred twice in one real turn (two separate provider
  calls), so keying the span on it alone gave the v2 tree two spans with one
  id — the conformance kit caught it. Both the live adapter and the backfill now
  scope the id by the request that produced it (`pi-0-1:call_319025`), keeping
  the provider's raw id beside it.
* **The coverage check was reading 1.1 GB of JSONL inside a request**, once per
  adapter, which timed out the dashboard's own adapters page once the trace
  history grew. It now reads the `sessions` projection the indexer already
  maintains (8.5 ms instead of ~16 s), with a single-pass file scan as the
  no-index fallback.

## D22 — Three review rounds on D21, and what they cost

The backfill and coverage work was reviewed adversarially twice (the first
architecture reviewer was interrupted mid-run and produced no report; its
partial transcript still contained one finding, which is counted below). The
findings were almost entirely about *silent* wrongness, and one of them was
mine in a way tests could not see: an earlier version of `test/coverage.test.py`
wrote its fixture's fake install time into the real
`~/.agenttrace/coverage.json`, which reclassified 127 of the user's Hermes
sessions as "started after the install".

| finding | change |
|---|---|
| a trailing transcript entry with no `id`/`parentId` collapsed the whole chain walk and stored `last_entry_id=None`, so every later run rewrote the same two records | the walk starts from the last entry that HAS an id, and `_last_entry_id` returns one |
| a DANGLING `parentId` (truncated transcript) discarded every real ancestor | damage is distinguished from a branch: a dangling link, a missing `parentId` field, or a cycle keeps the whole file; a clean root still excludes other branches |
| an id-less entry was forced to the end of the chain, reordering the conversation | the include-set is sorted by FILE position |
| `session_id_of` used the path as spelled, so `slug/s.jsonl` and its absolute form were two sessions | resolved, and the live-traced comparison canonicalises both sides (`/var` is a symlink to `/private/var` on macOS) |
| usage with only cache figures was dropped, and a negative count broke `input >= cache` | any figure makes the usage real; counts are clamped at 0 |
| a missing timestamp fell through to the import clock; then a future mtime fabricated a multi-year trace | entry ts → previous ts → header ts → the session file's own mtime, clamped at "now" |
| the install marker's mtime moved forward on a touch, hiding real gaps | the earliest observed line is persisted (`~/.agenttrace/coverage.json`), keyed by `adapter_id|marker_path`, bounded to `(0, min(marker, now)]`, and the origin is reported (`marker mtime` / `persisted (not corroborated)` / `persisted (a later marker was observed)`) |
| a copied old session looked recent (file mtime used as its start) | the start comes from the artefact's OWN name timestamp, mtime only as a fallback |
| a header-only transcript produced an unclearable warning | an artefact with no message entry is not a session |
| a source that yielded nothing read as a clean bill of health | it carries a `note` naming the path |
| two cache slots could report different verdicts at the same moment | ONE slot; the data source is the caller's choice |
| an empty/lagging index reported "not capturing" | reported as undecided, with the inventory it did find, and never dressed as a warning |
| `unscoped:<call_id>` collided across provenance, and the store silently dropped the loser | the fallback carries the result entry's id |
| **request ids were unique per PROCESS, not per session** — a resumed session produced `pi-0-1` twice for two different provider calls, and the conformance kit rejected the stream | the live adapter includes a per-run discriminator |
| `ingest_files` tracked size alone, so a file REPLACED by a larger one read from the old offset and ingested fragments | identity is tracked by inode, plus a backward mtime for restores |

The last two came from my own verification rather than from a reviewer: the
request-id one surfaced while checking the regenerated history, and the
replacement one while re-indexing it. Both are the same lesson as D17/D18 — the
identity of a thing (a run, a file) is not the identity of the container it
happens to live in, and every place we assumed otherwise produced silent data
loss rather than an error.

Verification status, stated plainly: the reviewer's round-3 verdict was "safe to
rely on for recovering a user's history, with two damaged-file caveats" — and
both caveats (a missing `parentId` field, a future mtime) were fixed AFTER that
verdict, together with the request-id and file-replacement bugs. They are
covered by tests and by a full re-import of the real 8,587-event history, but
not by a fourth review round.

## D23 — Live verification found four more, and one of them was mine

Verifying `/reload` on the running agent (D21's fix) turned into four more
defects, all found by looking at what the live adapter actually wrote:

| finding | change |
|---|---|
| **request ids were unique per RUN, not per session** — a resumed session restarted `turnIndex` at 0, so `pi-0-1` named two different provider calls and the conformance kit rejected the stream | the live adapter includes a per-run discriminator |
| **the per-turn request sequence was reset by every `turn_start`** — a turn that restarts (Pi retries after a context-overflow compaction) reused its own id, giving one span id two provider calls | the sequence is monotonic per run |
| **a session split across daily files got one task span PER FILE** — same span id, different bounds, because the synthesis was per file and a trace's children were in two of them | `convert_stream()` synthesizes over the union of files; `convert_file` keeps its single-file behaviour and says so |
| backfilled turns shared the live turn namespace, so a session that was both live-traced and backfilled met in one `trace_id` (two task roots, colliding spans) | backfilled turns are namespaced `bf-<n>` |

And one that was my own, in the cleanup rather than the code: pruning the
already-written collisions with a key of `(session, request_id)` deleted
**6,284 records including every `llm_response`** — because `request_id` is
deliberately SHARED between a request and its response; that is the field's
purpose. Restored from the backup and redone with `(session, id, event kind)`,
which dropped exactly the 2 colliding records. The lesson is the same one this
file keeps recording: a bulk edit over data whose identity rules you have not
fully restated is a data-loss bug waiting to happen, and the backup existed only
because the previous edit had been destructive too.

Also changed: the e2e scripts no longer treat the AGENT's exit status as the
ADAPTER's. Hermes' API key expired during this session (HTTP 401), so
`hermes chat` exits 1 while the adapter correctly records the failed attempt —
which is what a tracer is for. The gate is now "did the adapter write records",
with the agent's own failure reported as context. Failing the suite on an
expired key trains people to ignore the suite.

## D24 — M5 begins: the trace now knows which code it was about

A coding-agent trace without the code it worked on answers "the tests failed"
and not "the tests failed on this branch, at this commit, after editing these
files". `common/workspace_snapshot()` records the repository — root, branch,
HEAD, dirty state, changed files (untracked included, because that is what an
agent creating a file looks like) — onto the v1 `resource` field, which becomes
`event.resource` in v2. Three rules: read-only (`--no-optional-locks`, so git
never takes the user's index lock), bounded (timeout, capped diff), and honest
about WHEN (`at: capture` when an adapter saw it, `at: index` when it was read
later, because comparing a months-old trace against today's diff is a wrong
conclusion waiting to happen). The diff is off by default — it can carry source
and credentials — and the snapshot says it withheld it.

It lives in `common/`, not the platform layer, because a deployed Hermes plugin
is a COPY of that file and can import nothing else; one implementation serves
every adapter. That also exposed a real gap: **the Hermes adapter recorded no
`cwd` at all**, in any event, so nothing could ever link its traces to a
repository. It now snapshots the workspace once per session (the hook carries no
cwd, so `os.getcwd()` — the plugin runs inside the agent's process, which is the
directory it works in).

Also in this round:

* tool spans carry `attributes.file` when the arguments name one, so a reader can
  follow a single file through a trace instead of guessing from a command string;
* `agenttrace report` aggregates the trajectory evaluators across traces —
  per-signal, with sample sizes, confidence and `no_signal` counts, and never
  summed into one score (proposal §8.2). Cost is `unknown` when a model is
  unpriced, as everywhere else;
* **`--full` was not a rebuild.** Clearing the archive is what makes it one:
  without it the same event ids came back, ingest dedups by id, and stored
  payloads kept whatever an *older* converter had written — so a converter fix
  never reached an already-indexed trace. It now clears events/spans/sessions and
  re-reads the files, and deliberately keeps datasets/experiments, which are the
  user's own work rather than derived data;
* **the span upsert had a cross-process race.** SELECT-then-INSERT let the
  indexer thread of a running `agenttrace serve` and a one-shot
  `agenttrace index --full` both find no row and both insert, one dying on the
  UNIQUE constraint. It is now `INSERT OR IGNORE` followed by a merge, so SQLite
  decides the conflict atomically. Two writers on one database is the normal
  case, not an edge.

Two more came from the review of the previous round, both about evidence rather
than behaviour: the run discriminator was a **clock slice**
(`Date.now().toString(36).slice(-5)`), which gives two processes started in the
same millisecond the same id — the very collision it was added to prevent — and
is now random; and the e2e scripts' new gate ("did the adapter write records")
was too weak, because an adapter whose request hook broke still writes
`session_start`. The gate now requires the **conversation** (a request, a
response, a tool call), and when the agent itself failed the scripts say that
the reply-render assertion is not evidence of a rendered reply — it can match
the prompt.
