"""One resumable review task per worktree and checkout."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.schema.model import now_iso
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


@dataclass(frozen=True)
class TaskItem:
    root: Path
    checkout: str
    goal: str
    checkpoint: str
    generation: int

    @property
    def checkout_label(self) -> str:
        branch, _, name = self.checkout.partition(":")
        return name if branch == "branch" else f"detached at {name[:12] or 'unborn'}"


def active_tasks(store: TraceStore) -> list[TaskItem]:
    rows = store.query(
        """WITH tasks AS (
               SELECT t.root, t.checkout, t.goal,
                      (SELECT MAX(id) FROM worktree_task_events
                       WHERE root=t.root AND checkout=t.checkout) AS recent,
                      COALESCE((SELECT MAX(id) FROM worktree_task_events
                       WHERE root=t.root AND checkout=t.checkout AND kind='archived'), 0) AS generation
               FROM worktree_tasks t
           )
           SELECT t.root, t.checkout, t.goal, t.generation,
                  COALESCE((SELECT detail FROM worktree_task_events
                   WHERE root=t.root AND checkout=t.checkout AND kind='checkpoint' AND id>t.generation
                   ORDER BY id DESC LIMIT 1), '') AS checkpoint
           FROM tasks t ORDER BY t.recent DESC, t.root, t.checkout""")
    return [TaskItem(Path(row["root"]), row["checkout"], row["goal"], row["checkpoint"], row["generation"])
            for row in rows]


def task_review_state(review: dict[str, Any]) -> str:
    state: str = review["state"]
    if state == "unverified":
        states = {check["state"] for check in review["checks"]}
        return next((pending for pending in ("running", "stale") if pending in states), state)
    return state


def checkout_key(branch: str | None, head: str | None) -> str:
    return f"branch:{branch}" if branch is not None else f"detached:{head or ''}"


def worktree_task(store: TraceStore, root: Path, checkout: str) -> dict[str, Any] | None:
    row = store.one(
        """SELECT t.*, COALESCE((SELECT MAX(id) FROM worktree_task_events
                  WHERE root=t.root AND checkout=t.checkout), 0) AS revision,
                  COALESCE((SELECT MAX(id) FROM worktree_task_events
                  WHERE root=t.root AND checkout=t.checkout AND kind='archived'), 0) AS generation
           FROM worktree_tasks t WHERE root=? AND checkout=?""", (str(root), checkout))
    return dict(row) if row else None


def _archive_boundary(store: TraceStore, scope: tuple[str, str]) -> int:
    boundary = store.one(
        "SELECT COALESCE(MAX(id), 0) FROM worktree_task_events WHERE root=? AND checkout=? AND kind='archived'",
        scope)
    return boundary[0] if boundary else 0


def _checkpoint(store: TraceStore, scope: tuple[str, str], after: int | None = None) -> str:
    if after is None:
        after = _archive_boundary(store, scope)
    note = store.one(
        "SELECT detail FROM worktree_task_events WHERE root=? AND checkout=? AND kind='checkpoint' AND id>?"
        " ORDER BY id DESC LIMIT 1", (*scope, after))
    return note["detail"] if note else ""


def task_activity(store: TraceStore, root: Path, checkout: str) -> dict[str, Any]:
    scope = (str(root), checkout)
    after = _archive_boundary(store, scope)
    repairs = store.query(
        "SELECT DISTINCT check_id FROM worktree_task_events "
        "WHERE root=? AND checkout=? AND kind='repair_sent' AND id>?", (*scope, after))
    return {
        "events": [dict(row) for row in store.query(
            "SELECT * FROM worktree_task_events WHERE root=? AND checkout=? ORDER BY id DESC LIMIT 8", scope)],
        "checkpoint": _checkpoint(store, scope, after),
        "sent_checks": [row["check_id"] for row in repairs],
    }


def next_task_action(review: dict[str, Any], agent_status: str | None) -> tuple[str, str]:
    if not review["task"]:
        return "edit_task", "Start a task"
    if agent_status == "blocked":
        return "task_agent", "Answer agent"
    if agent_status in ("idle", "done", "working") and any(
        check["state"] == "failed" and check["id"] in review["activity"]["sent_checks"] for check in review["checks"]
    ):
        return "task_agent", "Follow up on repair"
    state = task_review_state(review)
    if state == "passed":
        return ("files", "Review verified changes") if review["files"] else ("archive_task", "Archive task")
    if review["checks"] and state != "unverified":
        return "checks", NEXT_STEP[state]
    return "files", "Review changes"


def _event(store: TraceStore, task: dict[str, Any], kind: str, detail: str, check_id: int | None = None) -> int:
    result = store._conn().execute(
        """INSERT INTO worktree_task_events (root,checkout,kind,goal,detail,check_id,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (task["root"], task["checkout"], kind, task["goal"], detail, check_id, now_iso()))
    assert result.lastrowid is not None
    return result.lastrowid


def save_task(store: TraceStore, root: Path, checkout: str, goal: str, *,
              pane_id: str | None, terminal_id: str | None, agent: str | None, session_id: str | None) -> None:
    goal = " ".join(goal.split())
    if not goal or len(goal) > 240:
        raise ValueError("Enter a task goal of 1–240 characters")
    with store.transaction():
        previous = worktree_task(store, root, checkout)
        store._conn().execute(
            """INSERT INTO worktree_tasks (root,checkout,goal,pane_id,terminal_id,agent,session_id)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(root,checkout) DO UPDATE SET
               goal=excluded.goal,pane_id=excluded.pane_id,terminal_id=excluded.terminal_id,
               agent=excluded.agent,session_id=excluded.session_id""",
            (str(root), checkout, goal, pane_id, terminal_id, agent, session_id),
        )
        current = worktree_task(store, root, checkout)
        assert current is not None
        if current != previous:
            detail = f"Source: {agent} · {pane_id}" if agent and pane_id else ""
            _event(store, current, "updated" if previous else "started", detail)


def record_task_event(store: TraceStore, task: dict[str, Any], kind: str, detail: str,
                      check_id: int | None = None) -> int:
    with store.transaction():
        root, checkout = Path(task["root"]), task["checkout"]
        if worktree_task(store, root, checkout) != task:
            raise ValueError("The task changed. Reopen it and try again.")
        event = _event(store, task, kind, detail, check_id)
        if kind == "archived":
            store._conn().execute("DELETE FROM worktree_tasks WHERE root=? AND checkout=?", (str(root), checkout))
        return event


def resolve_repair_event(store: TraceStore, event: int, *, sent: bool) -> None:
    with store.transaction():
        store._conn().execute(
            "UPDATE worktree_task_events SET kind=? WHERE id=? AND kind='repair_pending'",
            ("repair_sent" if sent else "repair_unknown", event))
