#!/usr/bin/env python3
"""The workspace snapshot — the half of a coding trace that is the code.

A tracer that reads a repository has to be harmless in it, cheap on the agent's
path, and explicit about WHEN it looked: a snapshot taken at capture time says
"this is what the agent saw", one taken at index time says "this is the
directory now", and confusing the two makes a months-old trace look like it ran
against today's branch.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.workspace import file_of_tool_call, git_mode, snapshot  # noqa: E402

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


tmp = Path(tempfile.mkdtemp(prefix="ws-"))
repo = tmp / "repo"
repo.mkdir()
git(repo, "init", "-q", "-b", "main")
git(repo, "config", "user.email", "t@example.com")
git(repo, "config", "user.name", "t")
(repo / "a.py").write_text("print(1)\n", encoding="utf-8")
git(repo, "add", "-A")
git(repo, "commit", "-qm", "the first commit")

# ---- a clean repository ----------------------------------------------------
snap = snapshot(repo)
check("a repository is detected", snap is not None and snap.is_repo, str(snap))
check("the branch is read", snap.branch == "main", str(snap.branch))
check("HEAD is read and short", snap.head and len(snap.head) == 7, str(snap.head))
check("the commit subject is read",
      snap.subject == "the first commit", str(snap.subject))
check("a clean tree is not dirty", snap.dirty is False and snap.changed_count == 0)
check("the diff is WITHHELD by default, and says so",
      snap.diff is None and snap.diff_withheld is True,
      f"diff={snap.diff!r} withheld={snap.diff_withheld}")

# ---- a dirty tree, including an untracked file -----------------------------
(repo / "a.py").write_text("print(2)\n", encoding="utf-8")
(repo / "new.py").write_text("x = 1\n", encoding="utf-8")
snap = snapshot(repo)
check("a modified file makes the tree dirty", snap.dirty is True, str(snap.dirty))
check("changed files are listed", set(snap.changed_files) >= {"a.py", "new.py"},
      str(snap.changed_files))
# `??` matters: an agent's new file is exactly the change a reader wants, and a
# diff of tracked files alone would never show it.
full = snapshot(repo, include_diff=True)
check("the diff includes the untracked file (marked ??)",
      "?? new.py" in (full.diff or ""), (full.diff or "")[:200])
check("the diff of a tracked change is present", "-print(1)" in (full.diff or "")
      and "+print(2)" in (full.diff or ""), (full.diff or "")[:200])

# ---- caps ------------------------------------------------------------------
os.environ["AGENTTRACE_GIT_MAX"] = "40"
capped = snapshot(repo, include_diff=True)
check("the diff is capped and marked truncated",
      capped.diff_truncated and len(capped.diff or "") <= 40,
      f"len={len(capped.diff or '')} truncated={capped.diff_truncated}")
del os.environ["AGENTTRACE_GIT_MAX"]

# ---- modes -----------------------------------------------------------------
os.environ["AGENTTRACE_GIT"] = "off"
check("AGENTTRACE_GIT=off disables it entirely", snapshot(repo) is None)
os.environ["AGENTTRACE_GIT"] = "full"
mode_full = snapshot(repo)
check("AGENTTRACE_GIT=full includes the diff without an explicit flag",
      mode_full.diff is not None and not mode_full.diff_withheld)
check("git_mode() reports what it decided", git_mode() == "full")
del os.environ["AGENTTRACE_GIT"]
check("...and the default is metadata", git_mode() == "metadata")

# ---- non-repositories and missing paths -----------------------------------
outside = tmp / "plain"
outside.mkdir()
check("a directory that is not a repository yields None", snapshot(outside) is None)
check("a missing directory yields None", snapshot(tmp / "nope") is None)
check("no cwd yields None", snapshot(None) is None)

# ---- read-only: the index lock and the worktree are untouched -------------
before_status = subprocess.run(["git", "--no-optional-locks", "status", "--porcelain"],
                               cwd=repo, capture_output=True, text=True).stdout
snapshot(repo, include_diff=True)
after_status = subprocess.run(["git", "--no-optional-locks", "status", "--porcelain"],
                              cwd=repo, capture_output=True, text=True).stdout
check("snapshotting does not change the repository state",
      before_status == after_status, f"{before_status!r} vs {after_status!r}")
check("...and leaves no index lock behind", not (repo / ".git" / "index.lock").exists())

# ---- the `at` marker -------------------------------------------------------
check("a capture-time snapshot says capture", snapshot(repo, at="capture").at == "capture")
check("an index-time snapshot says index",
      snapshot(repo, at="index").at == "index")
check("the marker survives into the dict",
      snapshot(repo, at="index").to_dict()["at"] == "index")
check("a non-repo dict has no git block", snapshot(outside) is None)

# ---- tool-call -> file -----------------------------------------------------
check("the file of a tool call is found (direct key)",
      file_of_tool_call({"file_path": "a/b.py"}) == "a/b.py")
check("...and when nested", file_of_tool_call({"args": {"path": "x/y.txt"}}) == "x/y.txt")
check("...and when it is a notebook path",
      file_of_tool_call({"notebook_path": "n.ipynb"}) == "n.ipynb")
check("a command with no file yields None",
      file_of_tool_call({"command": "pytest -x"}) is None)
check("a non-dict yields None", file_of_tool_call("read a/b.py") is None)

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("workspace OK")
