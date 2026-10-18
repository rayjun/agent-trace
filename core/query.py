"""Query service — the semantic layer between the store and every surface
(HTTP API now, SDK later). Returns plain dicts; transport lives elsewhere.

Trace listing derives traces by GROUPing spans on trace_id rather than from a
materialized table: one less projection to keep consistent, and at local
scale (tens of thousands of spans) the group-by is milliseconds.

Status rollup rule (used everywhere, defined once):
  error      — any span in a failure terminal state
  incomplete — no failure, but at least one span never closed
  ok         — everything closed cleanly
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.storage.sqlite_store import TraceStore, _duration_ms

_TRACE_SELECT = """
SELECT s.trace_id AS trace_id,
       MIN(COALESCE(s.start_ts, s.end_ts)) AS first_ts,
       MAX(COALESCE(s.end_ts, s.start_ts)) AS last_ts,
       COUNT(*) AS span_count,
       -- Synthetic task rows only MIRROR their children, so letting them vote
       -- in these aggregates is self-fulfilling: a root marked `interrupted`
       -- because a child is open would itself count as an error and flip the
       -- trace from `incomplete` to `error`. Same rule as overview/per_agent.
       SUM(s.ended = 0 AND s.synthetic = 0) AS unclosed,
       SUM(s.status IN ('error','interrupted','cancelled')
           AND s.synthetic = 0) AS errors,
       SUM(COALESCE(s.input_tokens, 0)) AS input_tokens,
       SUM(COALESCE(s.output_tokens, 0)) AS output_tokens,
       SUM(COALESCE(s.cache_read_tokens, 0)) AS cache_read_tokens,
       MIN(s.adapter_id) AS adapter_id,
       MAX(s.session_id) AS session_id,
       root.name AS root_name,
       root.status AS root_status
