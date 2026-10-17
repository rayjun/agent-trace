"""The reference EventSink: locked append to a JSONL file.

This is the v2 counterpart of the write path in common/agenttrace_common.py
(same thread lock + flock, same fail-open rule), written against the canonical
event model. It duplicates ~30 lines of the v1 helper deliberately: the v1
module stays untouched until the three adapters migrate, and the platform
layers must not depend on code that gets byte-copied into a host process.

Rules kept from v1, because they are what made the traces trustworthy:

* Cross-process safety via flock — the codex hook is a fresh process per
  event, so a thread lock alone would interleave lines.
* Fail OPEN. emit() returns False on I/O error and counts it; it never raises
  into the host agent. An observability layer that can take down the observed
  process is a liability, not a feature.
* Validation is ON here (unlike v1's opt-in): this sink serves new code where
  the cost of one check per event is fine, and catching a contract violation
  at the write site is how it gets fixed at the source.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

try:                      # POSIX append locking; optional so tests still run
    import fcntl
except ImportError:       # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

from core.privacy import redact_event
from core.schema.model import validate_event

_write_lock = threading.Lock()

VALIDATE_ENV = "AGENTTRACE_SDK_VALIDATE"
REDACT_ENV = "AGENTTRACE_SDK_REDACT"


def _validate_on() -> bool:
    return (os.environ.get(VALIDATE_ENV) or "1").strip().lower() not in (
        "0", "false", "no", "off")


def _redact_on() -> bool:
    return (os.environ.get(REDACT_ENV) or "1").strip().lower() not in (
        "0", "false", "no", "off")


class JsonlEventSink:
    """Appends canonical events to one JSONL file. Thread + process safe.

    The write path is REDACT -> validate -> locked append. Redaction happens
    here because this is the last moment the raw value exists: a path that
    wrote first and redacted later would put a plaintext secret on disk
    (decision D7). Validate runs after redaction so a record that only becomes
    contract-violating once scrubbed is still caught.
    """

    def __init__(self, path: str | Path, *, validate: bool | None = None,
                 redact: bool | None = None) -> None:
        self.path = Path(path)
        self.validate = _validate_on() if validate is None else validate
        self.redact = _redact_on() if redact is None else redact
        self.events_written = 0
        self.write_errors = 0
        self.redacted_events = 0
        self.violations: list[str] = []

    def emit(self, event: dict[str, Any]) -> bool:
        if self.redact:
            event = redact_event(event)
            self.redacted_events += 1
        if self.validate:
            problems = validate_event(event)
            if problems:
                # A contract violation is a bug in the ADAPTER, not an I/O
                # condition: count it, remember it for the conformance report,
                # and refuse to write — persisting it would poison the index.
                self.write_errors += 1
                self.violations.extend(
                    f"{event.get('event_id', '?')}: {p}" for p in problems)
                return False
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        try:
            self._append(line)
        except OSError:
            self.write_errors += 1
            return False
        self.events_written += 1
        return True

    def emit_many(self, events: list[dict[str, Any]]) -> int:
        return sum(1 for e in events if self.emit(e))

    def _append(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.write(line + "\n")
                finally:
                    if fcntl is not None:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def close(self) -> None:
        """Present for symmetry with future buffered sinks; appends are direct."""
        return None
