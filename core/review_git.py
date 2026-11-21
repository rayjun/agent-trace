"""Read the repository state that a local check can be attached to."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import new as new_hash, sha256
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryFile
from typing import Any

MAX_FILES = 30_000
MAX_BYTES = 256 * 1024 * 1024
DIFF_LIMIT = 64_000


class GitReviewError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, allowed: tuple[int, ...] = (0,)) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "--literal-pathspecs", "-C", str(cwd), *args],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitReviewError("Git could not read the repository") from exc
    if proc.returncode not in allowed:
        raise GitReviewError(proc.stderr.decode(errors="replace").strip() or "Git could not read the repository")
    return proc.stdout if proc.returncode == 0 else b""


def repository_root(cwd: Path) -> Path | None:
    try:
        root = _git(cwd, "rev-parse", "--show-toplevel").removesuffix(b"\n")
    except GitReviewError:
        return None
    return Path(os.fsdecode(root)).resolve()


def checkout_identity(root: Path) -> tuple[str | None, str | None]:
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1))
    if branch:
        return branch.decode(errors="replace").strip(), None
    head = _git(root, "rev-parse", "--verify", "HEAD", allowed=(0, 128)).strip()
    return None, head.decode() if head else None


def _entries(root: Path) -> tuple[bytes, bytes, bytes]:
    head = _git(root, "rev-parse", "--verify", "HEAD", allowed=(0, 128)).strip()
    index = _git(root, "ls-files", "--stage", "-z")
    others = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return head, index, others


def _file_version(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_ino, info.st_dev, info.st_mode, info.st_size, info.st_ctime_ns, info.st_mtime_ns)


def _add(digest: Any, data: bytes) -> None:
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def _fingerprint(root: Path, entries: tuple[bytes, bytes, bytes]) -> tuple[str, bool]:
    head, index, others = entries
    digest = sha256()
    for part in (b"agenttrace-worktree-v1", head, index, others):
        _add(digest, part)
    untracked = set(others.split(b"\0")) - {b""}
    stage_entries = {}
    for item in index.split(b"\0"):
        if not item:
            continue
        meta, raw_path = item.split(b"\t", 1)
        mode, oid, stage = meta.split()
        if mode == b"160000":
            raise GitReviewError("Submodule contents cannot be verified as part of this snapshot")
        if stage != b"0":
            raise GitReviewError("Resolve Git index conflicts before verifying this snapshot")
        stage_entries[raw_path] = (mode, oid)
    paths = untracked | stage_entries.keys()
    if len(paths) > MAX_FILES:
        raise GitReviewError(f"Snapshot exceeds the {MAX_FILES:,} file limit")

    remaining = MAX_BYTES
    observed: dict[Path, tuple[int, ...] | None] = {}
    index_matches = not untracked
    for raw in sorted(paths):
        relpath = Path(os.fsdecode(raw))
        path = root / relpath
        _add(digest, raw)
        if relpath.is_absolute() or ".." in relpath.parts or not path.parent.resolve().is_relative_to(root):
            raise GitReviewError("Git reported a file outside the worktree")
        try:
            before = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            _add(digest, b"absent")
            observed[path] = None
            index_matches = False
            continue
        observed[path] = _file_version(before)
        stage_entry = stage_entries.get(raw)
        # Git status may suppress content changes via assume-unchanged or
        # stat caching. Compare raw worktree bytes with the actual index blob.
        blob = (new_hash("sha1" if len(stage_entry[1]) == 40 else "sha256", usedforsecurity=False)
                if stage_entry is not None else None)
        if stat.S_ISLNK(before.st_mode):
            mode = b"120000"
            link = os.fsencode(os.readlink(path))
            _add(digest, b"link")
            _add(digest, link)
            if blob is not None:
                blob.update(f"blob {len(link)}\0".encode())
                blob.update(link)
        elif stat.S_ISREG(before.st_mode):
            mode = b"100755" if before.st_mode & stat.S_IXUSR else b"100644"
            if before.st_size > remaining:
                raise GitReviewError(f"Snapshot exceeds the {MAX_BYTES // (1024 * 1024)} MiB content limit")
            remaining -= before.st_size
            contents = sha256()
            if blob is not None:
                blob.update(f"blob {before.st_size}\0".encode())
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                if _file_version(os.fstat(stream.fileno())) != _file_version(before):
                    raise GitReviewError("A worktree file changed while the snapshot was being read")
                left = before.st_size
                while left:
                    chunk = stream.read(min(left, 1024 * 1024))
                    if not chunk:
                        raise GitReviewError("A worktree file changed while the snapshot was being read")
                    contents.update(chunk)
                    if blob is not None:
                        blob.update(chunk)
                    left -= len(chunk)
            _add(digest, str(stat.S_IMODE(before.st_mode)).encode())
            _add(digest, contents.digest())
        else:
            raise GitReviewError("The worktree contains a file with an unsupported type")
        if blob is not None and stage_entry != (mode, blob.hexdigest().encode()):
            index_matches = False
    if entries != _entries(root):
        raise GitReviewError("Git HEAD, index or untracked paths changed during the snapshot")
    for path, expected in observed.items():
        try:
            actual = _file_version(path.lstat())
        except (FileNotFoundError, NotADirectoryError):
            actual = None
        if actual != expected:
            raise GitReviewError("A worktree file changed while the snapshot was being read")
    return digest.hexdigest(), index_matches


@dataclass(frozen=True)
class Worktree:
    root: Path
    head: str | None
    branch: str | None
    files: list[dict[str, str]]
    fingerprint: str | None
    unavailable: str | None
    staged_matches_worktree: bool


def worktree_status(root: Path) -> list[dict[str, str]]:
    status = _git(root, "status", "--porcelain=v1", "-z", "-uall", "--no-renames", "--ignore-submodules=none")
    return [{"path": os.fsdecode(item[3:]), "index": chr(item[0]), "worktree": chr(item[1])}
            for item in status.split(b"\0") if item]


def worktree(root: Path) -> Worktree:
    entries = _entries(root)
    files = worktree_status(root)
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1))
    try:
        fingerprint, index_matches = _fingerprint(root, entries)
        if files != worktree_status(root):
            raise GitReviewError("Git changes shifted during the snapshot")
        unavailable = None
    except (OSError, ValueError, GitReviewError) as exc:
        fingerprint = None
        index_matches = False
        unavailable = str(exc)
    return Worktree(root, entries[0].decode() or None, branch.decode(errors="replace").strip() or None,
                    files, fingerprint, unavailable, index_matches)


def file_diff(root: Path, path: str, *, untracked: bool = False) -> dict[str, str]:
    relative = Path(path)
    target = root / relative
    if relative.is_absolute() or ".." in relative.parts or not target.parent.resolve().is_relative_to(root):
        raise GitReviewError("Choose a file from the current worktree")
    if untracked and (target.is_symlink() or not target.is_file()):
        return {"untracked": "Preview unavailable for this file type."}

    def diff(*args: str) -> str:
        # Keep the retained diff small even when Git produces a large patch.
        try:
            with TemporaryFile() as output:
                proc = subprocess.run(
                    ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "--literal-pathspecs",
                     "-C", str(root), "diff", "--no-color", "--no-ext-diff", "--no-textconv",
                     *args],
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, timeout=8,
                )
                if proc.returncode not in ((0, 1) if untracked else (0,)):
                    raise GitReviewError("Git could not show the file diff")
                output.seek(0)
                data = output.read(DIFF_LIMIT + 1)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitReviewError("Git could not show the file diff") from exc
        text = data[:DIFF_LIMIT].decode(errors="replace")
        return text + "\n[Diff preview truncated]" if len(data) > DIFF_LIMIT else text

    if untracked:
        return {"untracked": diff("--no-index", "--", os.devnull, path)}
    return {"staged": diff("--cached", "--", path), "unstaged": diff("--", path)}
