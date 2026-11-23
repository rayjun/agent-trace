"""Resolve the Git worktree and native session of a Herdr pane."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from core.review_git import repository_root


@dataclass(frozen=True)
class PaneReview:
    root: Path
    pane_id: str
    agent: str | None
    session_id: str | None
    terminal_id: str | None = None
    status: str | None = None


def herdr_request(*args: str) -> dict[str, Any]:
    command = [os.environ.get("HERDR_BIN_PATH") or "herdr", *args]
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=3)
        payload = json.loads(result.stdout)["result"]
        if isinstance(payload, dict):
            return payload
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Herdr could not complete {' '.join(args[:2])}") from exc
    raise ValueError("Herdr returned an invalid response")


def pane_review(pane_id: str | None = None) -> PaneReview:
    # Popup commands have an active pane, but no pane of their own.
    active = os.environ.get("HERDR_ACTIVE_PANE_ID")
    if not active and os.environ.get("HERDR_ENV") != "1":
        raise ValueError("Run this command inside Herdr")
    target = pane_id or active or os.environ.get("HERDR_PANE_ID")
    if not target:
        raise ValueError("Herdr did not provide a pane ID; supply --pane")
    payload = herdr_request("pane", "get", target).get("pane")
    if not isinstance(payload, dict) or not isinstance(payload.get("pane_id"), str):
        raise ValueError(f"Could not read Herdr pane {target}")
    return _pane_context(payload)


def _pane_context(payload: dict[str, Any]) -> PaneReview:
    target = payload["pane_id"]
    cwd = payload.get("foreground_cwd") or payload.get("cwd")
    root = repository_root(Path(cwd)) if isinstance(cwd, str) and Path(cwd).is_absolute() else None
    if root is None:
        raise ValueError(f"Herdr pane {target} is outside a Git worktree")

    agent = payload.get("agent")
    agent = agent if isinstance(agent, str) and agent else None
    terminal = payload.get("terminal_id")
    status = payload.get("agent_status")
    session_id = None
    ref = payload.get("agent_session")
    if isinstance(ref, dict) and (agent is None or ref.get("agent") == agent):
        native_agent = ref.get("agent")
        value = ref.get("value")
        if isinstance(value, str) and value.strip():
            if native_agent in ("codex", "hermes") and ref.get("kind") == "id":
                agent, session_id = native_agent, value
            elif native_agent == "pi" and ref.get("kind") == "path" and Path(value).is_absolute():
                agent, session_id = native_agent, str(Path(value).resolve())
    return PaneReview(root, target, agent, session_id, terminal, status)


def task_source(root: Path, task: dict[str, Any]) -> PaneReview | None:
    if not task["pane_id"] or not task["terminal_id"] or not task["agent"]:
        return None
    try:
        pane = pane_review(task["pane_id"])
    except ValueError:
        return None
    if pane.root == root and pane.terminal_id == task["terminal_id"] and pane.agent == task["agent"]:
        if not task["session_id"] or pane.session_id == task["session_id"]:
            return pane
    return None


def publish_tasks(db: Path) -> None:
    """Refresh visible worktree statuses using Herdr's scheduled status command."""
    from core.repo_review import repository_review
    from core.review_git import checkout_identity
    from core.review_tasks import checkout_key, next_task_action, worktree_task
    from core.storage.sqlite_store import TraceStore

    if not db.is_file() or not (os.environ.get("HERDR_ACTIVE_PANE_ID") or os.environ.get("HERDR_ENV") == "1"):
        return
    sequence = str(time.monotonic_ns())
    store = TraceStore(db)
    try:
        tracked = {Path(row["root"]) for row in store.query("SELECT DISTINCT root FROM worktree_tasks")}
        if not tracked:
            return
        snapshot = herdr_request("api", "snapshot")["snapshot"]
        workspace_roots: dict[str, set[Path]] = {}
        roots_by_cwd: dict[Path, Path | None] = {}
        for pane in snapshot["panes"]:
            cwd = Path(pane.get("foreground_cwd") or pane.get("cwd") or ".")
            if not cwd.is_absolute() or not any(cwd.is_relative_to(root) for root in tracked):
                continue
            if cwd not in roots_by_cwd:
                roots_by_cwd[cwd] = repository_root(cwd)
            root = roots_by_cwd[cwd]
            if root is not None and root in tracked:
                workspace_roots.setdefault(pane["workspace_id"], set()).add(root)
        summaries: dict[Path, tuple[str, str] | None] = {}
        for workspace, roots in workspace_roots.items():
            if len(roots) != 1:
                continue
            root = next(iter(roots))
            if root not in summaries:
                summaries[root] = None
                try:
                    task = worktree_task(store, root, checkout_key(*checkout_identity(root)))
                    if task:
                        review = repository_review(store, root, auto_select_session=False,
                                                   allow_missing_session=True, include_task=True)
                        if review["task"] == task:
                            source = task_source(root, task)
                            _, step = next_task_action(review, source.status if source else None)
                            summaries[root] = (str(task["goal"])[:36], step)
                except (OSError, RuntimeError, ValueError):
                    continue
            summary = summaries[root]
            args = ["workspace", "report-metadata", workspace, "--source", "agenttrace-task",
                    "--seq", sequence, "--ttl-ms", "35000"]
            if summary:
                args += ["--token", f"review_goal={summary[0]}", "--token", f"review_step={summary[1]}"]
            else:
                args += ["--clear-token", "review_goal", "--clear-token", "review_step"]
            herdr_request(*args)
    finally:
        store.close()
