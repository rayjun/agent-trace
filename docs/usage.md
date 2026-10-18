# Usage details

Everything the README leaves out. Verified against `agenttrace --help` and run
by the test suite; if something here stops working, that is a bug, not a
documentation change.

## CLI

```
agenttrace ls [dirs...]            recent records, one line each
agenttrace show <request_id>       full request + response for one call
agenttrace search <query>          grep across record content
agenttrace sessions                group records by session
agenttrace stats                   calls / tokens / errors per agent and model
agenttrace session <id>            one conversation: every turn with its duration
agenttrace watch                   live full-screen panel
agenttrace serve                   indexer + local API + dashboard
agenttrace index [--full]          (re)build the SQLite index
agenttrace report [--verbose]      efficiency report across recent traces
agenttrace adapter list|test|cov   adapter registry, conformance, coverage
agenttrace otlp import|export      OTLP in / out
```

Every command that reads traces accepts `--agent`, `--model`, `--event`,
`--session`, `--since`, `--contains`, `--limit`, `--reverse`, and optional trace
directories (added last, because a leading `nargs="*"` would swallow the
subcommand).

### `ls` and the index

`ls` prints the newest `--limit` records, oldest first (`--reverse` flips that).
A first run builds a small index in `~/.cache/agent-trace`; after that
`ls`/`sessions`/`stats` answer in about a second instead of re-reading hundreds
of megabytes. The index only routes (which file, which byte offset) and
aggregates — bodies are always rendered from the JSONL, so a projection can
never print a stale preview.

## `show`: the prompt the model actually received

The three host adapters store the system prompt differently — Hermes writes it
into *both* `messages` and a separate `system_prompt` field, Pi into `messages`
only, Codex into the field only with an empty `messages` — so `show` folds all
three into the shape a request payload really has: one copy of the prompt, no
`system_prompt` / `instructions` headings beside it.

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

## `watch`: the live panel

```bash
agenttrace watch                    # current session, live
agenttrace watch --all-sessions     # every session
agenttrace watch --session <id>     # pin one session
agenttrace watch --history 200      # preload 200 records
agenttrace watch --no-follow        # print current contents and exit
```

Keys: `e` expand folded fields · `/` filter · `PgUp`/`PgDn` scroll · `q` quit.

In a split terminal:

```bash
tmux new-session -d -s trace
tmux split-window -t trace -h -l 45
# right pane runs: agenttrace watch
```

## `session`: a conversation, not a run

A trace is ONE turn; a session is the conversation. `agenttrace session <id>`
accepts the full id or any trailing part of it, and prints per turn:

```
session   /Users/you/.pi/agent/sessions/--repo--/2026-10-17T02-46-10-412Z_x.jsonl
turns     101   p50 17.4s   p95 2.3m
agent work 48.9m   wall 27.0h   outside turns 26.2h   (excluded 33 interrupted turn(s))
tokens    in 133,500,564  out 359,419  cache_read 122,836,761

  #  start                 duration status      spans    tokens  kinds
  1  2026-10-17T02:46:50.166Z      8.4m ok             80   885,199  llm,tool
```

Two numbers are kept apart on purpose: `agent work` is the time spent inside
turns we can trust, `wall` is how long the conversation was open, and
`outside turns` is the difference — the person thinking, or the machine idle.
A turn whose window contains another turn's start was interrupted (the process
died, or the session was resumed the next day), so its wall clock is partly a
gap; those turns are excluded from the work total and the count is reported.

## `report`: trajectory signals, never one score

```
traj:first_action_latency   73.3%  scored=60  flagged=16  no_signal=0  conf=0.727
traj:error_recovery         93.3%  scored=60  flagged=4   no_signal=0  conf=0.807
traj:retry_loops           100.0%  scored=60  flagged=0   no_signal=0  conf=1.0
det:tests_pass             100.0%  scored=1   flagged=0   no_signal=59 conf=1.0
```

Signals are reported separately: a retry loop can be the correct recovery, and
extra tokens can be the price of a harder task. `no_signal` counts the traces
where an evaluator could not tell — those are not failures. `--verbose` adds
the evidence behind each flagged trace.

## Adapters

```bash
agenttrace adapter list              # source, capabilities, verified-by test
agenttrace adapter list --json
agenttrace adapter test <id|file>    # conformance kit over a fixture or your own stream
agenttrace adapter coverage          # sessions the agent ran vs. traces we hold
```

`coverage` exits 1 when a session started after its adapter's install has no
traces — that is the "I installed it but nothing is captured" case. Third-party
adapters are discovered from `~/.agenttrace/adapters/*.json` or an
`agent_trace.adapters` entry point; discovery reads metadata only and never
imports the module. See `docs/adapters.md` and `docs/writing-an-adapter.md`.

## Recovering history

```bash
python3 pi/pi_import.py             # Pi: every session with no live traces
python3 pi/pi_import.py --list      # show what would be imported
python3 pi/pi_import.py --force     # include sessions that also have live traces
python3 pi/pi_import.py --reimport  # rewrite everything (cursor ignored)
python3 codex/codex_import.py <rollout.jsonl>
```

Backfilled records are labelled (`backfill=session`) and use their own id and
turn namespaces, so they are never confused with observed ones and cannot
collide with the live turns of the same session. Hermes keeps no transcript;
there is nothing to recover there.

## Environment variables

| variable | default | effect |
|---|---|---|
| `AGENTTRACE_HOME` | — | an extra trace directory to read |
| `AGENTTRACE_INDEX` | on | `0` forces a full rescan instead of the CLI index |
| `AGENTTRACE_VALIDATE` | off | `1` makes every adapter check each record against the contract as it writes |
| `AGENTTRACE_CAPTURE` | `full` | `metadata` keeps structure (counts, ids, timings, tool names, tokens) and drops bodies |
| `AGENTTRACE_REDACT` | on | `0` disables secret scrubbing |
| `AGENTTRACE_MAX_CHARS` | 200000 | per-field body cap |
| `AGENTTRACE_GIT` | `metadata` | `full` records the diff (capped), `off` captures no workspace |
| `AGENTTRACE_GIT_MAX` | 20000 | bytes of diff to keep |
| `AGENTTRACE_DB` | `~/.agenttrace/agenttrace.db` | index location |
| `AGENTTRACE_PLATFORM_DIR` | `~/.agenttrace` | where the db, pid, log and token live |
| `AGENTTRACE_TOKEN` | generated | bearer token for the local API |
| `AGENTTRACE_JUDGE_BASE_URL` / `_MODEL` | — | a local OpenAI-compatible endpoint for LLM-as-judge |

## Files

| path | contents |
|---|---|
| `~/.hermes/traces/`, `~/.pi/agent/traces/`, `~/.codex/traces/` | the JSONL traces |
| `~/.cache/agent-trace/` | the CLI's incremental index |
| `~/.agenttrace/agenttrace.db` | the SQLite projection |
| `~/.agenttrace/server.{pid,log,token}` | dashboard process, log, API token |
| `~/.agenttrace/coverage.json` | the earliest install line observed per adapter |
