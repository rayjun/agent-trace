#!/usr/bin/env python3
"""Manage the AgentTrace loopback dashboard as a Linux systemd user service."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
UNIT = "agenttrace.service"
MARKER = "# Managed by agenttrace-service.py; agenttrace-owned-unit-v1"


def quote_unit(value: str) -> str:
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    value = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Unsupported control character in service path")
    return '"' + value + '"'


def render_unit(port: int) -> str:
    platform_dir = Path(os.environ.get("AGENTTRACE_PLATFORM_DIR") or Path.home() / ".agenttrace").expanduser().resolve()
    command = [":" + sys.executable, str(ROOT / "scripts" / "serve-daemon.py"),
               "--foreground", "--host", "127.0.0.1", "--port", str(port)]
    return "\n".join([
        MARKER, "[Unit]", "Description=AgentTrace local dashboard", "", "[Service]",
        "Type=simple",
        "Environment=" + quote_unit("AGENTTRACE_PLATFORM_DIR=" + str(platform_dir)),
        "ExecStart=" + " ".join(quote_unit(arg) for arg in command),
        "Restart=on-failure", "RestartSec=3", "UMask=0077", "", "[Install]",
        "WantedBy=default.target", "",
    ])


def unit_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    if not config.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be absolute")
    return config / "systemd" / "user" / UNIT


def check_owned(path: Path) -> bool:
    if path.is_symlink():
        raise ValueError("Refusing to replace or remove a symlinked service unit")
    if not path.exists():
        return False
    if not path.is_file() or path.read_text(encoding="utf-8").splitlines()[:1] != [MARKER]:
        raise ValueError("Refusing to replace or remove a foreign service unit")
    return True


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        raise RuntimeError("systemctl --user " + args[0] + " failed; check the user systemd manager")
    return result


def report_status() -> None:
    for label, command in (("active", "is-active"), ("enabled", "is-enabled")):
        result = systemctl(command, UNIT, check=False)
        states = {"active", "inactive", "failed", "unknown", "enabled", "disabled", "static", "masked", "not-found", "activating", "deactivating", "enabled-runtime", "indirect", "linked", "linked-runtime"}
        state = result.stdout.strip()
        print(f"{label}: {state if state in states else 'unknown'}")
    user = pwd.getpwuid(os.getuid()).pw_name
    try:
        result = subprocess.run(["loginctl", "show-user", user, "--property=Linger", "--value"],
                                capture_output=True, text=True, timeout=10)
        linger = result.stdout.strip() if result.returncode == 0 else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        linger = "unknown"
    print("linger: " + (linger if linger in {"yes", "no"} else "unknown"))
    if linger != "yes":
        print("For operation after logout, ask your administrator to enable lingering for your user (loginctl enable-linger).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "status", "uninstall"])
    parser.add_argument("--dry-run", action="store_true", help="preview only; no filesystem changes or commands")
    parser.add_argument("--port", type=int, default=8741)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        if not sys.platform.startswith("linux"):
            raise ValueError("Only Linux systemd user services are supported")
        path = unit_path()
        owned = check_owned(path) if args.action != "status" else False
        if args.dry_run:
            if args.action == "install":
                render_unit(args.port)
                print(f"Would write managed unit: {path}")
                print("Would run: systemctl --user daemon-reload")
                print(f"Would run: systemctl --user enable --now {UNIT}")
            elif args.action == "uninstall":
                print(f"Would disable --now and remove managed unit: {path}" if owned else "No managed unit installed; nothing to remove")
                if owned:
                    print("Would run: systemctl --user daemon-reload")
            else:
                print("Would query user service active/enabled state and loginctl linger state")
            return 0
        if args.action == "status":
            report_status()
        elif args.action == "install":
            content = render_unit(args.port)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".agenttrace-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                check_owned(path)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            systemctl("daemon-reload")
            systemctl("enable", "--now", UNIT)
            print("Installed AgentTrace user service")
            report_status()
        elif owned:
            systemctl("disable", "--now", UNIT)
            check_owned(path)
            path.unlink()
            systemctl("daemon-reload")
            print("Uninstalled AgentTrace user service")
        else:
            print("No managed unit installed; nothing to remove")
        return 0
    except (OSError, UnicodeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, (ValueError, RuntimeError)):
            print(f"Error: {exc}", file=sys.stderr)
        else:
            print("Error: service operation failed; check filesystem permissions and user systemd availability", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
