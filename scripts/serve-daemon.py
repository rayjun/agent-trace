#!/usr/bin/env python3
"""Start the dashboard as a process that outlives the shell that launched it.

Why this exists: `nohup agenttrace serve &` is not enough. nohup only ignores
SIGHUP — the process stays in the launcher's process GROUP, so anything that
cleans up that group (a test harness reaping a command, a terminal closing, a
`kill -- -PGID`) takes the dashboard with it. The log then ends mid-line and
the symptom is just "the page cannot connect".

macOS has no `setsid(1)`, so this does the two forks by hand: the parent exits
immediately (the launcher returns at once), the child calls setsid() to become
a session leader with no controlling terminal, and the grandchild execs the
server. Whatever happens to the original process group, the server is in its
own session.

    python3 scripts/serve-daemon.py --port 8741

Writes ~/.agenttrace/server.pid and ~/.agenttrace/server.log; stop it with
`agenttrace serve --stop` or by killing the pid in that file.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def platform_dir() -> Path:
    override = os.environ.get("AGENTTRACE_PLATFORM_DIR")
    return Path(override).expanduser() if override else Path.home() / ".agenttrace"


def daemonize(log_path: Path) -> None:
    """Return in the parent; continue in a detached grandchild."""
    if os.fork() > 0:
        os._exit(0)                     # launcher returns to the caller's shell
    os.setsid()                         # new session, no controlling terminal
    if os.fork() > 0:
        os._exit(0)                     # the session leader must not be the server
    os.chdir(str(ROOT))
    os.umask(0o077)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    os.dup2(handle, 1)
    os.dup2(handle, 2)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8741)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="trace-dir scan interval in seconds")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--foreground", action="store_true",
                        help="do not detach (useful for debugging)")
    parser.add_argument("dirs", nargs="*", type=Path,
                        help="trace dirs (default: the per-agent locations)")
    args = parser.parse_args(argv)

    log = platform_dir() / "server.log"
    if not args.foreground:
        print(f"detaching; pid file {platform_dir() / 'server.pid'}, log {log}")
        daemonize(log)

    # Import AFTER detaching: a failure here lands in the log file, not in a
    # terminal that is already gone.
    from core.storage.discovery import default_trace_dirs
    from server.main import load_or_create_token, platform_dir as _pd, serve

    dirs = [d.expanduser().resolve() for d in args.dirs] or default_trace_dirs()
    db = args.db or platform_dir() / "agenttrace.db"
    print(f"\n=== agent-trace serve started {datetime.now().isoformat(timespec='seconds')} ===")
    print(f"pid={os.getpid()} db={db} dirs={[str(d) for d in dirs]}")
    try:
        serve(host=args.host, port=args.port, db_path=db, dirs=dirs,
              scan_interval=args.interval, verbose=True)
    except Exception:                       # noqa: BLE001
        import traceback
        traceback.print_exc()
        return 1
    finally:
        print(f"=== stopped {datetime.now().isoformat(timespec='seconds')} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
