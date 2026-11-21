"""One resumable review task per worktree and checkout."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.storage.sqlite_store import TraceStore

NEXT_STEP = {
    "missing": "Run checks",
    "failed": "Investigate failure",
    "stale": "Rerun checks",
    "running": "Checking",
    "passed": "Checks passed",
    "unknown": "Verify again",
    "unverified": "Review changes",
}


def task_review_state(review: dict[str, Any]) -> str:
    state: str = review["state"]
    if state == "unverified":
        states = {check["state"] for check in review["checks"]}
        return next((pending for pending in ("running", "stale") if pending in states), state)
    return state


def checkout_key(branch: str | None, head: str | None) -> str:
    return f"branch:{branch}" if branch is not None else f"detached:{head or ''}"


def worktree_task(store: TraceStore, root: Path, checkout: str) -> dict[str, Any] | None:
    row = store.one("SELECT * FROM worktree_tasks WHERE root=? AND checkout=?", (str(root), checkout))
    return dict(row) if row else None


def save_task(store: TraceStore, root: Path, checkout: str, goal: str, *,
              pane_id: str | None, terminal_id: str | None, agent: str | None, session_id: str | None) -> None:
    goal = " ".join(goal.split())
    if not goal or len(goal) > 240:
        raise ValueError("Enter a task goal of 1–240 characters")
    with store.transaction():
        store._conn().execute(
            """INSERT INTO worktree_tasks (root,checkout,goal,pane_id,terminal_id,agent,session_id)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(root,checkout) DO UPDATE SET
               goal=excluded.goal,pane_id=excluded.pane_id,terminal_id=excluded.terminal_id,
               agent=excluded.agent,session_id=excluded.session_id""",
            (str(root), checkout, goal, pane_id, terminal_id, agent, session_id),
        )
