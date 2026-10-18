#!/usr/bin/env python3
"""The local HTTP server: routes, auth gates, SSE.

The gates are the point (docs/decisions.md D10): a loopback service is one
malicious-website-with-CORS-reading away from leaking every prompt on the
machine, so Host allowlisting, Origin checks and the bearer token are
tested as first-class behavior, not configuration trivia.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402
from server.main import make_server  # noqa: E402

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


# fixture: one trace in one file
td = Path(tempfile.mkdtemp())
traces_dir = td / "traces"
traces_dir.mkdir()
(traces_dir / "a.jsonl").write_text(json.dumps({
    "v": 1, "ts": "2026-10-17T10:00:00.000Z", "agent": "hermes",
    "event": "llm_request", "session_id": "s1", "turn_id": "t1",
    "request_id": "r1", "model": "m1"}) + "\n" + json.dumps({
    "v": 1, "ts": "2026-10-17T10:00:01.000Z", "agent": "hermes",
    "event": "llm_response", "session_id": "s1", "turn_id": "t1",
    "request_id": "r1", "response": {"content": "hi",
                                     "usage": {"input_tokens": 7}}}) + "\n",
    encoding="utf-8")

store = TraceStore(td / "srv.db")
indexer = Indexer(store, [traces_dir])
server = make_server(store, indexer, host="127.0.0.1", port=0,
                     token="test-token", scan_interval=0.2)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

BASE = f"http://127.0.0.1:{port}"


def req(path: str, headers: dict[str, str] | None = None,
        method: str = "GET") -> tuple[int, dict | str]:
    r = urllib.request.Request(BASE + path, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        # Error bodies are JSON too, and asserting on them is the point of the
        # failure-path checks — returning the raw string made every one of
        # those assertions compare a dict against a str.
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


# index triggers one scan synchronously so the assertions below are ordered
indexer.scan_once()

code, body = req("/api/health?token=test-token")
check("health ok with token", code == 200 and body["ok"] is True)

code, _ = req("/api/health")
check("api without token -> 401", code == 401)

code, _ = req("/api/health?token=wrong")
check("wrong token -> 401", code == 401)

code, body = req("/api/health",
                 headers={"Authorization": "Bearer test-token"})
check("bearer header accepted", code == 200)

code, _ = req("/api/health?token=test-token",
              headers={"Host": "evil.com"})
check("foreign Host header -> 403 (DNS rebinding)", code == 403)

code, _ = req("/api/health?token=test-token",
              headers={"Origin": "https://evil.com"})
check("foreign Origin -> 403", code == 403)

code, body = req("/")
check("index served without token", code == 200 and "agent-trace" in body)
# The assertion that matters is not "the token string appears somewhere" — it
# did, while the page was completely broken: a blind replace had rewritten the
# PROPERTY name too, so the page set `window.<token>` and the SPA read back
# undefined and sent an empty token. Execute the served bootstrap the way a
# browser does and check the global it actually defines.
import re as _re  # noqa: E402
import subprocess as _sp  # noqa: E402

scripts = _re.findall(r"<script>(.*?)</script>", body, _re.DOTALL)
check("the page has an inline bootstrap", bool(scripts), str(len(scripts)))
if scripts:
    # Two independent requirements, and the second is the one that was missing:
    # the assignment must target the property `__AGENTTRACE_TOKEN__` (a blind
    # replace of the placeholder had renamed it, so the page set
    # `window.<token>` and the SPA read back undefined) AND its value must be
    # the real token.
    assigned = _re.search(r'window\.(__?[A-Za-z_][\w]*)\s*=\s*"([^"]*)"', scripts[0])
    check("the served page assigns window.__AGENTTRACE_TOKEN__",
          bool(assigned) and assigned.group(1) == "__AGENTTRACE_TOKEN__",
          scripts[0].strip()[:120])
    check("...to the real token",
          bool(assigned) and assigned.group(2) == "test-token",
          str(assigned.group(2) if assigned else None))
    # Where a JS engine is available, execute the bootstrap the way a browser
    # does. The regex above states the shape; this states the behaviour, and
    # behaviour is what a dashboard needs.
    import shutil as _sh  # noqa: E402
    if _sh.which("node"):
        probe = (
            "const window = {};" + scripts[0]
            + "process.stdout.write(String(window.__AGENTTRACE_TOKEN__));"
        )
        out = _sp.run(["node", "-e", probe], capture_output=True, text=True)
        check("executing the bootstrap yields the token (as a browser would)",
              out.stdout.strip() == "test-token",
              f"stdout={out.stdout!r} stderr={out.stderr[-160:]!r}")
    else:
        print("  skip node-based bootstrap execution (node not installed)")
    check("...with the value placeholder fully substituted",
          "__AGENTTRACE_TOKEN_VALUE__" not in body
          and body.count("__AGENTTRACE_TOKEN__") == 1,
          f"identifier x{body.count('__AGENTTRACE_TOKEN__')}, "
          f"value-placeholder present={'__AGENTTRACE_TOKEN_VALUE__' in body}")

with urllib.request.urlopen(BASE + "/", timeout=5) as resp:
    check("no Access-Control-Allow-Origin header",
          resp.headers.get("Access-Control-Allow-Origin") is None)

code, body = req("/api/overview?token=test-token")
check("overview over the fixture",
      code == 200 and body["traces"] == 1 and body["tokens"]["input"] == 7)

code, body = req("/api/traces?token=test-token")
check("trace list has the fixture trace",
      code == 200 and body["total"] == 1)
trace_id = body["traces"][0]["trace_id"]

code, body = req(f"/api/traces/{trace_id}?token=test-token")
check("trace detail has two spans + synthetic root",
      code == 200 and len(body["spans"]) == 2)
check("unknown trace -> 404", req("/api/traces/nope?token=test-token")[0] == 404)

code, body = req("/api/sessions?token=test-token")
check("sessions endpoint", code == 200 and len(body["sessions"]) == 1)

# A session id is usually a FILE PATH, so it arrives percent-escaped. It must
# resolve with exactly one decode, and a double-escaped id (what an older
# dashboard put in the URL bar) must still resolve rather than 404 — the
# bookmarks people already have are not going to fix themselves.
sid = body["sessions"][0]["session_id"]
one = urllib.parse.quote(sid, safe="")
two = urllib.parse.quote(one, safe="")
code, detail = req(f"/api/sessions/{one}?token=test-token")
check("a session id with slashes resolves (single-escaped)",
      code == 200 and detail.get("session_id") == sid,
      f"{code} {str(detail)[:80]}")
code, detail = req(f"/api/sessions/{two}?token=test-token")
check("...and a double-escaped one still resolves (old bookmarks)",
      code == 200 and detail.get("session_id") == sid,
      f"{code} {str(detail)[:80]}")
code, _ = req("/api/sessions/nope-not-a-session?token=test-token")
check("an unknown session still 404s", code == 404, str(code))

code, body = req("/api/adapters?token=test-token")
check("adapters endpoint joins registry + observed",
      code == 200
      and any(a["adapter_id"] == "hermes" and a["observed"]
              for a in body["adapters"]))

code, _ = req("/static/../../etc/passwd")
check("static traversal refused", code == 404)

code, body = req("/static/app.js")
check("static js served", code == 200 and "EventSource" in body)
check("the SPA prefers the URL's token over the injected one",
      'new URLSearchParams(location.search).get("token")' in body, body[:200])
check("the SPA explains a missing token instead of 401-ing silently",
      "missingTokenNotice" in body and "no access token" in body)

code, body = req("/api/reindex", method="POST",
                 headers={"Authorization": "Bearer test-token"})
check("reindex rebuilds without duplicating",
      code == 200 and body["ok"] is True
      and store.one("SELECT COUNT(*) AS n FROM events")["n"] == 2)

# SSE: connect, then make a change, expect a change frame
sse_result: dict[str, str] = {}


def read_sse() -> None:
    try:
        r = urllib.request.urlopen(BASE + "/api/stream?token=test-token",
                                   timeout=5)
        # read1: return as soon as ANY bytes arrive — read(64) would park
        # until the buffer fills, which a heartbeat stream never does.
        sse_result["first"] = r.read1(64).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - report whatever happened
        sse_result["first"] = f"ERROR: {exc}"


sse_thread = threading.Thread(target=read_sse, daemon=True)
sse_thread.start()
import time  # noqa: E402

time.sleep(0.3)  # let the SSE client subscribe
(traces_dir / "a.jsonl").open("a", encoding="utf-8").write(json.dumps({
    "v": 1, "ts": "2026-10-17T10:00:02.000Z", "agent": "hermes",
    "event": "note", "session_id": "s1", "note": "late"}) + "\n")
report = indexer.scan_once()
if report.changed:
    with server.state.cond:
        server.state.version += 1
        server.state.cond.notify_all()
sse_thread.join(timeout=5)
check("SSE greets and stays open",
      "retry:" in sse_result.get("first", ""), sse_result.get("first", "")[:60])

# A handler exception must answer 500 and LEAVE THE SERVER RUNNING. It used to
# escape into socketserver's default handler, which printed the traceback to
# stderr and then (in a detached process) lost it, so the only evidence was a
# `-----` separator in the log and a page that could not connect.
class _BrokenStore:
    def __getattr__(self, name):
        def boom(*_a, **_kw):
            raise RuntimeError(f"store.{name} exploded")
        return boom


_real_store = server.state.store
server.state.store = _BrokenStore()
code, body = req("/api/overview?token=test-token")
check("a handler exception answers 500 instead of killing the server",
      code == 500 and isinstance(body, dict) and body.get("error"),
      f"{code} {str(body)[:120]}")
check("the server is still serving after that exception",
      req("/api/health?token=test-token")[0] == 500
      or req("/api/health?token=test-token")[0] == 200)
server.state.store = _real_store
code, _ = req("/api/overview?token=test-token")
check("...and works again once the fault is gone", code == 200, str(code))

# the pid file is how a detached dashboard is found and stopped
from server.main import write_pidfile  # noqa: E402
import os as _os  # noqa: E402
_os.environ["AGENTTRACE_PLATFORM_DIR"] = str(td)
path = write_pidfile(12345)
check("write_pidfile records the pid",
      path.is_file() and path.read_text().strip() == "12345", str(path))

server.state.stop()
server.shutdown()
server.server_close()
store.close()

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("HTTP server OK")
