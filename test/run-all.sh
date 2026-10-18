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
run "show request format"      python3 test/show-format.test.py
run "system prompt stored once" python3 test/system-prompt-once.test.py
run "cache hit rate <=100%"    python3 test/cache-rate.test.py
run "v2 canonical schema"     python3 test/v2_schema.test.py
run "v1 -> v2 converter"      python3 test/v2_convert.test.py
run "adapter conformance kit" python3 test/conformance.test.py
run "sqlite store + indexer"  python3 test/storage.test.py
run "query service layer"     python3 test/query.test.py
run "local http server"       python3 test/server.test.py
run "datasets + evaluators + experiments" python3 test/evaluation.test.py
run "otlp import/export"     python3 test/otlp.test.py
run "langgraph callback adapter" python3 test/langgraph.test.py
run "adapter discovery + matrix" python3 test/discovery.test.py
run "M4 review regressions"   python3 test/review-fixes.test.py
run "pi adapter"            node    test/pi-adapter.test.mjs
run "pi backfill importer"  python3 test/pi-import.test.py
run "adapter coverage"      python3 test/coverage.test.py
run "workspace snapshot"    python3 test/workspace.test.py
run "dashboard routing"     node    test/dashboard-routing.test.mjs
run "watch TUI (pty)"       python3 test/watch-tui.test.py
if [ "${SKIP_E2E:-0}" != "1" ]; then
  run "hermes end-to-end"    bash    test/e2e-hermes.sh
  run "pi end-to-end"        bash    test/e2e-pi.sh
else
  echo "=== end-to-end (skipped: SKIP_E2E=1) ==="
fi

[ $fail -eq 0 ] && echo && echo "ALL TESTS PASSED" || { echo; echo "SOME TESTS FAILED" >&2; exit 1; }
