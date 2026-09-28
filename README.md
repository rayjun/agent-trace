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
```

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