FROM spans s
LEFT JOIN spans root ON root.span_id = s.trace_id AND root.kind = 'task'
GROUP BY s.trace_id
"""


def _trace_status(errors: int, unclosed: int) -> str:
    if errors:
        return "error"
    if unclosed:
        return "incomplete"
    return "ok"


def _row_to_trace(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "trace_id": row["trace_id"],
        "name": row["root_name"] or "trace",
        "adapter_id": row["adapter_id"],
        "session_id": row["session_id"],
        "status": _trace_status(row["errors"], row["unclosed"]),
        "start": row["first_ts"],
        "end": row["last_ts"],
        "duration_ms": _duration_ms(row["first_ts"], row["last_ts"]),
        "span_count": row["span_count"],
        "unclosed": row["unclosed"],
        "tokens": {
            "input": row["input_tokens"],
            "output": row["output_tokens"],
            "cache_read": row["cache_read_tokens"],
        },
    }


def list_traces(store: TraceStore, *, agent: str | None = None,
                status: str | None = None, since: str | None = None,
                limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Trace list. `since` filters WHOLE traces by their start time.

    The filter is applied to the aggregate, not spliced into the span WHERE
    clause: filtering rows first would clamp `start` upward and drop the
    tokens/spans of earlier work, so a long trace would silently report a
    partial duration and a partial token count.
    """
    where: list[str] = []
    having: list[str] = []
    params: list[Any] = []
    if agent:
        where.append("s.adapter_id = ?")
        params.append(agent)
    if status == "error":
        having.append("errors > 0")
    elif status == "incomplete":
        having.append("errors = 0 AND unclosed > 0")
    elif status == "ok":
        having.append("errors = 0 AND unclosed = 0")

    sql = _TRACE_SELECT
    if where:
        sql = sql.replace("GROUP BY", "WHERE " + " AND ".join(where) + "\nGROUP BY")
    if having:
        sql += "\nHAVING " + " AND ".join(having)
    outer_where = ""
    if since:
        outer_where = "WHERE first_ts >= ?"
        params.append(since)
    base = f"SELECT * FROM ({sql}) {outer_where}"
    total = store.one(f"SELECT COUNT(*) AS n FROM ({base})", tuple(params))["n"]
    rows = store.query(
        base + "\nORDER BY first_ts DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset))
    return {"total": total, "traces": [_row_to_trace(r) for r in rows]}


def get_trace(store: TraceStore, trace_id: str) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT * FROM spans WHERE trace_id = ?"
        " ORDER BY COALESCE(start_ts, end_ts), rowid", (trace_id,))
    if not rows:
        return None
    spans = [_row_to_span(r) for r in rows]
    events = [
        {
            # Every envelope field a discrete event needs to round-trip. Each
            # omission here is invisible until something compares payloads:
            # `privacy` and `observed_at` were both being dropped, which made
            # an exported event come back different from the one on disk.
            "event_id": e.get("event_id"),
            "ts": e["ts"],
            "observed_at": e.get("observed_at"),
            "name": (e.get("event") or {}).get("name"),
            "attributes": (e.get("event") or {}).get("attributes"),
            "span_id": e.get("span_id"),
            "privacy": e.get("privacy"),
            "source": e.get("source"),
            "agent_version": e.get("agent_version"),
        }
        for e in store.iter_events(trace_id) if e["type"] == "event"
    ]
    first_ts = min(s["start"] or s["end"] or "" for s in spans)
    last_ts = max(s["end"] or s["start"] or "" for s in spans)
    errors = sum(1 for s in spans
                 if s["status"] in ("error", "interrupted", "cancelled")
                 and not s["synthetic"])
    unclosed = sum(1 for s in spans if not s["ended"])
    # Summing with `or 0` per span turns "no adapter captured usage" into a
    # confident 0, which downstream turns into a token budget that passes and
    # a cost of zero. Report None when NO span has any token figure.
    has_usage = any(t is not None for s in spans
                    for t in (s["tokens"]["input"], s["tokens"]["output"],
                              s["tokens"]["cache_read"]))
    # The workspace belongs to the SESSION, and the adapter records it on a
    # session-level event (the only cheap place: once per session, not per
    # span). Looking only at this trace's spans therefore found nothing on every
    # trace. Prefer a snapshot that carries `git` — a bare `cwd` is not what a
    # reader came for — and fall back to any resource at all.
    # A span whose window CONTAINS the start of another trace in the same session
    # was interrupted: the process died or the session was resumed between its
    # two halves, so its duration is a gap rather than work (one codex span
    # "took" 26 days that way). Structural, so no threshold to tune, and a
    # genuine long call has nothing else starting inside it.
    starts: list[str] = []
    session_id = rows[0]["session_id"]
    if session_id:
        starts = [r["start_ts"] for r in store.query(
            "SELECT DISTINCT trace_id, MIN(start_ts) AS start_ts FROM spans"
            " WHERE session_id = ? AND trace_id != ? AND start_ts IS NOT NULL"
            " GROUP BY trace_id", (session_id, trace_id))]
    for span in spans:
        begin, finish = span.get("start"), span.get("end")
        if begin and finish:
            span["duration_suspect"] = any(begin < other < finish
                                          for other in starts)

    workspace = _session_workspace(store, rows[0]["session_id"],
                                   [s.get("resource") for s in spans])
    return {
        "trace_id": trace_id,
        "adapter_id": rows[0]["adapter_id"],
        "session_id": rows[0]["session_id"],
        "workspace": workspace,
        "status": _trace_status(errors, unclosed),
        "start": first_ts,
        "end": last_ts,
        "duration_ms": _duration_ms(first_ts, last_ts),
        "tokens": {
            "input": (sum(s["tokens"]["input"] or 0 for s in spans)
                      if has_usage else None),
            "output": (sum(s["tokens"]["output"] or 0 for s in spans)
                       if has_usage else None),
            "cache_read": (sum(s["tokens"]["cache_read"] or 0 for s in spans)
                           if has_usage else None),
        },
        "spans": spans,
        "events": events,
    }


def _session_workspace(store: TraceStore, session_id: str | None,
                       own: list[Any]) -> dict[str, Any] | None:
    """The workspace of the session a trace belongs to.

    Read from the session's events, because that is where an adapter puts it.
    `own` (this trace's spans) is tried first so a per-span resource still wins
    for an adapter that records one there.
    """
    candidates = [r for r in own if isinstance(r, dict)]
    if session_id:
        for row in store.query(
                "SELECT payload FROM events WHERE session_id = ?"
                ' AND payload LIKE \'%"resource"%\' LIMIT 20', (session_id,)):
            try:
                event = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            resource = event.get("resource")
            if isinstance(resource, dict):
                candidates.append(resource)
    if not candidates:
        return None
    for resource in candidates:
        if isinstance(resource.get("git"), dict):
            return resource
    return candidates[0]


