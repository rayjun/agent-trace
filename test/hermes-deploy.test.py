#!/usr/bin/env python3
"""The Hermes plugin is installed as a COPY — prove the installed copy works.

`install.sh hermes` copies hermes/__init__.py, hermes/plugin.yaml and
common/agenttrace_common.py into ~/.hermes/plugins/agent-trace/. The plugin
then has to find its shared module *without* the repo, which is the whole risk
of a copy install: if the shared file is not shipped, or the loader only probes
the repo layout, the plugin dies on import inside a running agent — silently,
because Hermes reports plugin failures out of band.

This test drives the REAL installer into a throwaway $HOME rather than copying
the files by hand. A hand-rolled copy would keep passing after someone added a
file to the plugin but forgot to add it to install.sh — the exact failure that
left a duration-unit fix "fixed" in git while live traces kept writing floats.

It asserts:
  1. install.sh deploys every file the plugin needs, byte-identical
  2. the deployed plugin imports with the repo out of reach
  3. it writes schema-v1 records to its own traces dir
  4. the shared module next to it is the one actually used
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOYED = [
    "hermes/__init__.py",
    "hermes/plugin.yaml",
    "common/agenttrace_common.py",
]
fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="agenttrace-deploy-"))
    hermes_home = tmp / ".hermes"
    plugin = hermes_home / "plugins" / "agent-trace"
    # install.sh refuses to install into a profile home that does not exist —
    # correct behaviour (it must not silently create another app's home), so
    # the fixture stands in for "hermes has run at least once".
    hermes_home.mkdir(parents=True, exist_ok=True)

    # Drive the real installer. stdin is /dev/null and install.sh itself
    # timeouts the `hermes plugins enable` call, so this cannot hang.
    env = dict(os.environ, HOME=str(tmp))
    env.pop("HERMES_HOME", None)          # must not point at the real profile
    env.pop("PYTHONPATH", None)
    p = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "hermes"],
        capture_output=True, text=True, env=env, cwd=str(ROOT),
        stdin=subprocess.DEVNULL, timeout=180,
    )
    check("install.sh hermes exits 0", p.returncode == 0,
          f"rc={p.returncode}\nstdout={p.stdout}\nstderr={p.stderr}")

    print("1. everything the plugin needs was deployed")
    deployed = sorted(x.name for x in plugin.iterdir()) if plugin.is_dir() else []
    for rel in DEPLOYED:
        dest = plugin / Path(rel).name
        check(f"{rel} deployed", dest.is_file(), f"missing {dest}")
        if dest.is_file():
            check(f"{rel} byte-identical to the repo",
                  dest.read_bytes() == (ROOT / rel).read_bytes(),
                  f"{dest} differs from {rel}")
    # Nothing extra that could shadow the repo version.
    check("no unexpected files in the plugin dir",
          deployed == sorted(Path(r).name for r in DEPLOYED), str(deployed))
    if not plugin.is_dir():
        # Nothing was installed: report and stop instead of crashing on the
        # import/emit steps below — a failed assertion is not an exception.
        print("\nFAILED: install produced no plugin dir", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
        return 1

    # Import the DEPLOYED copy in a subprocess where the repo is not on the
    # path and HOME points at the fake profile, so nothing can fall back to a
    # repo file by accident.
    script = (
        "import importlib.util, json, pathlib, sys\n"
        "spec = importlib.util.spec_from_file_location('deployed', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "mod.on_post_api_request(\n"
        "    api_request_id='r1', session_id='s1', model='m', provider='p',\n"
        "    api_mode='chat_completions', api_duration=2.5,\n"
        "    finish_reason='stop', usage={'prompt_tokens': 7, 'completion_tokens': 3},\n"
        "    assistant_message={'content': 'deployed ok'},\n"
        ")\n"
        "files = list(pathlib.Path(sys.argv[2], 'traces').glob('*.jsonl'))\n"
        "print(json.dumps([json.loads(l) for f in files\n"
        "                  for l in f.read_text().splitlines() if l.strip()]))\n"
    )
    print("2. the deployed plugin runs without the repo")
    env2 = dict(env, HERMES_HOME=str(hermes_home))
    p = subprocess.run(
        [sys.executable, "-c", script, str(plugin / "__init__.py"), str(hermes_home)],
        capture_output=True, text=True, env=env2, cwd="/", timeout=60,
    )
    check("imports and emits", p.returncode == 0,
          f"rc={p.returncode} stderr={p.stderr[-800:]}")

    recs = json.loads(p.stdout) if p.stdout.strip() else []
    check("one record written", len(recs) == 1, str(len(recs)))
    if recs:
        r = recs[0]
        check("record is schema v1", r.get("v") == 1, repr(r.get("v")))
        check("record tagged agent=hermes", r.get("agent") == "hermes",
              repr(r.get("agent")))
        check("duration_ms is int ms", r.get("duration_ms") == 2500,
              repr(r.get("duration_ms")))
        check("usage normalised to shared fields",
              (r.get("response") or {}).get("usage", {}).get("input_tokens") == 7,
              json.dumps((r.get("response") or {}).get("usage")))

    # The loader must have picked the file NEXT TO the plugin, not a repo one.
    print("3. the shared module came from the deploy, not the repo")
    where = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util, sys\n"
         "spec = importlib.util.spec_from_file_location('deployed', sys.argv[1])\n"
         "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
         "print(mod.common.__file__)\n",
         str(plugin / "__init__.py")],
        capture_output=True, text=True, env=env2, cwd="/", timeout=60,
    )
    shared = where.stdout.strip()
    check("loaded from the deployed copy",
          Path(shared).resolve() == (plugin / "agenttrace_common.py").resolve(),
          f"loaded {shared!r}")

    # `install.sh status` must agree with what we just measured — the checker
    # and the installer are useless if they disagree about what "installed"
    # means.
    print("4. install.sh status agrees")
    st = subprocess.run(["bash", str(ROOT / "install.sh"), "status"],
                        capture_output=True, text=True, env=env, cwd=str(ROOT),
                        stdin=subprocess.DEVNULL, timeout=60)
    check("status exits 0 when everything is deployed", st.returncode == 0,
          f"rc={st.returncode}\n{st.stdout}")
    # Match the component rows only — the `traces:` footer also contains the
    # word "hermes" and is not a status line.
    hermes_lines = [l for l in st.stdout.splitlines()
                    if l.startswith("  hermes ")]
    check("status reports one row per hermes profile",
          len(hermes_lines) >= 1, st.stdout)
    check("status reports every hermes copy as ok",
          bool(hermes_lines) and all(" ok " in l for l in hermes_lines),
          "\n".join(hermes_lines))
    check("status reports no SYNC NEEDED", "SYNC NEEDED" not in st.stdout,
          st.stdout)

    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print(f"\nFAILED: {', '.join(fails)}", file=sys.stderr)
        return 1
    print("PASS — installed hermes plugin matches the repo and runs without it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
