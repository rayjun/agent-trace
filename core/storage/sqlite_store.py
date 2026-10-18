"""SQLite materialized store — the queryable projection over the JSONL truth.

Two tiers, mirroring the architecture (docs/architecture.md):

* `events` — the raw canonical archive. Idempotent by `event_id`; each row
  points back at its source file+line so a shrunk/rotated file can be
  deleted selectively and the projections rebuilt.
* `spans` / `sessions` — PROJECTIONS. They are rebuilt from `events`
  (`rebuild_projections`) or maintained incrementally (`ingest` +
  `refresh_projections`). A projection bug is fixed by rebuilding, never by
  patching rows: JSONL is the only truth.

Design rules inherited from cli/trace_index.py:

* Append-only ingest: only bytes past a file's last offset are parsed. A
  file that shrank invalidates its events (they would re-materalize from
  stale bytes) and is re-ingested from zero.
* WAL + busy_timeout: the CLI, the indexer and the HTTP server may all have
  the DB open; a writer must not transiently block readers into errors.
* Token/model columns on `spans` are a projection (like trace_index's
  PROJECTED): extracted once at ingest so stats never parse JSON in SQL.

Stdlib only (sqlite3).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from core.schema.model import TERMINAL_STATUSES, now_iso, validate_event
DB_SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_files (
  path TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  -- Identity of the file, so a REPLACEMENT is detected. Size alone only catches
  -- truncation: a file swapped for a different, LARGER one looked like an
  -- append, and the offset then landed in the middle of a record.
  inode INTEGER,
  indexed_bytes INTEGER NOT NULL,
  corrupt_lines INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  ts TEXT NOT NULL,
  adapter_id TEXT NOT NULL,
  session_id TEXT,
  trace_id TEXT,
  span_id TEXT,
  payload TEXT NOT NULL,
  source_file TEXT,
  source_line INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS spans (
  span_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  session_id TEXT,
  parent_span_id TEXT,
  adapter_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  start_ts TEXT,
  end_ts TEXT,
  duration_ms INTEGER,
  model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  input_json TEXT,
  output_json TEXT,
  error_json TEXT,
  attributes_json TEXT,
  privacy_json TEXT,
  source_json TEXT,
  agent_version TEXT,
  synthetic INTEGER NOT NULL DEFAULT 0,
  ended INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS idx_spans_span ON spans(span_id);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id, start_ts);
CREATE INDEX IF NOT EXISTS idx_spans_session ON spans(session_id);
CREATE INDEX IF NOT EXISTS idx_spans_parent ON spans(parent_span_id);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT NOT NULL,
  adapter_id TEXT NOT NULL,
  first_ts TEXT NOT NULL,
  last_ts TEXT NOT NULL,
  ended INTEGER NOT NULL DEFAULT 0,
  model TEXT,
  cwd TEXT,
  PRIMARY KEY (session_id, adapter_id)
);

-- M3: evaluation. Datasets hold inputs/expected/metadata ONLY — never a
-- wholesale copy of the source trace (proposal §7.1). Experiments pin the
-- dataset version + evaluator ids so a result is reproducible; runs link an
-- example to a recorded trace (playback scoring; real replay is M5).
CREATE TABLE IF NOT EXISTS datasets (
  dataset_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_examples (
  example_id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL,
  inputs TEXT NOT NULL,
  expected TEXT,
  metadata TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_examples_dataset ON dataset_examples(dataset_id);

CREATE TABLE IF NOT EXISTS experiments (
  experiment_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  dataset_version INTEGER NOT NULL,
  agent_config TEXT,
  evaluator_ids TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'created',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_runs (
  run_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  example_id TEXT NOT NULL,
  trace_id TEXT,
  status TEXT NOT NULL,
  tokens_in INTEGER,
  tokens_out INTEGER,
  cache_read INTEGER,
  duration_ms INTEGER,
  cost_usd REAL,
  scores TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_experiment ON experiment_runs(experiment_id);
"""

_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