def _row_to_span(row: sqlite3.Row) -> dict[str, Any]:
    attrs = _load(row["attributes_json"])
    return {
        "span_id": row["span_id"],
        "parent_span_id": row["parent_span_id"],
        "kind": row["kind"],
        "name": row["name"],
        # `incomplete` is derived HERE, at read time, not stored: the stored
        # status stays `running` so that a late span_end can self-heal the
        # display without a rewrite. Consumers that read the spans table
        # directly see `running` — docs/schema.md says so.
        "status": row["status"] if row["ended"] else "incomplete",
        "start": row["start_ts"],
        "end": row["end_ts"],
        "duration_ms": row["duration_ms"],
        "model": row["model"],
        "tokens": {
            "input": row["input_tokens"],
            "output": row["output_tokens"],
            "cache_read": row["cache_read_tokens"],
        },
        "input": _load(row["input_json"]),
        "output": _load(row["output_json"]),
        "error": _load(row["error_json"]),
        "attributes": attrs,
        "privacy": _load(row["privacy_json"]),
        "source": _load(row["source_json"]),
        "resource": _load(row["resource_json"]),
        "agent_version": row["agent_version"],
        "synthetic": bool(row["synthetic"]),
        "ended": bool(row["ended"]),
    }


def list_sessions(store: TraceStore, *, limit: int = 100) -> dict[str, Any]:
    rows = store.query(
        "SELECT s.*, "
        " (SELECT COUNT(DISTINCT trace_id) FROM spans sp"
        "   WHERE sp.session_id = s.session_id) AS trace_count,"
        " (SELECT COALESCE(SUM(sp.input_tokens),0) FROM spans sp"
        "   WHERE sp.session_id = s.session_id) AS input_tokens,"
        " (SELECT COALESCE(SUM(sp.output_tokens),0) FROM spans sp"
        "   WHERE sp.session_id = s.session_id) AS output_tokens"
        " FROM sessions s ORDER BY last_ts DESC LIMIT ?", (limit,))
    return {"sessions": [
        {
            "session_id": r["session_id"],
            "adapter_id": r["adapter_id"],
            "first_ts": r["first_ts"],
            "last_ts": r["last_ts"],
            "ended": bool(r["ended"]),
            "model": r["model"],
            "cwd": r["cwd"],
            "trace_count": r["trace_count"],
            "tokens": {"input": r["input_tokens"], "output": r["output_tokens"]},
        } for r in rows]}


