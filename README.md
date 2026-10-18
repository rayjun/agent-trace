# agent-trace

[English](README.md) · [中文](README.zh-CN.md)

**Local-first observability & evaluation for AI agents.** Trace, debug and
evaluate your agents — and keep the data on your machine.

Every model call, tool execution, subagent and failure is captured into a local
JSONL stream, indexed into SQLite, and readable four ways: a CLI, a live TUI
panel, a browser dashboard, and an evaluation harness that turns past runs into
repeatable tests.

Works with **Hermes**, **OpenAI Codex CLI** and **Pi**, plus **any app that
speaks OpenTelemetry** and **LangChain/LangGraph**. No proxy, no cloud, no
account, no external service.

```bash
bash install.sh all              # adapters + CLI
agenttrace serve --daemon        # indexer + API + dashboard on 127.0.0.1:8741
```

## Install

Run from the repo root — one command per agent, plus the CLI:

```bash
bash install.sh hermes [profile|all]    # Hermes plugin
bash install.sh pi                      # Pi extension
python3 codex/install_hook.py           # Codex hook
bash install.sh cli                     # agenttrace command
bash install.sh all                     # everything above
bash install.sh status                  # what is installed — and what is STALE
```

The Hermes adapter is installed as a *copy*, so `status` compares the deployed
bytes against this repo and tells you when the running agent is still on old
code. The other three are live references into the checkout.

Prove it works with one real call — `agenttrace ls` prints the agent as its
second column:

```bash
hermes chat -q "Reply with exactly: OK"    # -> ~/.hermes/traces/
pi -p "Reply with exactly: OK"             # -> ~/.pi/agent/traces/

agenttrace ls        # new rows showing `hermes` / `pi`
agenttrace watch     # the same traffic, live
```

Traces land in `~/.hermes/traces/`, `~/.pi/agent/traces/` and
`~/.codex/traces/`.

## What it captures

One record per observation, in the shared format (`schema/trace.schema.json`):

* **model calls** — the prompt as the model received it, the response, tool
  calls it asked for, tokens (input/output/cached/reasoning) and latency;
* **tool executions** — arguments, result body, exit code, duration;
* **sessions and turns** — enough structure to reconstruct a whole conversation;
* **failures** — provider errors, tool errors, and spans that never ended;
* **the workspace** — repository, branch, commit and what changed (see below).

## Read the traces

`agenttrace` is installed by `bash install.sh cli` (a symlink into
`~/.local/bin`):

```bash
agenttrace ls                      # recent records, one line each
agenttrace show <request_id>       # full request + response for one call
agenttrace search "17*23"          # grep across all record content
agenttrace sessions                # group by session id
agenttrace stats                   # calls / tokens / errors per agent and model
agenttrace session <id>            # ONE conversation: every turn with its own duration
```

Every command accepts `--agent`, `--model`, `--event`, `--session`, `--since`,
`--contains`, `--limit`, and optional trace directories.

`show` prints the prompt as the model actually received it: one system message
first, every message marked with who wrote it. The adapters store the system
prompt differently (Hermes writes it into *both* `messages` and a separate
`system_prompt` field, Pi into `messages` only, Codex into the field only with
an empty `messages`), so `show` folds all three into the shape a request
payload really has — one copy of the prompt, no `system_prompt` / `instructions`
headings beside it:

```jsonc
{
  "model": "space-bunny-free",
  "messages": [
    { "role": "system", "content": "You are a helpful coding assistant." },
    { "role": "user", "content": "Hello, who are you?" }
  ],
  "tools": ["read", "write"],
  "message_count": 2,
  "tool_count": 2
}
```

`ls` prints the most recent records — the newest `--limit`, oldest first; add
`--reverse` for newest first. A first run pays a one-off cost to build a small
index in `~/.cache/agent-trace`, after which `ls`/`sessions`/`stats` answer in
about a second instead of rescanning hundreds of megabytes. Set
`AGENTTRACE_INDEX=0` to force a full rescan, or `AGENTTRACE_VALIDATE=1` to make
every adapter check each record against the contract as it writes it.

## Watch it live

```bash
agenttrace watch                    # current session, live (default)
agenttrace watch --all-sessions     # every session
agenttrace watch --session <id>     # pin one session
agenttrace watch --history 200      # preload 200 records
agenttrace watch --no-follow        # print current contents and exit
```

A full-screen panel that follows the trace stream while the agent runs — one
block per call: what you sent, what the model replied, which tools it ran,
tokens and latency.

Keys: `e` expand folded fields · `/` filter · `PgUp`/`PgDn` scroll · `q` quit.

Run it in a second terminal, or split your shell:

