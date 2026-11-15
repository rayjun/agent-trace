"""Synchronize native Codex transcripts before indexing their trace records.

The existing importer owns capture/redaction/cursors. Polling calls it only
for new or changed files; hooks and polling share its per-rollout lock.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any


def codex_trace_dir() -> Path:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    return Path(os.environ.get("AGENTTRACE_CODEX_DIR") or home / "traces").expanduser()


class CodexSync:
    def __init__(self, dirs: list[Path]) -> None:
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
        self.root = home / "sessions"
        self.target = codex_trace_dir()
        self.state = Path(os.environ.get("AGENTTRACE_STATE_DIR") or self.target / ".state").expanduser()
        self.enabled = self.target.resolve() in {d.resolve() for d in dirs}
        self.importer = Path(__file__).resolve().parent.parent / "codex" / "codex_import.py"
        self.fingerprints: dict[Path, tuple[int, int, int]] = {}
        self.files = 0
        self.last_success: float | None = None
        self.errors: list[str] = []

    def poll(self, stop: threading.Event) -> None:
        self.errors = []
        if not self.enabled or not self.root.is_dir():
            return
        try:
            paths = sorted(self.root.rglob("rollout-*.jsonl"))
        except OSError as exc:
            self.errors = [f"Codex source discovery failed: {type(exc).__name__}"]
            return
        self.files = len(paths)
        for path in paths:
            if stop.is_set():
                break
            try:
                stat = path.stat()
                signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                if self.fingerprints.get(path) == signature:
                    continue
                environment = dict(os.environ, AGENTTRACE_CODEX_DIR=str(self.target),
                                   AGENTTRACE_STATE_DIR=str(self.state))
                result = subprocess.run(
                    [sys.executable, str(self.importer), "--live", str(path)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=30, env=environment)
                if result.returncode:
                    # Never display a native transcript or exception payload.
                    self.errors.append(f"Codex capture failed: {path.name}")
                else:
                    self.fingerprints[path] = signature
                    self.last_success = time.time()
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.errors.append(f"Codex capture {type(exc).__name__}: {path.name}")
        present = set(paths)
        self.fingerprints = {p: signature for p, signature in self.fingerprints.items() if p in present}

    def health(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "available": self.root.is_dir(),
                "files": self.files, "last_success": self.last_success,
                "errors": list(self.errors)}
