# agent-trace

[English](README.md) · [中文](README.zh-CN.md)

Capture every LLM call an agent makes — request, response, tools, tokens —
into one local JSONL stream, then read it back with a single CLI or watch it
live in a side panel while the agent runs.

Works with **Hermes**, **OpenAI Codex CLI** and **Pi**. No proxy, no cloud, no
external service: traces stay on disk as plain JSONL.

> **Where this is going:** agent-trace is growing from a trace viewer into a
> local-first observability & evaluation platform — one canonical event
> schema, an adapter contract with a conformance kit, a local collector,
> and a web dashboard. The v2 schema, adapter SDK, v1→v2 converter and the
> architecture decisions are in `docs/` (`architecture.md`, `schema.md`,
> `writing-an-adapter.md`, `decisions.md`). The v1 format above is untouched
> and remains what all three adapters write.

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

## Read the traces

`agenttrace` is installed by `bash install.sh cli` (a symlink into
`~/.local/bin`):

```bash
agenttrace ls                      # recent records, one line each
agenttrace show <request_id>       # full request + response for one call
agenttrace search "17*23"          # grep across all record content
agenttrace sessions                # group by session id
agenttrace stats                   # calls / tokens / errors per agent and model
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

indexes all known trace dirs into `~/.agenttrace/agenttrace.db` (incremental,
crash-safe — JSONL stays the source of truth) and serves a dashboard on
`http://127.0.0.1:8741` with a one-time token in the startup line. Overview,
trace explorer, a full trace detail (execution tree, waterfall, span
input/output/errors, token usage), sessions, and per-adapter capability vs.
observed-data health. Updates live over SSE as agents write new traces.
Everything is local: loopback bind, Host/Origin checks, no CORS, no cloud.

```bash
agenttrace index                   # one-shot reindex, prints the report
agenttrace index --full            # rebuild projections from the raw archive
```

## "My agent is running but nothing shows up"

A live adapter is loaded when the agent starts, so registering one does nothing
for a session that is already running — restart the agent (Pi: `/reload` also
works). The dashboard now says so itself: it warns when a session started after
the adapter was installed has no traces.

Already-lost history can be recovered for agents that keep a transcript:

```bash
python3 pi/pi_import.py            # Pi: every session with no live traces
python3 codex/codex_import.py <rollout.jsonl>
agenttrace adapter coverage         # what is missing, and why (exit 1 on a gap)
```

## Adapters

Five adapters ship: **hermes**, **codex**, **pi**, plus **otlp** (import and
export any OTel-instrumented app) and **langgraph** (LangChain/LangGraph
callback handler).

```bash
agenttrace adapter list            # source, capabilities, verified-by test
agenttrace adapter test langgraph  # conformance kit over the shipped fixture
agenttrace otlp import otel.json   # OTLP in
agenttrace otlp export --trace <id> > otel.json   # OTLP out
```

Capabilities are declared honestly at four levels (`supported` / `partial` /
`unsupported` / `unknown`) and the dashboard shows what each adapter can and
cannot see. Third-party adapters are discovered from manifests or entry
points **without importing them** — code runs only when you explicitly ask.
See `docs/adapters.md`.

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
    {
      "role": "system",                       // ← written by developer
      "content": "You are a helpful coding assistant."
    },
    {
      "role": "user",                         // ← user input
      "content": "Hello, who are you?"
    }
  ],
  "tools": [
    "read",
    "write"
  ],
  "message_count": 2,
  "tool_count": 2
}
```

Every command accepts `--agent`, `--model`, `--event`, `--session`, `--since`,
`--contains`, `--limit`, and optional trace directories.

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

## How it fits together

```
hermes/__init__.py   plugin hooks ─┐
codex/codex_import.py rollout tail ├─> shared record shape ─> schema/trace.schema.json
pi/agent-trace.ts    Pi events    ┘         │
                                    common/agenttrace_common.py
                                             │
                       cli/agenttrace.py ────┴───> ls / show / search / sessions / stats
                       cli/agenttrace_watch.py ──> live panel (cli/panel/*)
```

`common/agenttrace_common.py` is the single source of truth for the rules all
three adapters must share: timestamps, content flattening, usage mapping,
truncation, redaction and the locked JSONL append. `schema/CHANGELOG.md` records
what each contract field means and why.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install ruff mypy
test/run-all.sh            # every test (SKIP_E2E=1 to skip the live model calls)
.venv/bin/ruff check cli common codex hermes test
.venv/bin/mypy
cd pi && npm ci && npm run typecheck
```

CI runs all of the above on every push (`.github/workflows/ci.yml`). The e2e
tests make real model calls and need a live `hermes`/`pi` install, so they are
opt-in locally rather than part of CI.