```bash
tmux new-session -d -s trace
tmux split-window -t trace -h -l 45
# right pane runs: agenttrace watch
```

## The dashboard

```bash
agenttrace serve --daemon          # indexer + local API + web UI, detached
agenttrace serve --status          # is it running? which pid?
agenttrace serve --stop
agenttrace serve                   # foreground (Ctrl-C to stop)
```

`--daemon` is the one to use for "start it once, look at it later". It re-execs
through `scripts/serve-daemon.py`, which detaches into its own session
(`fork` + `setsid`; macOS has no `setsid(1)`). A plain `nohup ... &` stays in
the launcher's process group and is taken down by whatever reaps that group —
the symptom is a page that suddenly cannot connect. The pid is written to
`~/.agenttrace/server.pid`, the log to `~/.agenttrace/server.log`.

It indexes every known trace dir into `~/.agenttrace/agenttrace.db`
(incremental, crash-safe — JSONL stays the source of truth) and serves a
dashboard on `http://127.0.0.1:8741`, with a one-time token in the startup line.
Updates arrive live over SSE while agents write.

| page | what it answers |
|---|---|
| **Overview** | traces, error rate, token usage, duration p50/p95, per-adapter totals |
| **Traces** | search by time, agent, status, duration, tokens, session |
| **Trace detail** | execution tree + waterfall, span input/output/error/raw, the workspace, token usage. A trace is **one turn** |
| **Sessions** | every conversation; click one for **Session detail** |
| **Session detail** | each turn with its own duration, its share of agent time, and the same turns on the session's wall clock — so the gaps are visible |
| **Datasets / Experiments** | evaluation runs, per-example scores, evidence |
| **Adapters** | declared capabilities vs. observed data, ingest health, and coverage gaps |

```bash
agenttrace index                   # one-shot reindex, prints the report
agenttrace index --full            # re-ingest everything from the JSONL files
```

`--full` clears the archive and re-reads the files, which is how a converter
change reaches traces that were already indexed. Datasets and experiments are
kept — they are your work, not derived data.

## Evaluate a run, not just read it

Turn a bad trace into a regression test, then score a whole set:

```bash
agenttrace report --limit 100       # efficiency report across recent traces
agenttrace report --verbose         # ...with the evidence behind each flag
```

`report` runs the same evaluators the dashboard uses and prints each signal
separately — first-action latency, error recovery, retry loops, repeated calls,
context growth — with sample sizes, mean confidence and a `no_signal` count.
**Signals are never summed into one score**: a retry loop can be the correct
recovery, and extra tokens can be the price of a harder task. Cost is reported
as `unknown` when a model has no configured price, rather than estimated.

Three kinds of evaluator:

| kind | examples |
|---|---|
| deterministic | tests pass, output contains X, budget respected, **the agent claimed success while the last test run failed** |
| trajectory | identical failing calls repeated, error recovery, context growth without progress, time to first action |
| LLM-as-judge | task completion, judged by a **local** model endpoint (unconfigured ⇒ reported as "no signal", never a fake verdict) |

Datasets are built from real traces (the task and pointers, not a copy of the
whole prompt history), experiments pin the dataset version, evaluator versions
and agent config, and runs use **playback** scoring: they score recorded
executions rather than re-running the agent.

## "My agent is running but nothing shows up"

An adapter is loaded when its host process starts, so registering one does
nothing for a session that is already running — restart the agent, or `/reload`
in Pi. The dashboard says this itself, and `agenttrace adapter coverage` tells
you exactly what is missing:

```bash
agenttrace adapter coverage         # what is missing, and why (exit 1 on a gap)
```

It compares the sessions each agent left artefacts for against the sessions we
hold traces for, split by age against the adapter's install time: a session
started **after** the install with no traces is a defect and warns; one from
before it is history and is listed as a backfill candidate.

History can be recovered for agents that keep a transcript:

```bash
python3 pi/pi_import.py             # Pi: every session with no live traces
python3 pi/pi_import.py --list      # what would be imported
python3 codex/codex_import.py <rollout.jsonl>
```

Backfilled records are labelled (`backfill=session`, their own id namespaces) so
they are never mistaken for observed ones, and the request side is rebuilt from
the transcript and says so. Hermes keeps no transcript, so there is nothing to
recover there.

## Adapters

Five ship: **hermes**, **codex**, **pi**, **otlp** (import/export any
OTel-instrumented app) and **langgraph** (LangChain/LangGraph callback handler).

```bash
agenttrace adapter list            # source, capabilities, verified-by test
agenttrace adapter test langgraph  # conformance kit over the shipped fixture
agenttrace otlp import otel.json   # OTLP in
agenttrace otlp export --trace <id> > otel.json   # OTLP out
```

