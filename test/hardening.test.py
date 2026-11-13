#!/usr/bin/env python3
"""Regressions from the round that asked "how far is this from production".

Five defects, each of which was found by *running* the thing rather than by
reading it, and each of which fails silently in the field:

1. A `codex` on PATH that is a launcher script defeated the event probe, so
   every hook event looked unsupported and the installer wired nothing at all —
   an install that reports success and records no traces.
2. Deleting a trace's JSONL left its rows queryable forever, with no command to
   remove them. (The guard: a vanished *directory* must not wipe anything, or
   unmounting a backup drive destroys the index.)
3. `doctor` picked the file with the newest mtime, so `touch`ing an old file
   reported a live install as 30 days stale and exited 1.
4. The test suite and the local-model judge went through `http_proxy` for
   loopback traffic, so a developer with a VPN client saw failures that did not
   exist and a local judge looked unreachable.
5. The projection refresh parsed every event payload to look for one
   `session_end`, and it ran on every timer tick whether or not anything had
   changed: 8.7s per pass on a 19k-event database.

Run: python3 test/hardening.test.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILS
    if cond:
        print(f"  ok   {name}")
    else:
        FAILS += 1
        print(f"  FAIL {name}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 1. a launcher script on PATH must be resolved before probing
# ---------------------------------------------------------------------------

section("1. codex launcher scripts")


def load_installer():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "agent_trace_install_hook", ROOT / "codex" / "install_hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = load_installer()

tmp = Path(tempfile.mkdtemp(prefix="hard codex "))
fake_bin = tmp / "vendor" / "aarch64-apple-darwin" / "bin" / "codex"
fake_bin.parent.mkdir(parents=True)
magic = b"\xcf\xfa\xed\xfe"            # Mach-O 64-bit, then ASCII event names
fake_bin.write_bytes(magic + b"\x00" * 32
                     + b"UserPromptSubmit\x00PreToolUse\x00Stop\x00SessionStart\x00")
launcher = tmp / "codex"
launcher.write_text(
    "#!/bin/sh\n"
    f'PKG="{tmp}"\n'
    f'BIN="$PKG/vendor/aarch64-apple-darwin/bin/codex"\n'
    '[ ! -x "$BIN" ] && exit 1\n'
    'exec "$BIN" "$@"\n')
launcher.chmod(0o755)

targets = mod._launcher_targets(launcher)
# .resolve(): /var is a symlink to /private/var on macOS, and the launcher
# spells the path without either.
check("the launcher's exec target is resolved",
      fake_bin.resolve() in [p.resolve() for p in targets], f"got {targets}")
check("a text launcher is not treated as a binary",
      not mod._looks_like_binary(launcher))
check("the resolved target is treated as a binary", mod._looks_like_binary(fake_bin))

real_which = mod.codex_bin
try:
    mod.codex_bin = lambda: str(launcher)          # type: ignore[assignment]
    got = mod.codex_supported_events(["UserPromptSubmit", "Stop", "SessionEnd"])
    check("events present in the resolved binary report supported",
          got["UserPromptSubmit"] and got["Stop"], str(got))
    check("an event absent from the resolved binary still reports unsupported",
          got["SessionEnd"] is False, str(got))

    # A launcher that cannot be resolved must not turn into "nothing is
    # supported": that silently installs no hooks at all.
    opaque = tmp / "opaque"
    opaque.write_text("#!/bin/sh\nexec /nonexistent/codex \"$@\"\n")
    mod.codex_bin = lambda: str(opaque)            # type: ignore[assignment]
    got2 = mod.codex_supported_events(["UserPromptSubmit"])
    check("an unresolvable launcher falls back to 'cannot probe', not 'unsupported'",
          got2["UserPromptSubmit"] is True, str(got2))
finally:
    mod.codex_bin = real_which                      # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 2. the index follows deletions, but a missing directory is not a deletion
# ---------------------------------------------------------------------------

section("2. deleting a trace file")

tmp = Path(tempfile.mkdtemp(prefix="hard-del-"))
traces = tmp / "traces"
traces.mkdir()
datadir = tmp / "plat"
env = {**os.environ, "HOME": str(tmp / "h"), "AGENTTRACE_REDACT": "0",
       "AGENTTRACE_PLATFORM_DIR": str(datadir)}
(tmp / "h").mkdir()


def cli(*args: str, timeout: int = 300) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(ROOT / "cli" / "agenttrace.py"), *args],
                       capture_output=True, text=True, env=env, cwd=str(ROOT),
                       timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def iso(offset_s: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                         time.gmtime(time.time() + offset_s))


def rec(ts: str, session: str = "s1") -> str:
    return json.dumps({"v": 1, "ts": ts, "agent": "hermes", "event": "llm_request",
                       "session_id": session, "request": {"model": "m"},
                       "response": {"content": "x"}})


db = datadir / "agenttrace.db"


def count(sql: str) -> int:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return int(c.execute(sql).fetchone()[0])
    finally:
        c.close()


(traces / "a.jsonl").write_text("\n".join(rec(f"2026-10-21T10:00:0{i}.000Z")
                                          for i in range(3)) + "\n")
(traces / "b.jsonl").write_text("\n".join(rec(f"2026-10-21T10:00:0{i}.000Z", "s2")
                                          for i in range(3)) + "\n")
cli("index", str(traces))
check("both files indexed", count("SELECT COUNT(*) FROM events") == 6,
      str(count("SELECT COUNT(*) FROM events")))

(traces / "a.jsonl").unlink()
rc, out = cli("index", str(traces))
report = json.loads(out[out.index("{"):])
check("a deleted file's events are dropped", count("SELECT COUNT(*) FROM events") == 3,
      str(count("SELECT COUNT(*) FROM events")))
check("...and its ingest_files row with them",
      count("SELECT COUNT(*) FROM ingest_files") == 1)
check("the report says so instead of deleting silently",
      report.get("files_dropped") == 1, str(report.get("files_dropped")))

# the guard: a whole directory gone is an unmounted volume, not a deletion
(traces / "a.jsonl").write_text("\n".join(rec(f"2026-10-21T10:00:0{i}.000Z")
                                          for i in range(3)) + "\n")
cli("index", str(traces))
before = count("SELECT COUNT(*) FROM events")
(traces / "b.jsonl").unlink()
(traces / "a.jsonl").unlink()
traces.rmdir()                                      # e.g. the disk went away
cli("index", str(traces))
check("a vanished directory does NOT wipe the index",
      count("SELECT COUNT(*) FROM events") == before,
      f"{before} -> {count('SELECT COUNT(*) FROM events')}")

# A key written before paths were canonicalised (an older build stored
# `str(path)`, so a symlinked root produced a different spelling) must be
# respelled in place, not re-read in full and counted as two files.
(traces.parent / "traces2").mkdir()
(traces.parent / "traces2" / "c.jsonl").write_text(rec(iso(0), "s3") + "\n")
cli("index", str(traces.parent / "traces2"))
legacy_root = traces.parent / "traces2-alias"
legacy_root.symlink_to(traces.parent / "traces2", target_is_directory=True)
c = sqlite3.connect(str(db))
c.execute("UPDATE ingest_files SET path = ? WHERE path LIKE ?",
          (str(legacy_root / "c.jsonl"), "%traces2%"))
c.commit()
c.close()
rc, out = cli("index", str(traces.parent / "traces2"))
rep = json.loads(out[out.index("{"):])
check("a legacy path spelling is respelled, not re-ingested",
      rep.get("files_rekeyed") == 1 and rep.get("files_ingested") == 0
      and rep.get("events_added") == 0, str(rep))
check("...leaving exactly one row for the file",
      count("SELECT COUNT(*) FROM ingest_files WHERE path LIKE '%traces2%'") == 1)


# ---------------------------------------------------------------------------
# 3. doctor ages the newest RECORD, not the newest file
# ---------------------------------------------------------------------------

section("3. doctor capture check")


tmp = Path(tempfile.mkdtemp(prefix="hard-doctor-"))
traces = tmp / "traces"
traces.mkdir()
env = {**os.environ, "HOME": str(tmp / "h"), "AGENTTRACE_REDACT": "0",
       "AGENTTRACE_PLATFORM_DIR": str(tmp / "plat")}
(tmp / "h").mkdir()

(traces / "z-fresh.jsonl").write_text(rec(iso(0)) + "\n")
stale = traces / "a-old.jsonl"
stale.write_text(rec(iso(-30 * 86400)) + "\n")
time.sleep(0.02)
os.utime(stale, None)                       # touched last, content 30 days old

rc, out = cli("doctor", str(traces), "--fast")
cap = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("Capture")]
check("touching an old file is not reported as a stale capture",
      rc == 0 and cap and "z-fresh.jsonl" in cap[0], f"exit {rc}: {cap[:1]}")

(traces / "z-fresh.jsonl").unlink()
rc, out = cli("doctor", str(traces), "--fast")
cap = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("Capture")]
check("genuinely stale capture still warns and exits 1",
      rc == 1 and cap and "30 d" in cap[0], f"exit {rc}: {cap[:1]}")


# ---------------------------------------------------------------------------
# 4. loopback defeats an exported proxy
# ---------------------------------------------------------------------------

section("4. http_proxy does not capture loopback")


def serve_one() -> tuple[object, str, list[str]]:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    hits: list[str] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions", hits


srv, url, hits = serve_one()
saved = {k: os.environ.get(k) for k in ("http_proxy", "https_proxy", "all_proxy",
                                        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
os.environ["http_proxy"] = "http://127.0.0.1:9"     # a port nothing listens on
os.environ["https_proxy"] = os.environ["http_proxy"]
try:
    from evaluation.evaluators.llm_judge import _opener
    # `build_opener(ProxyHandler({}))` drops the handler entirely rather than
    # keeping an inert one, so "no ProxyHandler at all" is the observable form
    # of "this request cannot be proxied".
    loop = _opener(url)
    check("a loopback endpoint gets a proxy-free opener",
          not any(type(h).__name__ == "ProxyHandler" for h in loop.handlers),
          str([type(h).__name__ for h in loop.handlers]))
    far = _opener("https://api.example.com/v1/chat")
    proxied = [h for h in far.handlers if type(h).__name__ == "ProxyHandler"]
    check("a remote endpoint keeps the environment's proxy",
          bool(proxied) and bool(proxied[0].proxies),
          str([type(h).__name__ for h in far.handlers]))
    hits.clear()
    with _opener(url).open(url, timeout=5) as resp:      # type: ignore[arg-type]
        check("the loopback request reaches the server despite http_proxy",
              resp.status == 200 and len(hits) == 1, f"status={resp.status} hits={hits}")
finally:
    srv.shutdown()                                     # type: ignore[attr-defined]
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# 5. the projection refresh is bounded, and skipped when nothing changed
# ---------------------------------------------------------------------------

section("5. projection refresh cost")


tmp = Path(tempfile.mkdtemp(prefix="hard-refresh-"))
traces = tmp / "traces"
traces.mkdir()
datadir = tmp / "plat"
env = {**os.environ, "HOME": str(tmp / "h"), "AGENTTRACE_REDACT": "0",
       "AGENTTRACE_PLATFORM_DIR": str(datadir)}
(tmp / "h").mkdir()
rows = []
for i in range(4000):
    rows.append(json.dumps({
        "v": 1, "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                    time.gmtime(1761000000 + i)),
        "agent": "hermes", "event": "llm_request", "session_id": f"s{i % 4}",
        # a payload big enough that parsing all of them would dominate
        "request": {"model": "m", "content": "x" * 4000},
        "response": {"content": "y" * 4000}}))
(traces / "big.jsonl").write_text("\n".join(rows) + "\n")
(traces / "end.jsonl").write_text(json.dumps({
    "v": 1, "ts": "2026-10-21T11:00:00.000Z", "agent": "hermes",
    "event": "session_end", "session_id": "s0", "note": "done"}) + "\n")

rc, out = cli("index", str(traces))
check("indexing a 4k-record file succeeds", rc == 0, out[-300:])
c = sqlite3.connect(f"file:{datadir / 'agenttrace.db'}?mode=ro", uri=True)
ended = c.execute("SELECT session_id FROM sessions WHERE ended=1").fetchall()
c.close()
check("session_end still marks its session ended", [r[0] for r in ended] == ["s0"],
      str(ended))

from core.storage.indexer import Indexer                      # noqa: E402
from core.storage.sqlite_store import TraceStore              # noqa: E402

store = TraceStore(datadir / "agenttrace.db")
idx = Indexer(store, [traces])
start = time.perf_counter()
idx.scan_once()
first = time.perf_counter() - start
start = time.perf_counter()
report = idx.scan_once()
second = time.perf_counter() - start
store.close()
check("an unchanged rescan is cheap (no files, no refresh)", second < 0.2,
      f"{second * 1000:.0f} ms")
check("an unchanged rescan reports no work",
      report.events_added == 0 and not report.changed, str(report.__dict__))
check("a full pass over 4k records stays under 5s", first < 5.0, f"{first:.2f}s")

print()
if FAILS:
    print(f"FAILED — {FAILS} check(s)")
    sys.exit(1)
print("hardening OK")
