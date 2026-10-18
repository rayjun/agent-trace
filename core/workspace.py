"""The workspace a trace ran against — repository, branch, commit, diff summary.

Why this exists (proposal §8.1): a coding agent's trace is only half readable
without the code it was looking at. "The tool failed, then the agent edited a
file, then the tests passed" says nothing until you know which repository, which
branch, which commit, and what changed. The trace and the working tree are two
halves of one story.

Three rules shape this module:

* **Read-only.** Every git call is `git --no-optional-locks <read-only verb>`.
  A tracer that can write to the repository it observes, or leave index locks
  behind, is a tracer that will be uninstalled the first time it corrupts
  someone's staging area.
* **Bounded.** Every call has a timeout, every list a cap, and the diff is
  behind an explicit opt-in. A trace of a monorepo must not fsync a 40 MB diff
  into a JSONL file on every session.
* **Honest about WHEN.** A snapshot taken at capture time and one taken at
  index time are different claims: the first says "this is what the agent saw",
  the second says "this is what the directory looks like now". `at` records
  which one it is, because a reader comparing a trace's diff against a
  months-old session would otherwise draw the wrong conclusion.

Privacy: `diff` and `changed_files` can contain source and credentials. They are
OFF by default (`AGENTTRACE_GIT=full` turns them on), and a snapshot always
reports what it withheld.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The snapshot itself lives in `common/` next to the v1 rules: a deployed Hermes
# plugin is a COPY of that file and can import nothing else, so an adapter-side
# implementation there is the only way every adapter shares one.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
import agenttrace_common as _common  # noqa: E402

GIT_ENV = "AGENTTRACE_GIT"          # metadata (default) | full | off
CAP_ENV = "AGENTTRACE_GIT_MAX"      # bytes of diff to keep
#: Kept for callers that pass it through; the shared implementation owns the
#: real timeout so an adapter and the platform cannot drift.
DEFAULT_TIMEOUT = 5.0

def git_mode() -> str:
    """`metadata` (default), `full`, or `off`."""
    return _common.git_mode()


@dataclass
class GitSnapshot:
    """What the repository looked like. Every field is best-effort."""

    root: str | None = None
    branch: str | None = None
    head: str | None = None           # short sha
    subject: str | None = None        # the commit's subject line
    detached: bool = False
    dirty: bool = False
    changed_files: list[str] = field(default_factory=list)
    changed_count: int = 0
    diff: str | None = None
    diff_truncated: bool = False
    diff_withheld: bool = False
    #: When the snapshot was taken: "capture" (the adapter saw it live) or
    #: "index" (nobody recorded one, so it was read later — the repository may
    #: have moved on).
    at: str = "capture"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"at": self.at}
        for key in ("root", "branch", "head", "subject"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.detached:
            out["detached"] = True
        if self.dirty:
            out["dirty"] = True
        if self.changed_count:
            out["changed_count"] = self.changed_count
        if self.changed_files:
            out["changed_files"] = self.changed_files
        if self.diff is not None:
            out["diff"] = self.diff
        if self.diff_truncated:
            out["diff_truncated"] = True
        if self.diff_withheld:
            out["diff_withheld"] = True
        if self.error:
            out["error"] = self.error
        return out

    @property
    def is_repo(self) -> bool:
        return self.root is not None


def _cap() -> int:
    try:
        return max(0, int(os.environ.get(CAP_ENV) or DEFAULT_DIFF_CAP))
    except ValueError:
        return DEFAULT_DIFF_CAP


def snapshot(cwd: str | os.PathLike[str] | None, *,
             at: str = "capture",
             include_diff: bool | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> GitSnapshot | None:
    """Read the git state of `cwd`, or None when there is nothing to say.

    None means "not a repository, or git is unavailable" — different from an
    object with an `error`, which means "git was asked and refused". The
    distinction is what lets a caller avoid writing an empty block onto every
    trace from a directory that was never under version control.
    """
    if include_diff is not None:
        # An explicit flag wins for this call; otherwise the env decides inside
        # the shared implementation.
        previous = os.environ.get("AGENTTRACE_GIT")
        os.environ["AGENTTRACE_GIT"] = "full" if include_diff else "metadata"
        try:
            data = _common.workspace_snapshot(cwd, at=at)
        finally:
            if previous is None:
                os.environ.pop("AGENTTRACE_GIT", None)
            else:
                os.environ["AGENTTRACE_GIT"] = previous
    else:
        data = _common.workspace_snapshot(cwd, at=at)
    if data is None:
        return None
    return GitSnapshot(
        root=data.get("root"), branch=data.get("branch"), head=data.get("head"),
        subject=data.get("subject"), detached=bool(data.get("detached")),
        dirty=bool(data.get("dirty")),
        changed_files=list(data.get("changed_files") or []),
        changed_count=int(data.get("changed_count") or 0),
        diff=data.get("diff"), diff_truncated=bool(data.get("diff_truncated")),
        diff_withheld=bool(data.get("diff_withheld")), at=data.get("at", at),
        error=data.get("error"))


#: Directory names that never belong to the agent's own work and would drown the
#: signal in a monorepo.
def file_of_tool_call(args: Any) -> str | None:
    """The file a tool call touched, when the arguments name one.

    Used to link a tool span to a path so the dashboard can answer "which file
    was being edited when this failed" without guessing from a free-text command.
    """
    keys = ("file_path", "path", "filename", "file", "notebook_path", "target")
    if isinstance(args, dict):
        for key in keys:
            value = args.get(key)
            if isinstance(value, str) and value:
                return value[:400]
        # nested under `input`/`args`, which several tool wrappers use
        for key in ("input", "args", "arguments", "parameters"):
            nested = file_of_tool_call(args.get(key))
            if nested:
                return nested
    return None
