# agent-trace

[English](README.md) · [中文](README.zh-CN.md)

**Local-first observability & evaluation for AI agents.** Trace, debug and
evaluate your agents — and keep the data on your machine.

Every model call, tool execution, subagent and failure lands in a local JSONL
stream, gets indexed into SQLite, and can be read four ways: a CLI, a live TUI
panel, a browser dashboard, and an evaluation harness that turns past runs into
repeatable tests. Works with **Hermes**, **Codex CLI**, **Pi**, **anything that
speaks OpenTelemetry**, and **LangChain/LangGraph**. No proxy, no cloud, no
account.

```bash
bash install.sh all              # adapters + CLI
agenttrace serve --daemon        # indexer + API + dashboard on 127.0.0.1:8741
```

## Quick start

```bash
bash install.sh hermes [profile|all]   # or: pi | codex | cli
hermes chat -q "Reply with exactly: OK"

agenttrace ls                      # recent records
agenttrace watch                   # the same traffic, live
agenttrace session <id>            # one conversation, every turn timed
agenttrace report                  # efficiency signals across recent runs
agenttrace serve                   # the dashboard
```

## Architecture

```mermaid
flowchart TB
    subgraph L1["L1 · agents — host processes we do not own"]
        A["Hermes · Codex CLI · Pi<br/>OTel apps · LangGraph"]
    end
    subgraph L2["L2 · adapter layer — one per agent"]
        B["capabilities: supported · partial · unsupported · unknown<br/>conformance kit over the emitted stream"]
    end
    subgraph L3["L3 · pipeline"]
        C1["capture<br/>validate → redact → append"]
        C2["index<br/>dedup → order → materialize"]
    end
    subgraph L4["L4 · storage"]
        D1[("JSONL<br/>truth · append-only")]
        D2[("SQLite<br/>derived · rebuildable")]
    end
    subgraph L5["L5 · local API"]
        E["read queries + SSE<br/>loopback · Host/Origin · token"]
    end
    subgraph L6["L6 · surfaces"]
        F["Dashboard · CLI · TUI · Evaluation"]
    end

    L1 --> L2
    L2 --> C1
    C1 --> D1
    D1 --> C2
    C2 --> D2
    D2 --> E
    E --> L6
    L6 -. "may add datasets and scores,<br/>never edits a trace" .-> D2
```

The arrow that does not exist is the important one: **nothing writes back into the
trace stream.** Datasets and scores are separate tables; a recorded execution is
immutable, which is what makes an experiment reproducible months later.

## Architecture principles

Each of these is enforced somewhere in the code, not just intended — the right
column names where.

| principle | in practice |
|---|---|
| **Local-first** | nothing leaves the machine: no cloud, no account, no telemetry. Loopback bind, `Host` allowlist, no CORS, `Origin` check, bearer token at 0600 (`server/main.py`) |
| **The contract is the wire format** | `schema/events.schema.json`, not a language interface. The Pi adapter is TypeScript and conforms without importing any of this; the kit checks emitted streams (`adapter_sdk/testing.py`) |
| **JSONL is truth, the index is derived** | a corrupt or stale index is rebuilt, never repaired — which is also how a converter change reaches already-indexed traces (`core/storage/`, `agenttrace index --full`) |
| **Redact before disk** | redaction runs inside the adapter, before the append. There is no path that writes first and scrubs later (`core/privacy.py`, `common/agenttrace_common.py`) |
| **Capture fails open** | one lost event is acceptable; a blocked agent is not. The sink swallows I/O errors and counts them (`adapter_sdk/event_sink.py`) |
| **Capability honesty** | four levels, and an absent field renders as "this adapter did not provide it", never as an empty string (`adapter_sdk/capabilities.py`) |
| **Evidence, not scores** | evaluators return evidence and confidence; trajectory signals are reported separately and never summed (`evaluation/`) |
| **Fail visibly** | an empty dashboard must explain itself: coverage gaps, "index still building", and errors that name the route they came from (`core/coverage.py`, `server/static/app.js`) |

The reasoning behind each, including the ones that were reversed after review,
is in [docs/decisions.md](docs/decisions.md).

## Commands

| | |
|---|---|
| `agenttrace ls` / `show` / `search` | recent records · one call in full · grep across content |
| `agenttrace sessions` / `stats` | group by session · calls, tokens, errors per agent/model |
| `agenttrace session <id>` | a conversation: every turn with its own duration |
| `agenttrace report` | trajectory signals — retry loops, error recovery, context growth |
| `agenttrace watch` | live full-screen panel while the agent runs |
| `agenttrace serve` | indexer + local API + dashboard (`--daemon`, `--status`, `--stop`) |
| `agenttrace index [--full]` | (re)build the SQLite index from the JSONL files |
| `agenttrace adapter list\|test\|coverage` | registry · conformance kit · capture gaps |
| `agenttrace otlp import\|export` | OTLP in and out |

