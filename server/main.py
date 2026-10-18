"""agent-trace local HTTP server — stdlib only, deliberately not FastAPI.

Why stdlib (decision D9): this repo installs by symlink from a checkout, with
no packaging step; requiring `pip install fastapi uvicorn` would make the
dashboard the first component that cannot run from a bare clone. The API
surface below is small, JSON-shaped and transport-thin over core/query — if a
packaging story ever exists, FastAPI can re-expose the same routes without
breaking clients.

Security model for a loopback service (docs/decisions.md D10):

* Bind 127.0.0.1 by default. AGENTTRACE_HOST overrides; anything else prints
  a loud warning, because off-loopback turns every mitigation below into a
  requirement rather than a backstop.
* Host header allowlist {localhost, 127.0.0.1, [::1]} — DNS-rebinding
  defense: a browser pointed at attacker-controlled DNS resolving to
  127.0.0.1 arrives with the attacker's Host and is rejected.
* No CORS headers, ever: a cross-origin page can fire requests but cannot
  read a single byte of the responses.
* Origin header, when present, must itself be loopback.
* Bearer token on /api/*: generated on first run into
  ~/.agenttrace/server.token (0600), printed once at startup, injectable via
  AGENTTRACE_TOKEN. The dashboard receives it through the served index.html —
  which is safe because reading '/' from a browser context is already blocked
  by the checks above, and a local same-UID process could read the token
  file anyway.

SSE: /api/stream pushes {"type":"change"} after any ingest pass that added
events; clients refetch. Heartbeat every 15 s keeps proxies and readers
honest. ThreadingHTTPServer parks one thread per SSE client, which is fine
for a single-user local tool.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from core.query import get_trace, list_adapters, list_sessions, list_traces, overview
from core.storage.indexer import Indexer, ScanReport
from core.storage.sqlite_store import TraceStore

DEFAULT_PORT = 8741
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
_STATIC_DIR = Path(__file__).resolve().parent / "static"

_TOKEN_ENV = "AGENTTRACE_TOKEN"

#: Substituted in index.html with the token. Kept distinct from the
#: `window.__AGENTTRACE_TOKEN__` PROPERTY name so a replace cannot mangle it.
TOKEN_PLACEHOLDER = "__AGENTTRACE_TOKEN_VALUE__"


def platform_dir() -> Path:
    """~/.agenttrace — DB, token, future server state. Distinct from the
    ~/.agent-trace trace dir (which is scanned for *.jsonl; the DB must not
    live inside its own scan scope)."""
    override = os.environ.get("AGENTTRACE_PLATFORM_DIR")
    return Path(override).expanduser() if override else Path.home() / ".agenttrace"


def load_or_create_token() -> str:
    if os.environ.get(_TOKEN_ENV):
        return os.environ[_TOKEN_ENV]
    path = platform_dir() / "server.token"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass  # read-only home: token still protects this process's lifetime
    return token


class ServerState:
    """Everything handlers need, in one place, no module-level globals."""

    def __init__(self, store: TraceStore, indexer: Indexer, token: str,
                 scan_interval: float = 1.0) -> None:
        self.store = store
        self.indexer = indexer
        self.token = token
        self.scan_interval = scan_interval
        self.started_at = time.time()
        self.version = 0            # bumped whenever ingest adds events
        self.cond = threading.Condition()
        self.last_scan = ScanReport()
        self.scanning = False
        #: Passes that finished. A coverage verdict computed from an index that
        #: has not completed its first pass is not "the adapter captures
        #: nothing", it is "we do not know yet" — and reporting the former is a
        #: false alarm on every fresh start.
        self.scans_completed = 0
        self._stop = threading.Event()

    def indexer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                report = self.indexer.scan_once()
            except Exception as exc:  # never let the loop die silently
                report = ScanReport(errors=[f"scan failed: {exc}"])
            self.last_scan = report
            with self.cond:
                self.scans_completed += 1
                self.cond.notify_all()
            if report.changed:
                with self.cond:
                    self.version += 1
                    self.cond.notify_all()
            self._stop.wait(self.scan_interval)

    def wait_for_first_scan(self, timeout: float = 60.0) -> bool:
        """Block until the index can answer a coverage question."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.scans_completed == 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.cond.wait(timeout=remaining)
        return True

    def stop(self) -> None:
        self._stop.set()
        with self.cond:
            self.cond.notify_all()