def session_detail(store: TraceStore, session_id: str) -> dict[str, Any] | None:
    """One session as a CONVERSATION: every turn, with its own duration.

    A trace is one turn (that is what "Trace: one task execution" means), so a
    reader looking at a trace sees one round and cannot tell how long the
    conversation took, where the time went, or how much of it was the agent
    working rather than the person typing. Those are different numbers and both
    are useful:

      active_ms     — the sum of a session's turn ACTIVE time (what the agent
                      actually spent: the work of each span, added up)
      wall_ms       — first turn's start to last turn's end
      outside_ms    — the difference: the person thinking, the machine idle, or
                      the session sitting open between resumes

    A turn also carries both, because a turn's wall clock is not its work: a
    session that is resumed the next day has a turn whose start and end straddle
    the gap, and summing THOSE produced a session "agent time" of 9.5 days inside
    a 1.1-day window — an impossible number that hid the real one. `active_ms` is
    the sum of a turn's own spans, which cannot exceed the work done.
    """
    rows = store.query(
        "SELECT * FROM spans WHERE session_id = ? AND kind = 'task'"
        " ORDER BY COALESCE(start_ts, end_ts)", (session_id,))
    if not rows:
        return None

    turns: list[dict[str, Any]] = []
    for row in rows:
        trace_id = row["trace_id"]
        children = store.query(
            "SELECT kind, name, status, duration_ms, model,"
            "       input_tokens, output_tokens, cache_read_tokens"
            " FROM spans WHERE trace_id = ? AND span_id != ?"
            " ORDER BY COALESCE(start_ts, end_ts)", (trace_id, trace_id))
        kinds = sorted({c["kind"] for c in children})
        errors = sum(1 for c in children
                     if c["status"] in ("error", "interrupted", "cancelled"))
        models = [c["model"] for c in children if c["model"]]
        # The work of this turn: its spans' own durations. Bounded by reality in
        # a way the span's start-end difference is not (a resume gap between a
        # request and its response is not time spent working).
        active = sum(c["duration_ms"] or 0 for c in children)
        turns.append({
            "trace_id": trace_id,
            "name": row["name"],
            "start": row["start_ts"],
            "end": row["end_ts"],
            "duration_ms": row["duration_ms"],
            "active_ms": active,
            "status": row["status"] if row["ended"] else "incomplete",
            "spans": len(children),
            "kinds": kinds,
            "errors": errors,
            "model": models[0] if models else None,
            "tokens": {
                "input": sum(c["input_tokens"] or 0 for c in children),
                "output": sum(c["output_tokens"] or 0 for c in children),
                "cache_read": sum(c["cache_read_tokens"] or 0 for c in children),
            },
        })

    # Structural, not a threshold: a turn whose window CONTAINS another turn's
    # start was interrupted — the session was resumed, or the process died and
    # was restarted — so its wall clock is a gap, not work. No magic number, and
    # it cannot be fooled by a slow model: nothing else starts inside a genuine
    # 20-minute call.
    starts = sorted(t["start"] for t in turns if t["start"])
    for turn in turns:
        begin, finish = turn["start"], turn["end"]
        if begin and finish:
            turn["interrupted"] = any(
                begin < other < finish for other in starts)

    # Statistics over the turns we can trust. A turn whose window contains
    # another turn's start was interrupted, so its wall clock is partly a gap;
    # including it would report the gap as work (that is how codex's flushed
    # turns produced an 18-hour "average latency"). The count of excluded turns
    # is reported rather than hidden, and `work_ms` is a floor, not a guess.
    trusted = [t for t in turns if not t.get("interrupted")]
    durations = sorted(t["duration_ms"] or 0 for t in trusted)
    total = sum(durations)
    first = min((t["start"] or t["end"] or "") for t in turns)
    last = max((t["end"] or t["start"] or "") for t in turns)
    wall = _duration_ms(first, last) or 0

    def _pct(q: float) -> int | None:
        if not durations:
            return None
        return durations[min(len(durations) - 1, int(q * len(durations)))]

    models: dict[str, dict[str, int]] = {}
    for turn in turns:
        if not turn["model"]:
            continue
        bucket = models.setdefault(turn["model"], {"turns": 0, "input": 0})
        bucket["turns"] += 1
        bucket["input"] += turn["tokens"]["input"]

    return {
        "session_id": session_id,
        "adapter_id": rows[0]["adapter_id"],
        "start": first,
        "end": last,
        "turns": turns,
        "turn_count": len(turns),
        "work_ms": total,
        "work_excluded_turns": len(turns) - len(trusted),
        "turn_p50_ms": _pct(0.50),
        "turn_p95_ms": _pct(0.95),
        "wall_ms": wall,
        # Never negative: clocks in the source data are not guaranteed monotone,
        # and a negative "outside" would read as a bug in the agent.
        # Outside the turns: the session was open but the agent was not running
        # (the person thinking, or the process gone). Measured against the
        # TRUSTED turns only — an interrupted turn's wall clock is itself mostly
        # a gap, and subtracting it made a 27-hour session report zero idle time,
        # i.e. "the agent worked the whole time".
        "outside_ms": max(0, wall - total),
        "interrupted_turns": len(turns) - len(trusted),
        "errors": sum(t["errors"] for t in turns),
        "tokens": {
            "input": sum(t["tokens"]["input"] for t in turns),
            "output": sum(t["tokens"]["output"] for t in turns),
            "cache_read": sum(t["tokens"]["cache_read"] for t in turns),
        },
        "models": models,
    }


