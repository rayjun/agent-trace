#!/usr/bin/env bash
# End-to-end: a real pi session must land in the trace file the panel tails,
# with the reply renderable under its own session (no latest-file races —
# locate the session by the exact prompt, like e2e-hermes.sh).
# Skips (exit 0) when pi or the extension is not installed.
set -uo pipefail

# Count trace records without relying on glob success or `wc` reading empty
# stdin. The previous form — `cat "$DIR"/*.jsonl | wc -l || echo 0` — printed
# "0\n0" whenever no file existed yet: `wc` counted zero lines AND the `||`
# fired because `cat` failed, so the two-line value then made
# `[ "$after" -le "$before" ]` error out ("integer expression expected") and
# the gate silently stopped gating.
count_records() {
  local dir="$1" total=0 f
  for f in "$dir"/*.jsonl; do
    [ -f "$f" ] || continue
    total=$(( total + $(wc -l < "$f") ))
  done
  printf '%s' "$total"
}

cd "$(dirname "${BASH_SOURCE[0]}")/.."

TRACE_DIR="$HOME/.pi/agent/traces"
PROMPT="Reply with exactly: E2E-OK"

if ! command -v pi >/dev/null 2>&1; then
  echo "SKIP — pi not on PATH"
  exit 0
fi
if ! pi list 2>/dev/null | grep -qi "agent-trace"; then
  echo "SKIP — extension not registered (run: bash install.sh pi)"
  exit 0
fi
mkdir -p "$TRACE_DIR"

# PI_E2E_MODEL overrides the model. The settings default can be deprecated
# upstream (kimi-k2.6 was), which fails the run for a reason that has nothing
# to do with the adapter — the trace path is what this test measures.
PI_ARGS=()
[ -n "${PI_E2E_MODEL:-}" ] && PI_ARGS=(--model "$PI_E2E_MODEL")

# pi honours PI_SESSION_FILE/PI_SESSION_ID from the environment. An agent
# harness exporting them (i.e. an agent running this test) would make the
# child pi append to the CALLER's session: the trace then carries the caller's
# session id, the caller's transcript absorbs the e2e prompt, and the locator
# below cannot find this run's own session. Give the test call a clean session.
unset PI_SESSION_FILE PI_SESSION_ID

before=$(count_records "$TRACE_DIR")

# A command must never be able to hang the suite or the installer, but
# `timeout` is GNU coreutils and is ABSENT on stock macOS — where its absence
# made this script report "pi -p exited non-zero" for a pi that never ran.
# The watchdog below is dependency-free; it returns the command's own exit
# status, and kills the command if it overruns.
run_with_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"; return $?
  fi
  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"; return $?
  fi
  "$@" &
  local cmd_pid=$!
  ( sleep "$secs"; kill -TERM "$cmd_pid" 2>/dev/null ) >/dev/null 2>&1 &
  local watchdog=$!
  local rc=0
  wait "$cmd_pid" || rc=$?
  kill -TERM "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  return $rc
}

# ${PI_ARGS[@]+...}: bash 3.2 (macOS /bin/bash) treats "${PI_ARGS[@]}" on an
# empty array as unbound under `set -u`. The expansion guard is the portable
# form and a no-op on newer bash.
# The agent's own exit status is not the adapter's: a provider failure makes
# `pi -p` exit non-zero while the adapter still records the attempt. The gate
# below is the record count, not the agent's status.
PI_LOG=$(mktemp)
pi_rc=0
run_with_timeout 240 pi ${PI_ARGS[@]+"${PI_ARGS[@]}"} -p "$PROMPT" \
  >"$PI_LOG" 2>&1 || pi_rc=$?
sleep 1

after=$(count_records "$TRACE_DIR")
if [ "$after" -le "$before" ]; then
  echo "FAIL — no new records (before=$before after=$after)"
  [ "$pi_rc" -ne 0 ] && { echo "       (and pi itself failed:)"; \
    tail -3 "$PI_LOG" | sed 's/^/       /'; }
  exit 1
fi
# "Some record was written" is too weak: an adapter whose provider hook broke
# still writes session_start. Require the conversation itself.
CONV=$(python3 - "$TRACE_DIR" <<'PYEOF'
import glob, json, os, sys
kinds = {"llm_request", "llm_response", "assistant_message", "tool_call", "tool_result"}
seen = 0
for path in sorted(glob.glob(os.path.join(sys.argv[1], "*.jsonl"))):
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
  echo "FAIL — the adapter wrote records but NO conversation in $TRACE_DIR"
  exit 1
fi
if [ "$pi_rc" -ne 0 ]; then
  echo "NOTE — pi exited $pi_rc and the adapter recorded the attempt anyway"
  grep -iE "404|401|403|429|expired|quota|rate limit|not found" "$PI_LOG" \
    | head -2 | sed 's/^/       /' || true
fi
rm -f "$PI_LOG"

# Locate THIS conversation's session id by the exact prompt (session_id is
# ctx.sessionManager.getSessionFile() — a full path string).
sid=$(python3 - "$TRACE_DIR" "$PROMPT" <<'PY'
import json, sys, glob
d, prompt = sys.argv[1], sys.argv[2]
# LAST match wins: the same prompt is sent on every run, so the first match is
# an older session and the render would pass without exercising this run's
# records. Newest file first, newest line first.
for p in sorted(glob.glob(d.rstrip("/") + "/*.jsonl"), reverse=True):
    with open(p, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("event") != "user_prompt":
            continue
        for m in (r.get("request") or {}).get("messages") or []:
            c = m.get("content")
            if isinstance(c, str) and c.strip() == prompt:
                print(r.get("session_id") or "", end="")
                raise SystemExit(0)
print("", end="")
PY
)

if [ -z "$sid" ]; then
  echo "FAIL — session with the e2e prompt not found"
  exit 1
fi

out=$(python3 cli/agenttrace.py watch "$TRACE_DIR" --no-follow --session "$sid" 2>&1 || true)
if ! printf '%s' "$out" | grep -q "E2E-OK"; then
  echo "FAIL — panel did not render the reply for session $sid"
  exit 1
fi

echo "PASS — pi traced to session $sid and the panel rendered it"