Filters (`--agent --model --event --session --since --contains --limit`) and the
environment variables are in **[docs/usage.md](docs/usage.md)**.

## Dashboard

`agenttrace serve --daemon` indexes every known trace dir and serves
`http://127.0.0.1:8741` with a one-time token in the startup line. Updates
arrive live over SSE.

**Overview** (errors, tokens, latency) · **Traces** (search and filter) ·
**Trace detail** (execution tree + waterfall, span input/output/errors, the
workspace, raw JSON) · **Sessions → Session detail** (each turn's duration, its
share of agent time, and where the gaps were) · **Datasets / Experiments** ·
**Adapters** (declared capabilities vs. observed data, ingest health, gaps).

A trace is **one turn** — that is what "one task execution" means — which is why
per-conversation time lives on the session page.

## Evaluation

```bash
agenttrace report --limit 100 --verbose
```

Turn a bad trace into a regression test and score a whole set: a dataset holds
the task and expected outcome (pointers, not a copy of the prompt history), an
experiment pins the dataset version, evaluator versions and agent config, and
runs use **playback** scoring — they score recorded executions rather than
re-running the agent.

| evaluator kind | examples |
|---|---|
| deterministic | tests pass · output contains X · budget respected · **the agent claimed success while the last test run failed** |
| trajectory | repeated failing calls · error recovery · context growth without progress · time to first action |
| LLM-as-judge | task completion, judged by a **local** endpoint (unconfigured ⇒ reported as "no signal", never a fake verdict) |

Signals are never summed into one score: a retry loop can be the correct
recovery. Cost is `unknown` for unpriced models rather than estimated.

## Adapters

Five ship: **hermes**, **codex**, **pi**, **otlp**, **langgraph**. Capabilities
are declared at four levels — `supported`, `partial`, `unsupported`, `unknown` —
so a gap reads as "this adapter did not provide this field" rather than as an
empty string. Codex, for instance, declares `streaming: unsupported` because its
rollout file is written after completion.

Third-party adapters are found via a manifest or an entry point **without being
imported**; code runs only when you explicitly ask. See
[docs/adapters.md](docs/adapters.md) and
[docs/writing-an-adapter.md](docs/writing-an-adapter.md).

## "I installed it and nothing shows up"

An adapter loads when its host process starts, so it does nothing for a session
that is already running — restart the agent, or `/reload` in Pi. The dashboard
says so itself:

```bash
agenttrace adapter coverage         # what is missing, and why (exit 1 on a gap)
```

It compares the sessions each agent left artefacts for against the sessions we
hold traces for, split by age against the adapter's install time: after the
install is a defect, before it is a backfill candidate.

```bash
python3 pi/pi_import.py             # Pi: recover sessions with no live traces
python3 codex/codex_import.py <rollout.jsonl>
```

Backfilled records are labelled and namespaced so they are never mistaken for
observed ones, and the request side is rebuilt from the transcript — and says so.

## Privacy

* Nothing leaves the machine: no cloud, no account, no telemetry.
* Redaction happens in the adapter, before disk (`AGENTTRACE_REDACT=0` disables
  it); `AGENTTRACE_CAPTURE=metadata` keeps structure and drops bodies.
* The local API is **not** implicitly trusted: loopback bind, `Host` allowlist
  (DNS rebinding), no CORS headers, `Origin` check, bearer token at mode 0600.
* Workspace capture is read-only and never takes your git index lock; diffs are
  off by default (`AGENTTRACE_GIT=full` opts in) and the snapshot records when it
  looked, so a months-old trace is not read against today's branch.

Deliberately not here yet: retention/auto-delete policies, deleting a single
trace or session, real agent replay (playback only), cross-machine aggregation.
The reasoning behind each decision is in [docs/decisions.md](docs/decisions.md).

## Development

```bash
test/run-all.sh                    # everything (SKIP_E2E=1 to skip real model calls)
.venv/bin/ruff check . && .venv/bin/mypy
cd pi && npm ci && npm run typecheck
```

~29 test files: per-adapter fixtures, the conformance kit, dashboard routing,
the two end-to-end tests, and `test/review-fixes.test.py`, which holds every
finding from the review rounds — including the ones the fixes themselves
introduced. CI runs lint, types and the suite on every push.