Capabilities are declared honestly at four levels — `supported`, `partial`,
`unsupported`, `unknown` — and the dashboard shows what each adapter can and
cannot see. A gap is rendered as "this adapter did not provide this field", not
as an empty string; codex, for example, declares `streaming: unsupported`
because its rollout file is written after completion.

Third-party adapters are discovered from manifests or entry points **without
importing them**: discovery reads metadata, and code runs only when you
explicitly ask. See `docs/adapters.md` and `docs/writing-an-adapter.md`.

## The code a trace was about

A coding agent's trace is half readable without the code it was working on, so
each session records its workspace: repository root, branch, commit, dirty
state and the changed files (untracked ones included). Tool calls that name a
file carry it, so you can follow one file through a trace.

The snapshot is **read-only** (`git --no-optional-locks`, so git never takes
your index lock), **bounded** (timeouts, capped sizes), and **labelled with when
it looked**: `at: capture` when the adapter saw it live, `at: index` when nobody
recorded one and it was read later. Confusing the two would make a months-old
trace look like it ran against today's branch.

Diffs are **off by default** — they can contain source and credentials — and the
snapshot says it withheld them:

```bash
AGENTTRACE_GIT=full agenttrace serve   # include the diff (capped)
AGENTTRACE_GIT=off  agenttrace serve   # no workspace capture at all
```

## Privacy and safety

* **Nothing leaves the machine.** No cloud, no account, no telemetry.
* **Redaction happens at capture time**, in the adapter, before a value reaches
  disk — there is no path that writes first and scrubs later. `AGENTTRACE_REDACT=0`
  disables it; `AGENTTRACE_CAPTURE=metadata` keeps the structure (counts, ids,
  timings, tool names, tokens) and drops the bodies.
* **The local API is not implicitly trusted.** It binds loopback, allowlists the
  `Host` header (DNS-rebinding defense), sends no CORS headers, checks `Origin`,
  and requires a bearer token (`~/.agenttrace/server.token`, mode 0600).
* **Datasets store pointers, not copies** of prompt history; exported traces are
  the only place a diff can appear, and only when you asked for it.

Deliberately **not** here yet: retention/auto-delete policies, deletion of a
single trace or session from the CLI, real agent replay (playback only), and
cross-machine aggregation. See `docs/decisions.md`.

## How it fits together

```
hermes/__init__.py    plugin hooks ─┐
codex/codex_import.py rollout tail  ├─ adapters ─> canonical events (JSONL)
pi/agent-trace.ts     Pi events     │                 │
adapters/otlp/        OTLP JSON     │                 │  schema/events.schema.json
adapters/langgraph/   callbacks    ─┘                 │  common/agenttrace_common.py
                                                      │  adapter_sdk/*
                                       core/storage/ ──┴─> indexer ─> SQLite
                                                           │
                            core/query.py ─────────────────┤
                                                           ├─> server/  (API + SSE + dashboard)
                                                           ├─> cli/     (ls show search sessions stats session watch)
                                                           └─> evaluation/ (datasets · evaluators · experiments)
```

`schema/events.schema.json` is the contract every adapter converges on;
`schema/trace.schema.json` is the older flat record that the three host
adapters still write, converted at read time (`core/convert/v1_to_v2.py`) so no
existing data is lost. `common/agenttrace_common.py` holds the rules every
adapter must share — timestamps, content flattening, usage mapping, truncation,
redaction, locked append — and is copied next to the deployed Hermes plugin,
which is why the shared layers import the standard library only.
`schema/CHANGELOG.md` records what each contract field means and why.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install ruff mypy
test/run-all.sh            # every test (SKIP_E2E=1 to skip the live model calls)
.venv/bin/ruff check cli common codex hermes test
.venv/bin/mypy
cd pi && npm ci && npm run typecheck
```

The suite is ~30 files and includes fixtures per adapter, a conformance kit, the
dashboard's routing, and the two end-to-end tests (real model calls; they need a
live `hermes`/`pi` install). `test/review-fixes.test.py` holds every finding from
the review rounds, including the ones the fixes themselves introduced; the
reasoning behind each is in `docs/decisions.md`.

CI runs lint, types and the full suite on every push
(`.github/workflows/ci.yml`).

## Docs

| file | contents |
|---|---|
| `docs/architecture.md` | the six layers and the boundaries between them |
| `docs/schema.md` | the canonical event model, field by field |
| `docs/adapters.md` | the five adapters, capabilities, coverage, recovery |
| `docs/writing-an-adapter.md` | how to add one, and how it will be checked |
| `docs/examples.md` | runnable starting points |
| `docs/decisions.md` | every architectural decision, with the reasoning |
