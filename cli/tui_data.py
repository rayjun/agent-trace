"""Local data source for the Textual dashboard; no UI dependency.

Each query closes its thread-local SQLite connection. The indexer owns another
connection/thread, so a long scan never blocks terminal input or WAL reads.
"""
from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any, Callable, TypeVar

import agenttrace_common as common
from core import query
from core.storage.indexer import Indexer
from core.storage.sqlite_store import TraceStore
from tui_sources import CodexSync

T = TypeVar("T")
SessionKey = tuple[str, str]
PAGE_SIZE = 30


class LocalBackend:
    def __init__(self, db: Path, dirs: list[Path], *, index: bool = True,
                 interval: float = 2.5, capture: bool = False) -> None:
        self.db = db
        self.dirs = dirs
        self.index = index
        self.interval = interval
        self.store: TraceStore | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.scanning = False
        self.scans = 0
        self.errors: list[str] = []
        self.updated_at: float | None = None
        self.capture = CodexSync(dirs) if capture and index else None

    def start(self) -> None:
        # Opening an old index may migrate projections; callers run this off UI.
        store = TraceStore(self.db)
        store.close()
        self.store = store
        if self.index and not self._stop.is_set():
            self._thread = threading.Thread(target=self._index_loop, daemon=True,
                                            name="agenttrace-tui-indexer")
            self._thread.start()

    def _index_loop(self) -> None:
        assert self.store is not None
        indexer = Indexer(self.store, self.dirs)
        try:
            while not self._stop.is_set():
                if common.index_locked():
                    self._stop.wait(self.interval)
                    continue
                self.scanning = True
                try:
                    if self.capture is not None:
                        self.capture.poll(self._stop)
                    if self._stop.is_set():
                        break
                    report = indexer.scan_once()
                    self.errors = report.errors + report.problems + (list(self.capture.errors) if self.capture else [])
                    self.scans += 1
                    self.updated_at = time.time()
                except Exception as exc:
                    self.errors = [f"{type(exc).__name__}: {exc}"]
                finally:
                    self.scanning = False
                self._stop.wait(self.interval)
        finally:
            self.store.close()

    def close(self) -> None:
        # The worker closes its own connection after its current atomic scan.
        self._stop.set()
        if self.store is not None:
            self.store.close()

    def _read(self, operation: Callable[[TraceStore], T]) -> T:
        if self.store is None:
            raise RuntimeError("The local archive is still opening")
        try:
            # A refresh may issue several queries while the indexer commits.
            # Pin one WAL read snapshot for their complete result.
            self.store._conn().execute("BEGIN")
            return operation(self.store)
        finally:
            self.store._conn().rollback()
            self.store.close()

    def dashboard(self, search: str = "", agent: str | None = None,
                  offset: int = 0) -> dict[str, Any]:
        def read(store: TraceStore) -> dict[str, Any]:
            from core.review import session_review
            sessions = query.list_sessions(store, search=search, agent=agent, offset=offset, limit=PAGE_SIZE)
            for row in sessions["sessions"]:
                review = session_review(store, row["session_id"], agent=row["adapter_id"])
                if review:
                    row["task"] = review["task"]
                    row["review_state"] = review["state"]
            return {
                "overview": query.overview(store),
                "sessions": sessions,
                "recent": query.list_traces(store, limit=24)["traces"],
                "usage_known": bool(store.one(
                    "SELECT 1 FROM spans WHERE input_tokens IS NOT NULL"
                    " OR output_tokens IS NOT NULL LIMIT 1")),
                "cache_known": bool(store.one(
                    "SELECT 1 FROM spans WHERE cache_read_tokens IS NOT NULL LIMIT 1")),
                "agents": [r[0] for r in store.query(
                    "SELECT DISTINCT adapter_id FROM sessions ORDER BY adapter_id")],
                "health": {"scanning": self.scanning, "scans": self.scans,
                           "index": self.index, "errors": list(self.errors),
                           "updated_at": self.updated_at,
                           "capture": self.capture.health() if self.capture else None},
            }
        return self._read(read)

    def repository(self, root: Path, session: str | None = None,
                   agent: str | None = None, from_turn: str | None = None, *,
                   auto_select_session: bool = True,
                   allow_missing_session: bool = False, include_task: bool = False) -> dict[str, Any]:
        from core.repo_review import repository_review
        return self._read(lambda store: repository_review(
            store, root, session=session, agent=agent, from_turn=from_turn,
            auto_select_session=auto_select_session, allow_missing_session=allow_missing_session,
            include_task=include_task))

    def save_worktree_task(self, root: Path, checkout: str, goal: str, *,
                           expected_task: dict[str, Any] | None,
                           pane_id: str | None, terminal_id: str | None,
                           agent: str | None, session_id: str | None) -> None:
        from core.review_git import checkout_identity
        from core.review_tasks import checkout_key, save_task, worktree_task
        if checkout_key(*checkout_identity(root)) != checkout:
            raise ValueError("The checkout changed. Refresh before saving the task.")
        if self.store is None:
            raise RuntimeError("The local archive is still opening")
        try:
            with self.store.transaction():
                if worktree_task(self.store, root, checkout) != expected_task:
                    raise ValueError("The task changed. Reopen it and try again.")
                save_task(self.store, root, checkout, goal, pane_id=pane_id, terminal_id=terminal_id,
                          agent=agent, session_id=session_id)
        finally:
            self.store.close()

    def record_task_event(self, task: dict[str, Any], kind: str, detail: str,
                          check_id: int | None = None) -> int:
        from core.review_git import checkout_identity
        from core.review_tasks import checkout_key, record_task_event
        if checkout_key(*checkout_identity(Path(task["root"]))) != task["checkout"]:
            raise ValueError("The checkout changed. Reopen the task and try again.")
        if self.store is None:
            raise RuntimeError("The local archive is still opening")
        try:
            return record_task_event(self.store, task, kind, detail, check_id)
        finally:
            self.store.close()

    def resolve_repair_event(self, event: int, *, sent: bool) -> None:
        from core.review_tasks import resolve_repair_event
        if self.store is None:
            raise RuntimeError("The local archive is still opening")
        try:
            resolve_repair_event(self.store, event, sent=sent)
        finally:
            self.store.close()

    def resolve_session(self, session_id: str, agent: str | None = None) -> SessionKey:
        def read(store: TraceStore) -> SessionKey:
            sql = "SELECT adapter_id FROM sessions WHERE session_id = ?"
            params = [session_id]
            if agent is not None:
                sql += " AND adapter_id = ?"
                params.append(agent)
            rows = store.query(sql + " LIMIT 2", tuple(params))
            if not rows:
                raise LookupError(f"No exact session match: {session_id}")
            if len(rows) > 1:
                raise ValueError(f"Session {session_id} is ambiguous; specify --agent")
            return rows[0][0], session_id
        return self._read(read)

    def session(self, key: SessionKey, offset: int = 0) -> dict[str, Any]:
        def read(store: TraceStore) -> dict[str, Any]:
            from core.review import session_review
            agent, session = key
            return {"review": session_review(store, session, agent=agent),
                    "turns": query.list_traces(store, agent=agent, session=session,
                                               offset=offset, limit=PAGE_SIZE)}
        return self._read(read)

    def trace(self, key: SessionKey, trace_id: str) -> dict[str, Any] | None:
        return self._read(lambda s: query.get_trace(
            s, trace_id, agent=key[0], session=key[1], summary=True))

    def span(self, key: SessionKey, trace_id: str, span_id: str) -> dict[str, Any] | None:
        def read(store: TraceStore) -> dict[str, Any] | None:
            span = query.get_span(store, trace_id, span_id, agent=key[0], session=key[1])
            if span and span.get("synthetic") and span_id == trace_id:
                trace = query.get_trace(store, trace_id, agent=key[0], session=key[1])
                span["events"] = trace["events"] if trace else []
            return span
        return self._read(read)
