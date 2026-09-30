# agent-trace

[English](README.md) · [中文](README.zh-CN.md)

Capture every LLM call an agent makes — request, response, tools, tokens —
into one local JSONL stream, then read it back with a single CLI or watch it
live in a side panel while the agent runs.

Works with **Hermes**, **OpenAI Codex CLI** and **Pi**. No proxy, no cloud, no
external service: traces stay on disk as plain JSONL.

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

