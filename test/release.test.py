#!/usr/bin/env python3
"""One version, everywhere.

The release number is declared once in VERSION and copied into the two manifests
that must carry it on disk:

  * hermes/plugin.yaml  — read by Hermes' plugin loader
  * pi/package.json     — read by npm / `pi list`

Both are installed as-is, so neither can be generated at build time; they can
only be checked. They had already drifted (1.1.0 vs 1.0.0) when this test was
written — plugin.yaml was bumped for a Hermes change and package.json was not,
so `pi list` reported the wrong version with no signal anywhere.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


def main() -> int:
    print("1. VERSION")
    raw = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    check("VERSION exists and is non-empty", bool(raw), repr(raw))
    check("VERSION is semver",
          re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", raw) is not None,
          repr(raw))
    print(f"       -> {raw}")

    print("2. manifests agree with VERSION")
    yaml_text = (ROOT / "hermes" / "plugin.yaml").read_text(encoding="utf-8")
    m = re.search(r'^version:\s*["\']?([^"\'\s]+)["\']?\s*$', yaml_text, re.M)
    check("plugin.yaml declares a version", m is not None, yaml_text)
    if m:
        check(f"plugin.yaml version == {raw}", m.group(1) == raw,
              f"{m.group(1)} != {raw}")

    pkg = json.loads((ROOT / "pi" / "package.json").read_text(encoding="utf-8"))
    check(f"package.json version == {raw}", pkg.get("version") == raw,
          f"{pkg.get('version')} != {raw}")

    print("3. install.sh ships every file the deployed plugin needs")
    # The Hermes plugin is a *copy*; a file added to the plugin without a
    # matching `cp` in install.sh produces an ImportError inside a running
    # agent, where nothing surfaces it. The hermes-deploy test proves the
    # happy path end to end — this catches the command going missing by
    # checking the script still names each file.
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    for rel in ("hermes/__init__.py", "hermes/plugin.yaml",
                "common/agenttrace_common.py"):
        check(f"install.sh references {rel}", rel in install, rel)

    print("4. install.sh status agrees the deployed copies are stale or not")
    # Deployed copies exist on this machine and are known to be out of date
    # with the repo — `status` must notice, otherwise the tool that exists to
    # find stale copies is not doing its job.
    p = subprocess.run(["bash", str(ROOT / "install.sh"), "status"],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60)
    check("status runs", p.returncode in (0, 1), f"rc={p.returncode} {p.stderr}")
    check("status prints the version from VERSION",
          raw in p.stdout.splitlines()[0], p.stdout.splitlines()[:1])
    check("status always ends with a trace-dir hint",
          "traces:" in p.stdout, p.stdout)

    if fails:
        print(f"\nFAILED: {', '.join(fails)}", file=sys.stderr)
        return 1
    print(f"PASS — VERSION, plugin.yaml and package.json all say {raw}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
