#!/usr/bin/env bash
# End-to-end: a real Hermes call must land in the trace file the panel tails.
#
# The adapter is installed per profile, so the profile under test and the trace
# directory being read must be the same one. Reading profile `ai` while invoking
# bare `hermes` (the default profile, which has no plugin) reports "no new
# records" no matter how healthy the adapter is. Override with AGENTTRACE_PROFILE.
set -euo pipefail

# Count trace records without relying on glob success or `wc` reading empty
# stdin. The previous form — `cat "$DIR"/*.jsonl | wc -l || echo 0` — printed
# "0\n0" whenever no file existed yet: `wc` counted zero lines AND the `||`
# fired because `cat` failed, so the two-line value then made
# `[ "$after" -le "$before" ]` error out ("integer expression expected") and
# the gate silently stopped gating.

# The agent's own exit status is NOT the adapter's. A provider failure (an
# expired key, a rate limit, a model that no longer exists) makes `hermes chat`
# exit non-zero while the adapter does its job perfectly — it records the failed
# attempt, which is exactly what a tracer is for. Failing the suite on that
# trains people to ignore it, so the two are reported separately: the gate is
# "did the adapter write records", and the agent's failure is stated as context.
count_records() {
  local dir="$1" total=0 f
  for f in "$dir"/*.jsonl; do
    [ -f "$f" ] || continue
    total=$(( total + $(wc -l < "$f") ))
  done
  printf '%s' "$total"
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${AGENTTRACE_PROFILE:-default}"

if [ "$PROFILE" = "default" ]; then
  HERMES_HOME_DIR="$HOME/.hermes"
  HERMES_ARGS=()
else
  HERMES_HOME_DIR="$HOME/.hermes/profiles/$PROFILE"
  HERMES_ARGS=(--profile "$PROFILE")
fi
TRACES="$HERMES_HOME_DIR/traces"

if [ ! -f "$HERMES_HOME_DIR/plugins/agent-trace/__init__.py" ]; then
  echo "SKIP — agent-trace is not installed for profile '$PROFILE' ($HERMES_HOME_DIR)"
  echo "       install it first:  bash install.sh hermes $PROFILE"
  exit 0
fi

before=$(count_records "$TRACES")
# ${HERMES_ARGS[@]+...}: portable empty-array expansion — bash 3.2 (macOS)
# errors on "${HERMES_ARGS[@]}" under `set -u` when the array is empty.
AGENT_LOG=$(mktemp)
# `|| agent_rc=$?` rather than a bare call: this script runs under `set -e`, so
# a failing agent would abort before the exit status is ever examined.
agent_rc=0
hermes ${HERMES_ARGS[@]+"${HERMES_ARGS[@]}"} chat -q "Reply with exactly: E2E-OK" \
  >"$AGENT_LOG" 2>&1 || agent_rc=$?
sleep 1
after=$(count_records "$TRACES")

if [ "$after" -le "$before" ]; then
  echo "FAIL — hermes produced no new trace records (before=$before after=$after)"
  echo "       profile=$PROFILE traces=$TRACES"
  [ "$agent_rc" -ne 0 ] && { echo "       (and the agent itself failed:)"; \
    tail -3 "$AGENT_LOG" | sed 's/^/       /'; }
  exit 1
fi
# A record count alone is too weak a gate: an adapter whose request hooks broke
# still writes `session_start`, and the suite would say PASS. What the trace must
# contain is the CONVERSATION — a request or a response — which is the thing this
# test is about.
CONV=$(python3 - "$TRACES" "$PROFILE" <<'PYEOF'
import glob, json, os, sys
traces = sys.argv[1]
kinds = {"llm_request", "llm_response", "llm_error", "assistant_message"}
seen = 0
for path in sorted(glob.glob(os.path.join(traces, "*.jsonl"))):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"event"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event") in kinds:
                seen += 1
print(seen)
PYEOF
)
if [ "${CONV:-0}" -le 0 ]; then
  echo "FAIL — the adapter wrote records but NO conversation (llm_request/llm_response)"
  echo "       a broken request hook writes session_start and nothing else"
  exit 1
fi
echo "PASS — hermes (profile=$PROFILE) wrote $((after - before)) new trace records, $CONV conversational"
if [ "$agent_rc" -ne 0 ]; then
  echo "NOTE — the agent exited $agent_rc, and the adapter recorded the attempt"
  echo "       anyway (that is what a tracer is for). The reply-render assertion"
  echo "       below is therefore NOT evidence that a reply was rendered — it can"
  echo "       match the prompt. Provider-side cause:"
  grep -iE "404|401|403|429|expired|quota|rate limit|not found" "$AGENT_LOG" \
    | head -2 | sed 's/^/       /' || true
fi
rm -f "$AGENT_LOG"

# Locate the session that produced the reply BEFORE rendering. The render
# scopes itself via latest_session — the trace file's LAST record — and any
# concurrent writer can land between `hermes chat` finishing and that read.
# The 09:00 cron appends into the same file; it started 0.1s before a render
# once and the panel dutifully rendered the cron session (132 lines, no
# E2E-OK). Pin the session explicitly so the test measures the adapter, not
# the write interleaving.
#
# The locator matches the PROMPT, not the reply: the model does not always
# obey "reply with exactly" — one run loaded a relay-crypto skill, ran six
# tool calls and answered "E2E-OK\n\n关于加密包装的说明…", which an exact
# content match could not find. The prompt text is deterministic; the reply's
# shape is not. The reply is still what the render greps for below.
sid=$(python3 - "$TRACES" <<'PY'
import glob, json, sys
WANT = "Reply with exactly: E2E-OK"
for path in sorted(glob.glob(sys.argv[1] + "/*.jsonl")):
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - 8_000_000))   # prompt is seconds old: tail only
        tail = fh.read().decode("utf-8", "replace").splitlines()
    for line in reversed(tail):
        if WANT not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event") != "user_prompt":
            continue
        msgs = (r.get("request") or {}).get("messages") or []
        text = "\n".join(str(m.get("content") or "") for m in msgs
                         if isinstance(m, dict))
        if WANT in text:
            print(r.get("session_id") or "")
            sys.exit(0)
PY
)
if [ -z "$sid" ]; then
  echo "FAIL — could not locate the session that produced E2E-OK"
  exit 1
fi
echo "PASS — reply traced to session $sid"

# Render into a file, then grep it. Piping straight into `grep -q` fails here:
# grep exits the moment it matches, agenttrace dies on SIGPIPE (141 = 128+13),
# and `set -o pipefail` turns that into a non-zero pipeline even though grep
# itself succeeded (0). The producer being killed by a closed pipe is correct
# Unix behaviour — the test was measuring the wrong process.
render=$(mktemp)
trap 'rm -f "$render"' EXIT
python3 "$ROOT/cli/agenttrace.py" watch "$TRACES" --no-follow --session "$sid" >"$render" 2>&1

if grep -q "E2E-OK" "$render"; then
  echo "PASS — panel renders the live reply"
else
  echo "FAIL — E2E-OK not found in rendered output ($(wc -l <"$render") lines rendered)"
  tail -5 "$render"
  exit 1
fi