#: The spans table definition, kept separate so the key migration can rebuild
#: it without duplicating the DDL above.
_SPANS_TABLE = """
CREATE TABLE IF NOT EXISTS spans (
  span_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  session_id TEXT,
  parent_span_id TEXT,
  adapter_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  start_ts TEXT,
  end_ts TEXT,
  duration_ms INTEGER,
  model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  input_json TEXT,
  output_json TEXT,
  error_json TEXT,
  attributes_json TEXT,
  privacy_json TEXT,
  source_json TEXT,
  agent_version TEXT,
  synthetic INTEGER NOT NULL DEFAULT 0,
  ended INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS idx_spans_span ON spans(span_id);
CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id, start_ts);
CREATE INDEX IF NOT EXISTS idx_spans_session ON spans(session_id);
CREATE INDEX IF NOT EXISTS idx_spans_parent ON spans(parent_span_id);
"""


def _spans_key_is_wrong(conn: sqlite3.Connection) -> bool:
    """True when `spans` still has the old single-column primary key."""
    rows = conn.execute("PRAGMA table_info(spans)").fetchall()
    if not rows:
        return False
    pk_columns = [r[1] for r in sorted(rows, key=lambda r: r[5]) if r[5]]
    return pk_columns != ["trace_id", "span_id"]

#: Columns added after the first release. `CREATE TABLE IF NOT EXISTS` cannot
#: add them to an existing database, and a projection that silently lacks a
#: column would make get_trace() hand out spans with missing keys — so the
#: migration is explicit and additive.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("ingest_files", "inode INTEGER"),
    ("spans", "privacy_json TEXT"),
    ("spans", "source_json TEXT"),
    ("spans", "agent_version TEXT"),
)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _TS_FMT)
    except (ValueError, TypeError):
        return None


def _duration_ms(start: str | None, end: str | None) -> int | None:
    a, b = _parse_ts(start), _parse_ts(end)
    if a is None or b is None:
        return None
    return max(0, int((b - a).total_seconds() * 1000))


