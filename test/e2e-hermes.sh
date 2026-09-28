#!/usr/bin/env bash
# End-to-end: a real Hermes call must land in the trace file the panel tails.
#
# The adapter is installed per profile, so the profile under test and the trace
# directory being read must be the same one. Reading profile `ai` while invoking
# bare `hermes` (the default profile, which has no plugin) reports "no new
# records" no matter how healthy the adapter is. Override with AGENTTRACE_PROFILE.
set -euo pipefail
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

before=$(cat "$TRACES"/*.jsonl 2>/dev/null | wc -l || echo 0)
hermes "${HERMES_ARGS[@]}" chat -q "Reply with exactly: E2E-OK" >/dev/null 2>&1
sleep 1
after=$(cat "$TRACES"/*.jsonl 2>/dev/null | wc -l || echo 0)

if [ "$after" -le "$before" ]; then
  echo "FAIL — hermes produced no new trace records (before=$before after=$after)"
  echo "       profile=$PROFILE traces=$TRACES"
  exit 1
fi
echo "PASS — hermes (profile=$PROFILE) wrote $((after - before)) new trace records"

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