def overview(store: TraceStore) -> dict[str, Any]:
    # errors exclude synthetic task rows: a synthetic root only MIRRORS its
    # children's failure — counting it double-counts every real error.
    totals = store.one(
        "SELECT COUNT(DISTINCT trace_id) AS traces, COUNT(*) AS spans,"
        " SUM(status IN ('error','interrupted','cancelled')"
        "     AND synthetic = 0) AS errors,"
        " SUM(ended = 0) AS unclosed,"
        " COALESCE(SUM(input_tokens),0) AS in_tok,"
        " COALESCE(SUM(output_tokens),0) AS out_tok,"
        " COALESCE(SUM(cache_read_tokens),0) AS cache_tok"
        " FROM spans") or {}
    per_agent = store.query(
        "SELECT adapter_id, COUNT(DISTINCT trace_id) AS traces,"
        " COUNT(*) AS spans,"
        # synthetic task rows only MIRROR a child's failure; counting them
        # would report two errors for one real one (and disagree with the
        # totals query above, which already excludes them).
        " SUM(status IN ('error','interrupted','cancelled')"
        "     AND synthetic = 0) AS errors,"
        " COALESCE(SUM(input_tokens),0) AS in_tok,"
        " COALESCE(SUM(output_tokens),0) AS out_tok"
        " FROM spans GROUP BY adapter_id ORDER BY traces DESC")
    # Only rows that actually have a duration. `COALESCE(duration_ms, 0)`
    # reported p50/p95 = 0ms for every trace whose task span carried none,
    # which reads as "all tasks are instant" rather than "not measured".
    durations = sorted(r[0] for r in store.query(
        "SELECT duration_ms FROM spans"
        " WHERE kind = 'task' AND duration_ms IS NOT NULL"))
    sessions = store.one("SELECT COUNT(*) AS n FROM sessions")
    return {
        "traces": totals["traces"] or 0,
        "spans": totals["spans"] or 0,
        "errors": totals["errors"] or 0,
        "unclosed": totals["unclosed"] or 0,
        "sessions": sessions["n"] if sessions else 0,
        "tokens": {"input": totals["in_tok"], "output": totals["out_tok"],
                   "cache_read": totals["cache_tok"]},
        "task_duration_ms": {
            "p50": _pct(durations, 0.50),
            "p95": _pct(durations, 0.95),
        },
        "per_agent": [dict(r) for r in per_agent],
    }


def list_adapters(store: TraceStore, *,
                  index_ready: bool = True) -> dict[str, Any]:
    """Registry declarations + what the data actually shows. Both, side by
    side — the Integrations page's job is to make drift between the two
    visible, not to hide it.

    M4 additions: `source` (builtin / entry_point / manifest) answers "where
    did this come from", and `compatibility` answers "what test proves it".
    A claim without a test renders as `unverified` rather than being hidden.
    """
    from adapter_sdk import compatibility, discovery
    from core import coverage as coverage_mod

    # Coverage answers a question the registry cannot: "the adapter is
    # installed" and "the adapter is capturing" are different facts. A live
    # hook is loaded at PROCESS START, so an agent that was already running
    # when the adapter was installed keeps writing nothing at all.
    # Index-backed: the indexer already stores (session_id, adapter_id) for
    # every event, so this is one indexed SELECT. Reading the raw trace files
    # here meant re-parsing 1.1 GB inside a request, which timed out the
    # dashboard's own adapters page.
    #
    # Until the first index pass finishes, the honest answer is "not yet": the
    # sessions projection is written at the END of a pass, so mid-pass every
    # adapter whose file has not been read looks like it captures nothing.
    if index_ready:
        provider = coverage_mod.store_session_provider(store.path)
        cov = {c.adapter_id: c for c in coverage_mod.cached(traced_provider=provider)}
        coverage_note = None
    else:
        cov = {}
        coverage_note = ("the index is still building its first pass; coverage "
                         "will be judged once it finishes")

    observed = {
        r["adapter_id"]: dict(r)
        for r in store.query(
            "SELECT adapter_id, COUNT(*) AS spans,"
            " SUM(ended = 0) AS unclosed,"
            " MAX(COALESCE(end_ts, start_ts)) AS last_activity"
            " FROM spans GROUP BY adapter_id")
    }
    files = [
        dict(r) for r in store.query(
            "SELECT path, indexed_bytes, corrupt_lines, last_error, updated_at"
            " FROM ingest_files ORDER BY updated_at DESC LIMIT 50")
    ]
    discovered = {e.adapter_id: e for e in discovery.discover()}
    adapters = []
    for entry in discovery.discover():
        compat = compatibility.for_adapter(entry.adapter_id)
        adapters.append({
            **entry.to_dict(),
            "observed": observed.get(entry.adapter_id),
            "coverage": cov[entry.adapter_id].to_dict()
            if entry.adapter_id in cov else None,
            "compatibility": ({"target": compat.target,
                               "status": compat.status,
                               "verified_by": compat.verified_by,
                               "notes": compat.notes} if compat else None),
        })
    return {
        "adapters": adapters,
        "ingest_files": files,
        "coverage_ready": index_ready,
        "coverage_note": coverage_note,
        "coverage_warnings": [c.warning() for c in cov.values() if c.warning()],
    }


def _load(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _pct(sorted_vals: list[int], q: float) -> int | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[idx]
