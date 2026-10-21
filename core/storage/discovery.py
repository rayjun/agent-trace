"""Where trace files live — the single source of truth for discovery.

This logic used to be private to cli/agenttrace.py. The indexer is now the
primary consumer (it feeds the store the CLI will also read), so the rules
moved here; the CLI imports them. Profile-awareness (hermes writes under
$HERMES_HOME/traces AND each named profile's dir) and the Pi double-location
(~/.pi/agent/traces vs ~/.pi/traces) are exactly the kind of subtlety that
must not drift between two copies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator


def default_trace_dirs(home: Path | None = None) -> list[Path]:
    """Every directory adapters may write into. Missing dirs are skipped."""
    home = home or Path.home()
    candidates: list[Path] = []

    if os.environ.get("AGENTTRACE_HOME"):
        candidates.append(Path(os.environ["AGENTTRACE_HOME"]))

    hermes_homes = [home / ".hermes"]
    if os.environ.get("HERMES_HOME"):
        hermes_homes.insert(0, Path(os.environ["HERMES_HOME"]))
    for hh in hermes_homes:
        candidates.append(hh / "traces")
        profiles = hh / "profiles"
        if profiles.is_dir():
            for p in sorted(profiles.iterdir()):
                if p.is_dir():
                    candidates.append(p / "traces")

    candidates += [home / ".codex" / "traces",
                   # Pi's own dir (PI_CODING_AGENT_DIR) wins: the adapter
                   # writes under ~/.pi/agent/traces, and ~/.pi/traces alone
                   # silently hid every pi record from the CLI.
                   home / ".pi" / "agent" / "traces",
                   home / ".pi" / "traces",
                   home / ".agent-trace"]

    out: list[Path] = []
    seen: set[Path] = set()
    for c in candidates:
        try:
            r = c.expanduser().resolve()
        except OSError:
            continue
        if r not in seen and r.is_dir():
            seen.add(r)
            out.append(r)
    return out


def iter_trace_files(dirs: list[Path]) -> Iterator[Path]:
    """Every *.jsonl under the given dirs, stable order.

    `ingest-faults.jsonl` is skipped by name: it lives outside every trace dir
    by design, but AGENTTRACE_PLATFORM_DIR (or a user passing a path) can put
    it inside one, and a fault log must never be read as a trace.
    """
    for d in dirs:
        try:
            for path in sorted(d.rglob("*.jsonl")):
                if path.name.startswith("ingest-faults"):
                    continue
                yield path
        except OSError:
            continue
