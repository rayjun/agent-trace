#!/usr/bin/env python3
"""Install the agent-trace Codex hook into ~/.codex/hooks.json (or config.toml).

Backs up any existing hooks.json first, merges rather than overwrites, and
verifies the result by re-reading the file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
SCRIPT = REPO / "codex_import.py"

# UserPromptSubmit + Stop are the two events that always fire for a turn and
# together guarantee a full import. The tool events add granularity when Codex
# runs them; a session that dies mid-turn is still covered by Stop.
#
# This list is deliberately limited to events verified present in the Codex
# binary. SessionEnd appears in the published hooks documentation but is absent
# from codex-cli 0.139.0, so wiring it produced a dead entry that never fired;
# `codex_supported_events` below checks the installed binary and reports it.
EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionStart"]


def codex_bin() -> str | None:
    return shutil.which("codex")


def codex_supported_events(events: list[str]) -> dict[str, bool]:
    """Probe the installed Codex binary for each event name.

    The hooks documentation lists more events than any single Codex build
    implements, so wiring a name the binary has never heard of yields a
    hooks.json entry that silently never runs. A name absent from the binary's
    strings is reported as unsupported instead of being installed.
    """
    exe = codex_bin()
    if not exe:
        return {e: True for e in events}   # cannot probe; do not block the install

    # codex is a node shim; the real binary is the vendored native executable.
    candidates = [exe]
    real = Path(exe).resolve()
    if real.is_file():
        vendored = list(real.parent.glob("../node_modules/@openai/*/vendor/*/bin/codex"))
        candidates += [str(p.resolve()) for p in vendored]

    blob = b""
    for c in candidates:
        try:
            blob += Path(c).read_bytes()
        except OSError:
            continue
    if not blob:
        return {e: True for e in events}

    out: dict[str, bool] = {}
    for e in events:
        out[e] = e.encode("ascii") in blob
    return out


def enable_feature() -> str:
    """Turn hooks on through Codex's own CLI, falling back to config.toml."""
    exe = codex_bin()
    if exe:
        try:
            p = subprocess.run([exe, "features", "enable", "hooks"],
                               capture_output=True, text=True, timeout=30)
            if p.returncode == 0:
                return f"enabled feature `hooks` via `codex features enable hooks` ({p.stdout.strip()})"
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def main() -> int:
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    hooks_json = codex_home / "hooks.json"
    config_toml = codex_home / "config.toml"

    supported = codex_supported_events(EVENTS)
    unsupported = [e for e, ok in supported.items() if not ok]
    if unsupported:
        print(f"! not found in the installed codex binary, skipping: {', '.join(unsupported)}")
    events = [e for e in EVENTS if e not in unsupported]

    cmd = [sys.executable, str(SCRIPT)]
    entry = {"type": "command", "command": " ".join(cmd), "timeout": 20}

    data = {"hooks": {}}
    if hooks_json.exists():
        try:
            data = json.loads(hooks_json.read_text(encoding="utf-8")) or data
        except json.JSONDecodeError:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(hooks_json, hooks_json.with_suffix(f".json.bak-{stamp}"))
            print(f"! existing hooks.json was invalid JSON; backed it up to {stamp}")
        data.setdefault("hooks", {})

    for ev in events:
        data["hooks"].setdefault(ev, [])
        # replace a previous agent-trace entry rather than stacking duplicates
        data["hooks"][ev] = [h for h in data["hooks"][ev]
                             if "codex_import.py" not in json.dumps(h)]
        data["hooks"][ev].append(dict(entry))

    codex_home.mkdir(parents=True, exist_ok=True)
    hooks_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {hooks_json}")

    # Codex gates hooks behind a feature flag. It is `hooks` (stable, on by
    # default since 0.124), not `codex_hooks`; ask Codex itself before editing
    # the file by hand.
    note = enable_feature()
    if note:
        print(note)
    elif config_toml.exists():
        text = config_toml.read_text(encoding="utf-8")
        if "[features]" not in text:
            with config_toml.open("a", encoding="utf-8") as fh:
                fh.write("\n[features]\nhooks = true\n")
            print(f"appended [features] hooks = true to {config_toml}")
        elif "hooks" not in text.split("[features]", 1)[1]:
            with config_toml.open("a", encoding="utf-8") as fh:
                fh.write("hooks = true\n")
            print(f"appended hooks = true under existing [features] in {config_toml}")
        else:
            print(f"{config_toml} already mentions hooks; left it alone")
    else:
        config_toml.write_text("[features]\nhooks = true\n", encoding="utf-8")
        print(f"created {config_toml}")

    # verify
    check = json.loads(hooks_json.read_text(encoding="utf-8"))
    for ev in events:
        assert len(check["hooks"].get(ev, [])) >= 1, ev
    print(f"verified: {len(events)} events wired ({', '.join(events)})")
    print("\nNext: run `codex` once and approve the agent-trace hook when prompted")
    print("(or set bypass_hook_trust = true in ~/.codex/config.toml).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