def _json_body(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agent-trace/2"

    # -- plumbing ---------------------------------------------------------

    @property
    def state(self) -> ServerState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes,
              content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj: Any, status: int = 200) -> None:
        self._send(status, _json_body(obj))

    # -- security gates -----------------------------------------------------

    def _host_ok(self) -> bool:
        # Strip the port the way HTTP does, not by splitting on ':' — an IPv6
        # Host arrives as '[::1]:8741', where a naive split yields '[' and
        # rejects legitimate loopback traffic.
        host = self.headers.get("Host") or ""
        if host.startswith("["):
            host = host[1:host.index("]")] if "]" in host else host
        else:
            host = host.split(":")[0]
        host = host.lower().rstrip(".")   # `localhost.` is a legal FQDN form
        return host in _LOOPBACK_HOSTS or not host  # empty: HTTP/1.0 tools

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # non-browser clients send none
        host = urlparse(origin).hostname or ""
        return host.lower() in _LOOPBACK_HOSTS

    def _token_ok(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        expected = f"Bearer {self.state.token}"
        if hmac.compare_digest(auth, expected):
            return True
        qs = parse_qs(urlparse(self.path).query)
        candidate = qs.get("token", [""])[0]
        # Constant-time compare: a length-varying `==` on a secret leaks its
        # prefix through timing to any local process that can probe the port.
        return hmac.compare_digest(candidate, self.state.token)

    def _guard(self, api: bool) -> bool:
        """Run the gates in order; send the rejection and return False on fail."""
        if not self._host_ok():
            self._send_json({"error": "Host header not allowed"}, 403)
            return False
        if not self._origin_ok():
            self._send_json({"error": "Origin not allowed"}, 403)
            return False
        if api and not self._token_ok():
            self._send_json({"error": "missing or invalid token"}, 401)
            return False
        return True

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        try:
            self._do_get()
        except (BrokenPipeError, ConnectionResetError):
            return                      # the client went away; not our problem
        except Exception:               # noqa: BLE001
            # One bad request must not take the dashboard down, and it must not
            # vanish either: this used to escape into socketserver's default
            # handler, which prints to stderr and (when the process is being
            # reaped) loses the traceback entirely — leaving only the
            # `-----` separator in the log as evidence that anything happened.
            self._fail("GET", self.path)

    def _do_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            if not self._guard(api=False):
                return
            self._serve_index()
            return
        if path.startswith("/static/"):
            if not self._guard(api=False):
                return
            self._serve_static(path[len("/static/"):])
            return
        if path == "/api/stream":
            if not self._guard(api=True):
                return
            self._serve_sse()
            return
        if path.startswith("/api/"):
            if not self._guard(api=True):
                return
            self._route_api(path, qs)
            return
        self._send_json({"error": "not found"}, 404)

    def _read_json(self) -> dict[str, Any] | None:
        """Parse a JSON POST body; None (after sending 400) on any problem."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 10_000_000:
            self._send_json({"error": "missing or oversized body"}, 400)
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "body is not valid JSON"}, 400)
            return None
        if not isinstance(body, dict):
            self._send_json({"error": "body must be a JSON object"}, 400)
            return None
        return body

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._do_post()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:               # noqa: BLE001
            self._fail("POST", self.path)

    def _fail(self, method: str, path: str) -> None:
        """Log the traceback and answer 500 instead of dying quietly."""
        import traceback
        traceback.print_exc()
        try:
            self._send_json({"error": "internal error", "path": path}, 500)
        except Exception:               # noqa: BLE001 - the client may be gone
            pass

    def _do_post(self) -> None:
        parsed = urlparse(self.path)
        if not self._guard(api=True):
            return
        path = parsed.path
        if path == "/api/reindex":
            report = self.state.indexer.full_rebuild()
            with self.state.cond:
                self.state.version += 1
                self.state.cond.notify_all()
            self._send_json({"ok": True, "report": report.__dict__})
            return
        self._route_post(path)

    def _route_post(self, path: str) -> None:
        from evaluation import datasets as ds_mod
        from evaluation import experiments as ex_mod

        store = self.state.store
        if path == "/api/datasets":
            body = self._read_json()
            if body is None:
                return
            if not body.get("name"):
                self._send_json({"error": "name is required"}, 400)
                return
            self._send_json(ds_mod.create_dataset(
                store, str(body["name"]), str(body.get("description") or "")),
                status=201)
            return
        if path == "/api/experiments":
            body = self._read_json()
            if body is None:
                return
            try:
                self._send_json(ex_mod.create_experiment(
                    store, str(body["name"]), str(body["dataset_id"]),
                    list(body.get("evaluator_ids") or []),
                    body.get("agent_config") or {}), status=201)
            except (KeyError, TypeError) as exc:
                self._send_json({"error": str(exc)}, 400)
            return

        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[1] == "datasets":
            dataset_id, action = parts[2], parts[3] if len(parts) > 3 else ""
            if action == "examples":
                body = self._read_json()
                if body is None:
                    return
                if not isinstance(body.get("inputs"), dict):
                    self._send_json({"error": "inputs (object) is required"}, 400)
                    return
                ex = ds_mod.add_example(store, dataset_id, body["inputs"],
                                        body.get("expected"),
                                        body.get("metadata"))
                self._send_json(ex.__dict__, status=201)
                return
            if action == "from_trace":
                body = self._read_json()
                if body is None:
                    return
                ex = ds_mod.from_trace(store, dataset_id,
                                       str(body.get("trace_id") or ""),
                                       body.get("expected"))
                if ex is None:
                    self._send_json({"error": "trace not found"}, 404)
                    return
                self._send_json(ex.__dict__, status=201)
                return
            if action == "import":
                body = self._read_json()
                if body is None:
                    return
                result = ds_mod.import_jsonl(store, str(body.get("jsonl") or ""),
                                             dataset_id=dataset_id)
                self._send_json(result)
                return
        if len(parts) == 4 and parts[1] == "experiments":
            experiment_id, action = parts[2], parts[3]
            if action == "attach":
                body = self._read_json()
                if body is None:
                    return
                run_id = ex_mod.attach(store, experiment_id,
                                       str(body.get("example_id") or ""),
                                       str(body.get("trace_id") or ""))
                self._send_json({"run_id": run_id}, status=201)
                return
            if action == "run":
                try:
                    self._send_json(ex_mod.run_experiment(store, experiment_id))
                except KeyError as exc:
                    self._send_json({"error": str(exc)}, 404)
                return
        self._send_json({"error": "not found"}, 404)

    def _route_api(self, path: str, qs: dict[str, list[str]]) -> None:
        store = self.state.store
        if path == "/api/health":
            scan = self.state.last_scan
            self._send_json({
                "ok": True,
                "version": self.state.version,
                "building": self.state.scans_completed == 0,
                "uptime_s": int(time.time() - self.state.started_at),
                "last_scan": {"files": scan.files_seen,
                              "events_added": scan.events_added,
                              # dedup is routine (a re-read of a file we
                              # already hold); `problems` is what deserves
                              # attention.
                              "events_deduped": scan.events_deduped,
                              "corrupt_lines": scan.corrupt_lines,
                              "problems": scan.problems,
                              "errors": scan.errors[:5]},
            })
        elif path == "/api/overview":
            self._send_json(overview(store))
        elif path == "/api/traces":
            self._send_json(list_traces(
                store,
                agent=_one(qs, "agent"), status=_one(qs, "status"),
                since=_one(qs, "since"),
                limit=_int(qs, "limit", 50), offset=_int(qs, "offset", 0)))
        elif path.startswith("/api/traces/"):
            trace_id = path[len("/api/traces/"):]
            trace = get_trace(store, trace_id)
            if trace is None:
                self._send_json({"error": "trace not found"}, 404)
            else:
                self._send_json(trace)
        elif path == "/api/sessions":
            self._send_json(list_sessions(store, limit=_int(qs, "limit", 100)))
        elif path.startswith("/api/sessions/"):
            # A session is a CONVERSATION: its turns, each with its own duration.
            # A trace is one turn, so this is the only place the per-turn time is
            # visible at all.
            from core.query import session_detail
            raw = path[len("/api/sessions/"):]
            detail = session_detail(store, unquote(raw))
            if detail is None and "%25" in raw.upper():
                # An id that is itself still escaped: older versions of the
                # dashboard escaped the route segment twice, so URLs already in
                # someone's history carry `%252F`. Decoding once more costs
                # nothing (a session id never contains a literal `%`) and makes
                # those bookmarks work instead of 404-ing.
                detail = session_detail(store, unquote(unquote(raw)))
            self._send_json(detail if detail is not None
                            else {"error": "session not found"},
                            200 if detail is not None else 404)
        elif path == "/api/adapters":
            self._send_json(list_adapters(
                store, index_ready=self.state.scans_completed > 0))
        elif path == "/api/evaluators":
            from evaluation.evaluators import all_evaluators
            self._send_json({"evaluators": [
                {"evaluator_id": e.evaluator_id, "kind": e.kind,
                 "version": e.version, "description": e.description}
                for e in all_evaluators()]})
        elif path == "/api/datasets":
            from evaluation import datasets as ds_mod
            self._send_json({"datasets": ds_mod.list_datasets(store)})
        elif path.startswith("/api/datasets/"):
            from evaluation import datasets as ds_mod
            parts = path.strip("/").split("/")
            dataset_id = parts[2]
            if len(parts) == 4 and parts[3] == "export":
                self._send(200, ds_mod.export_jsonl(store, dataset_id)
                           .encode("utf-8"), "application/x-ndjson")
                return
            ds = ds_mod.get_dataset(store, dataset_id)
            self._send_json(ds if ds is not None else {"error": "not found"},
                            200 if ds is not None else 404)
        elif path == "/api/experiments":
            from evaluation import experiments as ex_mod
            self._send_json({"experiments": ex_mod.list_experiments(store)})
        elif path.startswith("/api/experiments/"):
            from evaluation import experiments as ex_mod
            exp = ex_mod.get_experiment(store, path.strip("/").split("/")[2])
            self._send_json(exp if exp is not None else {"error": "not found"},
                            200 if exp is not None else 404)
        elif path == "/api/compare":
            from evaluation import experiments as ex_mod
            try:
                self._send_json(ex_mod.compare(
                    store, _one(qs, "a") or "", _one(qs, "b") or ""))
            except KeyError as exc:
                self._send_json({"error": str(exc)}, 404)
        else:
            self._send_json({"error": "not found"}, 404)

    # -- static + SSE -------------------------------------------------------

    def _serve_index(self) -> None:
        index = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        # Token bootstrap: see module docstring for why serving it here is safe.
        # Only the VALUE placeholder is substituted. Replacing the property name
        # too produced `window.<token> = "<token>"`, so the page set a global
        # nobody reads and every API call went out with an empty token.
        index = index.replace("__AGENTTRACE_TOKEN_VALUE__", self.state.token)
        if TOKEN_PLACEHOLDER in index:
            # A page whose bootstrap failed must say so, not silently 401.
            print("agent-trace: index.html still contains the token placeholder",
                  flush=True)
        self._send(200, index.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_static(self, name: str) -> None:
        # no path traversal: exactly one path segment, known suffixes only
        if "/" in name or ".." in name:
            self._send_json({"error": "not found"}, 404)
            return
        types = {".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml"}
        suffix = Path(name).suffix
        file = _STATIC_DIR / name
        if suffix not in types or not file.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        self._send(200, file.read_bytes(), types[suffix])

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            seen = self.state.version
            while True:
                with self.state.cond:
                    self.state.cond.wait(timeout=15.0)
                    version = self.state.version
                if version != seen:
                    seen = version
                    self.wfile.write(
                        b'data: {"type":"change","version":%d}\n\n' % version)
                else:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # client went away; the thread ends with it


def _one(qs: dict[str, list[str]], key: str) -> str | None:
    values = qs.get(key)
    return values[0] if values else None


def _int(qs: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return max(0, int(_one(qs, key) or default))
    except ValueError:
        return default


def make_server(store: TraceStore, indexer: Indexer, *, host: str, port: int,
                token: str, scan_interval: float = 1.0,
                verbose: bool = False) -> ThreadingHTTPServer:
    state = ServerState(store, indexer, token, scan_interval)

    class _Server(ThreadingHTTPServer):
        daemon_threads = True

    server = _Server((host, port), Handler)
    # Attached post-construction: assigning `state = state` inside the class
    # body compiles the name as LOAD_NAME, which skips the enclosing scope.
    server.state = state  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def write_pidfile(pid: int) -> Path:
    path = platform_dir() / "server.pid"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{pid}\n", encoding="utf-8")
    except OSError:
        pass
    return path


def serve(*, host: str, port: int, db_path: Path, dirs: list[Path],
          scan_interval: float = 1.0, verbose: bool = False,
          pidfile: bool = True) -> None:
    store = TraceStore(db_path)
    indexer = Indexer(store, dirs)
    token = load_or_create_token()
    server = make_server(store, indexer, host=host, port=port, token=token,
                         scan_interval=scan_interval, verbose=verbose)
    if pidfile:
        write_pidfile(os.getpid())
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {host} — this exposes every trace on this "
              "machine to the network. The token is the only barrier.",
              flush=True)
    loop = threading.Thread(target=server.state.indexer_loop, daemon=True)
    loop.start()
    url = f"http://{host}:{server.server_address[1]}/?token={token}"
    print(f"agent-trace dashboard: {url}", flush=True)
    print(f"indexing {len(dirs)} trace dir(s); Ctrl-C to stop", flush=True)
    # Say it once, at startup: an agent that was already running when its
    # adapter was installed writes nothing, and the dashboard looks merely
    # empty rather than broken. WAIT for the first index pass first — a verdict
    # computed from a half-built index reports "capturing nothing" for every
    # adapter whose sessions have not been ingested yet, which is a false alarm
    # on every fresh start.
    try:
        if server.state.wait_for_first_scan():
            from core import coverage as _cov
            _provider = _cov.store_session_provider(db_path)
            for line in [c.warning() for c in _cov.check(traced_provider=_provider)
                         if c.warning()]:
                print(f"WARNING: {line}", flush=True)
        else:
            print("(coverage check deferred: the first index pass is still "
                  "running)", flush=True)
    except Exception as exc:                    # noqa: BLE001
        print(f"(coverage check unavailable: {exc})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.state.stop()
        server.server_close()
        store.close()
