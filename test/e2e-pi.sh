#!/usr/bin/env bash
# End-to-end: a real pi session must land in the trace file the panel tails,
# with the reply renderable under its own session (no latest-file races —
# locate the session by the exact prompt, like e2e-hermes.sh).
# Skips (exit 0) when pi or the extension is not installed.
set -uo pipefail
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

before=$(cat "$TRACE_DIR"/*.jsonl 2>/dev/null | wc -l)

if ! timeout 240 pi "${PI_ARGS[@]}" -p "$PROMPT" >/dev/null 2>&1; then
  echo "FAIL — pi -p exited non-zero (model=${PI_E2E_MODEL:-default}; pass PI_E2E_MODEL if the default is unavailable)"
  exit 1
fi
sleep 1

after=$(cat "$TRACE_DIR"/*.jsonl 2>/dev/null | wc -l)
if [ "$after" -le "$before" ]; then
  echo "FAIL — no new records (before=$before after=$after)"
  exit 1
fi

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
