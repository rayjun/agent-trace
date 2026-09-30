#!/usr/bin/env bash
# Run every agent-trace test. Pi and the TUI need no network; the Hermes e2e
# makes one real model call, so it is the only slow one.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail=0
run() {
  echo "=== $1 ==="
  shift
  if "$@"; then :; else echo "FAILED: $*" >&2; fail=1; fi
}
run "codex incremental import" python3 test/codex-import.test.py
run "adapter regressions"      python3 test/adapters.test.py
run "hermes deployed plugin"   python3 test/hermes-deploy.test.py
run "release version"         python3 test/release.test.py
run "schema contract"          python3 test/schema.test.py
run "trace index"              python3 test/index.test.py
run "cache hit rate <=100%"    python3 test/cache-rate.test.py
run "pi adapter"            node    test/pi-adapter.test.mjs
run "watch TUI (pty)"       python3 test/watch-tui.test.py
if [ "${SKIP_E2E:-0}" != "1" ]; then
  run "hermes end-to-end"    bash    test/e2e-hermes.sh
  run "pi end-to-end"        bash    test/e2e-pi.sh
else
  echo "=== end-to-end (skipped: SKIP_E2E=1) ==="
fi

[ $fail -eq 0 ] && echo && echo "ALL TESTS PASSED" || { echo; echo "SOME TESTS FAILED" >&2; exit 1; }