class TraceStore:
    """One SQLite database of canonical events + projections.

    Thread-safe by connection-per-thread (the HTTP server is threaded; a
    shared sqlite3.Connection across threads is not safe by default).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        #: Per-ingest counters. "Rejected" used to cover three very different
        #: things — an event already present (idempotent re-read, expected), an
        #: event whose id already carried a DIFFERENT payload (an adapter bug),
        #: and a contract violation (also a bug). Reporting 28 of them for a
        #: clean run over real data made a healthy ingest look broken.
        self.added = 0
        self.duplicates = 0
        self.duplicate_conflicts = 0
        self.invalid = 0
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(DB_SCHEMA_VERSION),))
            conn.commit()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring a database from an older version up to the current shape.

        Two kinds of change, both explicit:
        * additive columns (CREATE TABLE IF NOT EXISTS cannot add them);
        * the `spans` key, which was `span_id` alone. Span ids are only unique
          WITHIN a trace — the v1 converter mints `llm:<request_id>`, and two
          sessions can both have `r1` — so a single-column key silently merged
          two sessions' spans into one row and let the second trace's error
          overwrite the first. SQLite cannot alter a PRIMARY KEY, so the table
          is rebuilt and its rows carried over.
        """
        for table, column_def in _MIGRATIONS:
            column = column_def.split()[0]
            existing = {r[1] for r in conn.execute(
                f"PRAGMA table_info({table})").fetchall()}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
        if _spans_key_is_wrong(conn):
            conn.execute("ALTER TABLE spans RENAME TO spans_old_key")
            # The old table's indexes keep their names in sqlite_master even
            # though they now belong to the renamed table, so `CREATE INDEX IF
            # NOT EXISTS` inside _SPANS_TABLE would skip them and DROP TABLE
            # would take them with it — leaving span/session/parent lookups as
            # full scans. Drop them first.
            for index in ("idx_spans_trace", "idx_spans_span", "idx_spans_trace_id",
                          "idx_spans_session", "idx_spans_parent"):
                conn.execute(f"DROP INDEX IF EXISTS {index}")
            conn.executescript(_SPANS_TABLE)
            names = [
                "span_id", "trace_id", "session_id", "parent_span_id",
                "adapter_id", "kind", "name", "status", "start_ts", "end_ts",
                "duration_ms", "model", "input_tokens", "output_tokens",
                "cache_read_tokens", "input_json", "output_json", "error_json",
                "attributes_json", "privacy_json", "source_json",
                "agent_version", "synthetic", "ended",
            ]
            columns = ", ".join(names)
            # trace_id was nullable under the old schema and is NOT NULL in
            # the new one; re-inserting such a row verbatim raised
            # IntegrityError and made the whole database unopenable. Fall back
            # to the span's own id, which is what the ingest path does anyway.
            conn.execute(
                f"INSERT OR REPLACE INTO spans ({columns})"
                f" SELECT {columns} FROM ("
                f"   SELECT span_id, COALESCE(trace_id, span_id) AS trace_id,"
                f"          session_id, parent_span_id, adapter_id, kind, name,"
                f"          status, start_ts, end_ts, duration_ms, model,"
                f"          input_tokens, output_tokens, cache_read_tokens,"
                f"          input_json, output_json, error_json,"
                f"          attributes_json, privacy_json, source_json,"
                f"          agent_version, synthetic, ended"
                f"   FROM spans_old_key)")
            conn.execute("DROP TABLE spans_old_key")

    # ------------------------------------------------------------------
    # ingest
    # ------------------------------------------------------------------

    def ingest(self, event: dict[str, Any], source_file: str | None = None,
               source_line: int | None = None) -> bool:
        """Validate + archive one event, then update projections.

        False means rejected (contract violation) or duplicated — either way
        nothing changed. Rejection never raises: a corrupt line in a 10k-line
        file must not take the rest of the file down with it.
        """
        if validate_event(event):
            self.invalid += 1
            return False
        with self._write_lock:
            conn = self._conn()
            try:
                existing = conn.execute(
                    "SELECT payload FROM events WHERE event_id = ?",
                    (event["event_id"],)).fetchone()
                if existing is not None:
                    # Same id, DIFFERENT payload means the id is not an
                    # identity — a contract violation. Dropping it silently
                    # hid the problem from every surface; it is counted and
                    # reported instead.
                    if _identity(json.loads(existing["payload"])) != _identity(event):
                        self.duplicate_conflicts += 1
                    else:
                        self.duplicates += 1
                    conn.rollback()
                    return False
                cur = conn.execute(
                    "INSERT INTO events"
                    " (event_id, type, ts, adapter_id, session_id, trace_id,"
                    "  span_id, payload, source_file, source_line)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (event["event_id"], event["type"], event["ts"],
                     event["adapter_id"], event.get("session_id"),
                     event.get("trace_id"), event.get("span_id"),
                     json.dumps(event, ensure_ascii=False),
                     source_file, source_line))
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                return False
        if cur.rowcount == 0:
            self.duplicates += 1
            return False  # duplicate event_id: deduped, projections unchanged
        self.added += 1
        self._apply_to_spans(event)
        return True

    def _apply_to_spans(self, event: dict[str, Any]) -> None:
        etype = event["type"]
        if etype == "span_start":
            self._upsert_span(event, is_start=True)
        elif etype == "span_end":
            self._upsert_span(event, is_start=False)

    def _upsert_span(self, event: dict[str, Any], is_start: bool) -> None:
        """Merge one half of a span into its row.

        The row is the join of both halves; either may arrive first (a hook
        flushing late, a crash). End attributes overlay start attributes
        because in converted v1 data the facts (usage, duration) live on the
        end. Token/model columns are projected from the merged attributes.
        """
        span = event.get("span") or {}
        span_id = event["span_id"]
        trace_id = event.get("trace_id") or span_id
        ts = event["ts"]
        attrs = span.get("attributes") or {}
        usage = attrs.get("usage") or {}

        with self._write_lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT * FROM spans WHERE span_id = ? AND trace_id = ?",
                (span_id, trace_id)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO spans"
                    " (span_id, trace_id, session_id, parent_span_id, adapter_id,"
                    "  kind, name, status, start_ts, end_ts, duration_ms, model,"
                    "  input_tokens, output_tokens, cache_read_tokens,"
                    "  input_json, output_json, error_json, attributes_json,"
                    "  privacy_json, source_json, agent_version, synthetic, ended)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (span_id, trace_id,
                     event.get("session_id"), event.get("parent_span_id"),
                     event["adapter_id"], span.get("kind", "chain"),
                     span.get("name", "?"),
                     span.get("status") or ("running" if is_start else "ok"),
                     ts if is_start else None,
                     None if is_start else ts,
                     attrs.get("duration_ms"),
                     attrs.get("model"),
                     usage.get("input_tokens"),
                     usage.get("output_tokens"),
                     usage.get("cache_read_tokens"),
                     _dump_or_none(span.get("input")),
                     _dump_or_none(span.get("output")),
                     _dump_or_none(span.get("error")),
                     _dump_or_none(attrs) if attrs else None,
                     _dump_or_none(event.get("privacy")),
                     _dump_or_none(event.get("source")),
                     event.get("agent_version"),
                     1 if attrs.get("synthetic") else 0,
                     0 if is_start else 1))
            else:
                merged_attrs = _merge_json(row["attributes_json"], attrs)
                usage = merged_attrs.get("usage") or {}
                start_ts = row["start_ts"] or (ts if is_start else None)
                if is_start:
                    end_ts = row["end_ts"]
                elif row["end_ts"] is None or ts >= row["end_ts"]:
                    end_ts = ts
                else:
                    end_ts = row["end_ts"]        # stale duplicate end
                duration = merged_attrs.get("duration_ms")
                if duration is None:
                    duration = _duration_ms(start_ts, end_ts)
                # A late span_start must never downgrade a row that already
                # reached a terminal state back to 'running' — but a TERMINAL
                # incoming status must always win, or a synthetic task row
                # (which is stored ended=1 from the moment it is derived)
                # swallows the real span_end's error and the failure becomes
                # invisible.
                incoming = span.get("status")
                # A terminal status arriving with an EARLIER timestamp than the
                # end we already have is a stale duplicate (a re-emitted hook
                # event, a replayed archive): accepting it would leave
                # end_ts=10:00:05 with the error from 10:00:02, a row that is
                # internally inconsistent. Compare against end_ts, and take a
                # terminal status only when it is at least as recent.
                stale = (row["end_ts"] is not None and ts < row["end_ts"])
                if incoming in TERMINAL_STATUSES and not stale:
                    status = incoming
                elif row["status"] in TERMINAL_STATUSES:
                    status = row["status"]
                else:
                    status = incoming or row["status"]
                # Kind/name/synthetic are identity facts the FIRST writer may
                # not have known: a synthetic task row materialized by
                # refresh_projections must yield to a real span_start that
                # arrives later, or a genuine task error stays invisible
                # (overview excludes synthetic rows).
                incoming_synthetic = 1 if attrs.get("synthetic") else 0
                is_start_update = 1 if is_start else 0
                conn.execute(
                    "UPDATE spans SET"
                    " start_ts=?, end_ts=?, status=?, duration_ms=?, model=?,"
                    " input_tokens=?, output_tokens=?, cache_read_tokens=?,"
                    " kind=CASE WHEN ? THEN ? ELSE kind END,"
                    " name=CASE WHEN ? THEN ? ELSE name END,"
                    " synthetic=CASE WHEN ? THEN 0 ELSE synthetic END,"
                    " privacy_json=COALESCE(privacy_json, ?),"
                    " source_json=COALESCE(source_json, ?),"
                    " agent_version=COALESCE(agent_version, ?),"
                    " input_json=COALESCE(input_json, ?),"
                    " output_json=COALESCE(output_json, ?),"
                    " error_json=COALESCE(error_json, ?),"
                    " attributes_json=?, ended=MAX(ended, ?)"
                    " WHERE span_id=? AND trace_id=?",
                    (start_ts, end_ts, status, duration,
                     merged_attrs.get("model") or row["model"],
                     usage.get("input_tokens") if usage else row["input_tokens"],
                     usage.get("output_tokens") if usage else row["output_tokens"],
                     (usage.get("cache_read_tokens") if usage
                      else row["cache_read_tokens"]),
                     is_start_update, span.get("kind"),
                     is_start_update, span.get("name"),
                     # a real (non-synthetic) start clears the synthetic flag
                     (is_start and not incoming_synthetic),
                     _dump_or_none(event.get("privacy")),
                     _dump_or_none(event.get("source")),
                     event.get("agent_version"),
                     _dump_or_none(span.get("input")),
                     _dump_or_none(span.get("output")),
                     _dump_or_none(span.get("error")),
                     json.dumps(merged_attrs, ensure_ascii=False),
                     0 if is_start else 1,
                     span_id, trace_id))
            conn.commit()

    # ------------------------------------------------------------------
    # projections
    # ------------------------------------------------------------------

    def refresh_projections(self) -> None:
        """Derive synthetic task spans + session rows from current data.

        Run after each ingest pass. Synthetic tasks are how a v1 turn (which
        no adapter ever delimited explicitly) gets a trace root: children
        converted from v1 point at parent_span_id == trace_id, so any trace
        with such children gets a task row bounding them. Bounds and status
        are RECOMPUTED each pass — an incremental ingest that appends to a
        turn widens its task instead of freezing it at first sight.
        """
        with self._write_lock:
            conn = self._conn()
            # Roots are traces that HAVE children (v1-converted turns) OR
            # traces that carry only discrete events. The second case matters:
            # a turn whose only records are user_prompt/assistant_message has
            # events but no span, and without a task row those events are
            # unreachable from list_traces/get_trace — the data exists in the
            # archive and no surface can show it.
            roots = {r[0] for r in conn.execute(
                "SELECT DISTINCT trace_id FROM spans"
                " WHERE parent_span_id = trace_id AND trace_id IS NOT NULL")}
            # An event-only trace is one with no span of its own. The check
            # must be scoped to the trace: `NOT IN (SELECT span_id FROM spans)`
            # was global, so an event whose trace_id happened to equal some
            # OTHER trace's span id was silently skipped and stayed invisible
            # in every surface.
            roots |= {r[0] for r in conn.execute(
                "SELECT DISTINCT trace_id FROM events e"
                " WHERE type = 'event' AND trace_id IS NOT NULL"
                "   AND NOT EXISTS (SELECT 1 FROM spans s"
                "                   WHERE s.span_id = e.trace_id"
                "                     AND s.trace_id = e.trace_id)")}
            for trace_id in sorted(roots):
                children = conn.execute(
                    "SELECT MIN(COALESCE(start_ts, end_ts)) AS first_ts,"
                    "       MAX(COALESCE(end_ts, start_ts)) AS last_ts,"
                    "       SUM(status IN ('error','interrupted','cancelled')"
                    "           AND synthetic = 0) AS bad,"
                    "       SUM(ended = 0) AS open_children,"
                    "       MIN(COALESCE(model, '')) AS model"
                    " FROM spans WHERE trace_id = ? AND span_id != ?",
                    (trace_id, trace_id)).fetchone()
                # Spanless (event-only) traces take their bounds from events.
                event_bounds = conn.execute(
                    "SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts,"
                    "       MIN(adapter_id) AS adapter_id, MAX(session_id) AS session_id"
                    " FROM events WHERE trace_id = ?", (trace_id,)).fetchone()
                first_ts = (children["first_ts"] if children else None) \
                    or (event_bounds["first_ts"] if event_bounds else None)
                last_ts = (children["last_ts"] if children else None) \
                    or (event_bounds["last_ts"] if event_bounds else None)
                if first_ts is None:
                    continue
                existing = conn.execute(
                    "SELECT synthetic FROM spans WHERE span_id = ? AND trace_id = ?",
                    (trace_id, trace_id)).fetchone()
                if existing is not None and not existing["synthetic"]:
                    continue  # a native task span outranks the synthetic one
                bad = (children["bad"] if children else 0) or 0
                open_children = (children["open_children"] if children else 0) or 0
                # A root whose child never closed is `interrupted`, not `ok`:
                # the trace-level status already says `incomplete` for exactly
                # this case, and a root span claiming "ok" while its own trace
                # claims "incomplete" is the kind of internal disagreement that
                # makes a reader distrust both numbers.
                status = ("error" if bad else
                          "interrupted" if open_children else "ok")
                attrs = {"synthetic": True}
                model = children["model"] if children else ""
                if model:
                    attrs["model"] = model
                conn.execute(
                    "INSERT INTO spans"
                    " (span_id, trace_id, session_id, parent_span_id, adapter_id,"
                    "  kind, name, status, start_ts, end_ts, duration_ms,"
                    "  attributes_json, synthetic, ended)"
                    " VALUES (?,?,?,NULL,?, 'task', ?, ?, ?, ?, ?, ?, 1, 1)"
                    " ON CONFLICT(trace_id, span_id) DO UPDATE SET"
                    "   start_ts=excluded.start_ts, end_ts=excluded.end_ts,"
                    "   status=excluded.status,"
                    "   duration_ms=excluded.duration_ms,"
                    "   attributes_json=excluded.attributes_json",
                    (trace_id, trace_id,
                     # session/adapter: prefer the events' values (they exist
                     # even when no span does), else the child spans'.
                     (event_bounds["session_id"] if event_bounds else None)
                     or _first_value(conn, "session_id", trace_id),
                     (event_bounds["adapter_id"] if event_bounds else None)
                     or _first_value(conn, "adapter_id", trace_id),
                     "task" if not model else f"task ({model})",
                     status, first_ts, last_ts,
                     # A derived root has no reported duration; computing it
                     # from its own bounds is the only honest answer, and
                     # leaving it NULL made every dashboard percentile read 0ms
                     # ("tasks are instant"), which is worse than absent.
                     _duration_ms(first_ts, last_ts),
                     json.dumps(attrs)))
            self._refresh_sessions(conn)
            conn.commit()

    def _refresh_sessions(self, conn: sqlite3.Connection) -> None:
        """Sessions from events; `ended` when a session_end event exists."""
        conn.execute(
            "INSERT INTO sessions (session_id, adapter_id, first_ts, last_ts, ended)"
            " SELECT session_id, adapter_id, MIN(ts), MAX(ts),"
            "        MAX(CASE WHEN json_extract(payload, '$.event.name')"
            "                  = 'session_end' THEN 1 ELSE 0 END)"
            " FROM events WHERE session_id IS NOT NULL"
            " GROUP BY session_id, adapter_id"
            " ON CONFLICT(session_id, adapter_id) DO UPDATE SET"
            "   first_ts=MIN(first_ts, excluded.first_ts),"
            "   last_ts=MAX(last_ts, excluded.last_ts),"
            "   ended=MAX(ended, excluded.ended)")
        # model/cwd: first known per session, from llm/tool span projections
        conn.execute(
            "UPDATE sessions SET model = ("
            "   SELECT model FROM spans s"
            "   WHERE s.session_id = sessions.session_id AND s.model IS NOT NULL"
            "   LIMIT 1)"
            " WHERE model IS NULL")
        conn.execute(
            "UPDATE sessions SET cwd = ("
            "   SELECT json_extract(s.attributes_json, '$.cwd') FROM spans s"
            "   WHERE s.session_id = sessions.session_id"
            "     AND json_extract(s.attributes_json, '$.cwd') IS NOT NULL"
            "   LIMIT 1)"
            " WHERE cwd IS NULL")

    def rebuild_projections(self) -> None:
        """Drop and replay every projection from the events archive.

        The recovery path for any projection inconsistency: JSONL is truth,
        SQLite is derived. Replay in ts order so span upserts see starts
        before ends where the source had them so.
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute("DELETE FROM spans")
            conn.execute("DELETE FROM sessions")
            rows = conn.execute(
                "SELECT payload FROM events ORDER BY ts, rowid").fetchall()
            conn.commit()
        for (payload,) in rows:
            event = json.loads(payload)
            if event["type"] in ("span_start", "span_end"):
                self._upsert_span(event, is_start=event["type"] == "span_start")
        self.refresh_projections()

    # ------------------------------------------------------------------
    # ingest file bookkeeping
    # ------------------------------------------------------------------

    def file_state(self, path: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM ingest_files WHERE path = ?", (path,)).fetchone()

    def note_file(self, path: str, size: int, mtime: float, indexed_bytes: int,
                  corrupt_lines: int = 0, last_error: str | None = None,
                  inode: int | None = None) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO ingest_files"
                " (path, size, mtime, inode, indexed_bytes, corrupt_lines,"
                "  last_error, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET"
                "   size=excluded.size, mtime=excluded.mtime,"
                "   inode=excluded.inode,"
                "   indexed_bytes=excluded.indexed_bytes,"
                "   corrupt_lines=excluded.corrupt_lines,"
                "   last_error=excluded.last_error, updated_at=excluded.updated_at",
                (path, size, mtime, inode, indexed_bytes, corrupt_lines,
                 last_error, now_iso()))
            conn.commit()

    def invalidate_file(self, path: str) -> None:
        """A file shrank (rotation/truncation): drop what came from it.

        Offsets into stale bytes would re-ingest the WRONG records, so the
        rows attributed to this file must go. But `events.source_file` records
        only the file we FIRST read an event from: an event that was deduped
        across two files disappears from the archive even though the other
        file still holds it, and nothing would ever re-read it because that
        file's offset is already past it. So every surviving file is rewound
        to zero and re-read on the next scan; event_id dedup makes that
        idempotent, and a rotation is rare enough that the extra pass is the
        cheap way to keep "JSONL is truth" actually true.
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute("DELETE FROM events WHERE source_file = ?", (path,))
            conn.execute("DELETE FROM ingest_files WHERE path = ?", (path,))
            conn.execute("UPDATE ingest_files SET indexed_bytes = 0")
            conn.commit()
        self.rebuild_projections()

    # ------------------------------------------------------------------
    # queries used by the HTTP layer (rows out; shaping lives in core/query)
    # ------------------------------------------------------------------

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self._conn().execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, params).fetchone()

    def iter_events(self, trace_id: str) -> Iterator[dict[str, Any]]:
        rows = self.query(
            "SELECT payload FROM events WHERE trace_id = ? ORDER BY ts, rowid",
            (trace_id,))
        for (payload,) in rows:
            yield json.loads(payload)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def _dump_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


#: The fields that make an event THAT event. This is what "the same event_id
#: carried a different payload" must be judged on.
_IDENTITY_FIELDS = ("event_id", "type", "trace_id", "span_id", "ts")


def _identity(event: dict[str, Any]) -> tuple[Any, ...]:
    """The identity of a canonical event.

    A full-payload comparison was too strict: a round trip through OTLP
    legitimately normalises payloads (null keys cannot be represented at all;
    a span's two halves must agree on one name/kind, which the model itself
    requires; per-half attributes arrive as one merged set). Those are
    normalisations of the SAME event, and refusing the second write is the
    correct handling either way — but calling them an "event_id conflict"
    buried the one thing this counter exists to catch: an id reused for an
    event that is not the same event.
    """
    return tuple(event.get(field) for field in _IDENTITY_FIELDS)


def _first_value(conn: sqlite3.Connection, column: str, trace_id: str) -> str | None:
    """First non-null `column` among a trace's spans. `column` is a literal
    from this module, never user input — the f-string cannot be injected."""
    row = conn.execute(
        f"SELECT {column} FROM spans WHERE trace_id = ? AND {column} IS NOT NULL"
        " LIMIT 1", (trace_id,)).fetchone()
    return row[0] if row else None


def _merge_json(existing: str | None, overlay: dict[str, Any]) -> dict[str, Any]:
    base: dict[str, Any] = {}
    if existing:
        try:
            parsed = json.loads(existing)
            if isinstance(parsed, dict):
                base = parsed
        except json.JSONDecodeError:
            pass
    base.update(overlay or {})
    return base
