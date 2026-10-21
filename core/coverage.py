"""Coverage: does an installed adapter actually have traces for the sessions
its agent ran?

Why this exists: "the adapter is installed" and "the adapter is capturing" are
different facts, and the dashboard only knows the first. The Pi adapter is a
live hook loaded at PROCESS START, so a session that began before the extension
was registered — or while the process was already running — is invisible no
matter how healthy the registry looks. On the machine that motivated this, 53
of 63 Pi sessions had no traces and nothing anywhere said so; it was found by
eye, from an empty dashboard.

So each adapter declares where its agent leaves session artefacts, and this
module compares that inventory against the sessions we actually have traces
for. The comparison is deliberately split by AGE relative to when the adapter
was installed:

  * sessions AFTER the install with no traces  -> something is wrong NOW
    (typically: the agent process predates the install, or the hook is broken)
  * sessions BEFORE the install               -> expected for a new install,
    and the candidates a backfill can recover

Collapsing those two into one number is what makes a warning ignorable.

Honest limits, stated rather than hidden: the inventory is built from files the
agent happens to leave on disk. An agent that prunes them, or a session with no
artefacts at all, is invisible to this check — so it reports what it can see and
names the source it used, instead of implying completeness.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SessionSource:
    """Where one agent leaves evidence that a session happened.

    Two kinds, because the agents differ: Pi and Codex write one file per
    session, while Hermes records sessions as ROWS in its own SQLite database
    (authoritative, and therefore better than guessing from its request dumps,
    which only exist for some sessions and repeat per request).
    """

    adapter_id: str
    label: str
    kind: str = "files"                # files | sqlite
    root: Path | None = None
    pattern: str = "*.jsonl"
    #: sqlite sources: opened READ-ONLY, because this database belongs to the
    #: agent and a coverage check has no business writing to it or holding a
    #: lock it did not ask for.
    db: Path | None = None
    query: str = ""
    #: sqlite sources: the query's second column is an epoch float, not an ISO
    #: string (Hermes stores `started_at` that way).
    time_is_epoch: bool = False
    #: How to turn an artefact path into the session id the traces carry. The
    #: live adapters mostly use the file path itself, which is what makes the
    #: comparison exact instead of a guess.
    key: str = "path"          # "path" | "stem" | "prefix'
    #: A file whose mtime marks when this adapter was installed. Used only to
    #: split the untraced sessions into "recent" and "historical".
    install_marker: Path | None = None
    #: How to recover the missing sessions, shown next to the warning.
    backfill_hint: str = ""


@dataclass
class Coverage:
    adapter_id: str
    label: str
    source: str
    #: Session artefacts the agent left on disk (deduplicated — Hermes writes
    #: one dump per REQUEST, so counting files would report 72 "sessions"
    #: where there are 20).
    sessions_known: int = 0
    #: Of those, how many we also have traces for. This is the coverage figure.
    sessions_covered: int = 0
    #: Trace files name sessions that have no surviving artefact at all (the
    #: agent pruned it). Reported so the two numbers are not misread as a
    #: contradiction.
    sessions_traced_only: int = 0
    untraced_recent: list[tuple[str, str]] = field(default_factory=list)
    untraced_historical: int = 0
    #: Untraced sessions whose age cannot be judged because the adapter left no
    #: install marker. Kept visible and kept OUT of the warning: "we cannot
    #: tell" must not be reported as "this is broken now", or every fresh
    #: install would scream about its own history and the warning would be
    #: ignored exactly when it matters.
    untraced_unknown_age: int = 0
    installed_at: str | None = None
    backfill_hint: str = ""
    error: str | None = None
    #: A note that is not a defect: "the agent has left no artefacts here yet".
    note: str | None = None
    #: True when no verdict could be reached (an index that is not built yet).
    #: Distinct from ok: "we cannot tell" is not "all is well", and it is also
    #: not a defect — the caller decides what to do with it.
    undecided: bool = False
    #: How the install line was decided (persisted vs the marker's mtime).
    install_source: str | None = None

    @property
    def missing(self) -> int:
        return (len(self.untraced_recent) + self.untraced_historical
                + self.untraced_unknown_age)

    @property
    def ok(self) -> bool:
        """No KNOWN defect. Unknown (an unreadable source) is not a defect.

        Conflating the two made a transient condition — an index still
        building, a database locked for a moment — read as "the adapter is
        broken", which is both wrong and the fastest way to get a warning
        ignored. `error` still surfaces in the message.
        """
        return not self.untraced_recent

    def to_dict(self) -> dict:
        return {
            "adapter_id": self.adapter_id,
            "label": self.label,
            "source": self.source,
            "sessions_known": self.sessions_known,
            "sessions_covered": self.sessions_covered,
            "sessions_traced_only": self.sessions_traced_only,
            "untraced_recent": [{"session": s, "started": t}
                                for s, t in self.untraced_recent[:20]],
            "untraced_recent_count": len(self.untraced_recent),
            "untraced_historical": self.untraced_historical,
            "untraced_unknown_age": self.untraced_unknown_age,
            "missing": self.missing,
            "installed_at": self.installed_at,
            "backfill_hint": self.backfill_hint,
            "ok": self.ok,
            "error": self.error,
            "note": self.note,
            "undecided": self.undecided,
            "install_source": self.install_source,
        }

    def warning(self) -> str | None:
        """One actionable line, or None when there is nothing to say."""
        if self.error:
            return f"{self.adapter_id}: coverage unknown ({self.error})"
        if not self.untraced_recent:
            if self.undecided:
                return None      # an undecided verdict is a note, not a warning
            return None
        oldest = min(t for _, t in self.untraced_recent)
        return (f"{self.adapter_id}: {len(self.untraced_recent)} session(s) "
                f"started after the adapter was installed have NO traces "
                f"(oldest {oldest}). The agent usually loads adapters at "
                f"process start, so a running process that predates the "
                f"install keeps writing nothing until it restarts."
                + (f" Recover with: {self.backfill_hint}" if self.backfill_hint else ""))


def _agent_dir(env: str, default: Path) -> Path:
    return Path(os.environ.get(env) or default).expanduser()


def sources() -> list[SessionSource]:
    home = Path.home()
    pi_agent = _agent_dir("PI_CODING_AGENT_DIR", home / ".pi" / "agent")
    codex_home = _agent_dir("CODEX_HOME", home / ".codex")
    hermes_home = _agent_dir("HERMES_HOME", home / ".hermes")
    return [
        SessionSource(
            adapter_id="pi",
            label="pi session transcripts",
            root=Path(os.environ.get("AGENTTRACE_PI_SESSIONS")
                      or pi_agent / "sessions"),
            pattern="*.jsonl",
            key="path",
            # `pi install` writes the package list here, so its mtime is when
            # this adapter became loadable — exactly the line the "recent"
            # comparison needs.
            install_marker=pi_agent / "settings.json",
            backfill_hint="python3 pi/pi_import.py",
        ),
        SessionSource(
            adapter_id="codex",
            label="codex rollout files",
            root=codex_home / "sessions",
            pattern="rollout-*.jsonl",
            key="prefix",          # session id lives inside the file (session_meta)
            install_marker=codex_home / "hooks.json",
            backfill_hint="python3 codex/codex_import.py <rollout.jsonl>",
        ),
        SessionSource(
            adapter_id="hermes",
            label="hermes sessions (state.db)",
            kind="sqlite",
            db=hermes_home / "state.db",
            # Only sessions that actually called a model: a session with zero
            # API calls has nothing a trace could contain, and counting it as
            # "missing" would be a false alarm.
            query="SELECT id, started_at FROM sessions"
                  " WHERE COALESCE(api_call_count, 0) > 0",
            time_is_epoch=True,
            install_marker=hermes_home / "plugins" / "agent-trace" / "__init__.py",
            backfill_hint="(no backfill yet: hermes keeps no transcript of the "
                          "wire request, only the session's own state)",
        ),
    ]


def _inventory(source: SessionSource) -> tuple[list[tuple[str, str, float]], str | None]:
    """(session rows, error) for one source. One row per session, oldest first."""
    if source.kind == "sqlite":
        return _sqlite_inventory(source)
    return _file_inventory(source), None


def _sqlite_inventory(source: SessionSource) -> tuple[list[tuple[str, str, float]], str | None]:
    import sqlite3

    if source.db is None or not source.db.exists():
        return [], None                      # adapter not installed; nothing to say
    try:
        conn = sqlite3.connect(f"file:{source.db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return [], f"cannot open {source.db.name} read-only: {exc}"
    try:
        rows = conn.execute(source.query).fetchall()
    except sqlite3.Error as exc:
        # A schema this check does not know is not a crash: report it as
        # unknown rather than guessing at a different query.
        return [], f"{source.db.name}: {exc}"
    finally:
        conn.close()
    out: list[tuple[str, str, float]] = []
    for key, when in rows:
        mtime = float(when or 0)
        out.append((str(key), _iso(mtime), mtime))
    out.sort(key=lambda r: r[2])
    return out, None


#: Pi names a session file after when the session started; Codex names a rollout
#: the same way. Reading it beats a file mtime, which a copy or a sync moves.
_NAME_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3})Z")


def _started_from_name(path: Path) -> float | None:
    """The session's start time, from the artefact's own name."""
    match = _NAME_TS_RE.search(path.name)
    if not match:
        return None
    try:
        stamp = datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S-%f")
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc).timestamp()


def _file_inventory(source: SessionSource) -> list[tuple[str, str, float]]:
    """One row per session, earliest artefact wins.

    Hermes repeats a file per request and Codex keeps one rollout per session;
    either way a file-level inventory would count a session once per call it
    made, so the earliest timestamp is the session's start.
    """
    earliest: dict[str, float] = {}
    if source.root is not None and source.root.is_dir():
        for path in sorted(source.root.rglob(source.pattern)):
            key = (_codex_session_id(path) if source.adapter_id == "codex"
                   else _key_for(source, path))
            if not key or not _has_content(path):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            # Prefer the session's own start time: a copied, synced or restored
            # file carries a fresh mtime, which made an old session look recent
            # and produced a warning nobody could act on.
            started = _started_from_name(path) or mtime
            if key not in earliest or started < earliest[key]:
                earliest[key] = started
    return [(key, _iso(mtime), mtime) for key, mtime in sorted(earliest.items())]


def _has_content(path: Path, limit: int = 4_000_000) -> bool:
    """Does this artefact contain an actual conversation?

    A Pi session file can hold a header and nothing else. Counting it as a
    session produces a warning that can never be cleared, because there is
    nothing for the importer to recover.
    """
    if path.suffix == ".json":
        return True                    # hermes dumps; not used as a source now
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"type"' not in line:
                    continue
                if '"message"' in line or '"response_item"' in line \
                        or '"event_msg"' in line:
                    return True
    except OSError:
        return False
    return False


def _canon_path(value: str) -> str:
    try:
        return os.path.realpath(value)
    except OSError:
        return value


def _canon_set(values: Iterable[str]) -> set[str]:
    return {_canon_path(v) for v in values}


def _key_for(source: SessionSource, path: Path) -> str | None:
    if source.key == "path":
        return str(path)
    if source.key == "stem":
        return path.stem
    if source.key == "prefix":
        # `request_dump_20260612_153025_a7aceb_20260612_155122_149341` ->
        # `20260612_153025_a7aceb`. Trailing dump timestamps are dropped by
        # taking the date_time_hash group.
        name = path.stem
        parts = name.split("_")
        for prefix in ("request_dump", "rollout"):
            if parts and parts[0] == prefix:
                parts = parts[1:]
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            return "_".join(parts[:3]).replace("-", "_")
        return path.stem
    return None


def _codex_session_id(path: Path) -> str | None:
    """Codex session ids live in the file's first session_meta line."""
    import json
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "session_meta":
                    sid = (obj.get("payload") or {}).get("id")
                    return str(sid) if sid else None
    except OSError:
        return None
    return None


_SESSION_ID_RE = re.compile(r'"session_id"\s*:\s*"((?:[^"\\]|\\.)*)"')
_AGENT_RE = re.compile(r'"agent"\s*:\s*"([^"]+)"')


def scan_trace_files(trace_dirs: list[Path]) -> dict[str, set[str]]:
    """{adapter_id: session ids} from the raw JSONL, in ONE pass, for every
    adapter at once.

    Two properties matter here:
      * one pass, not one per adapter — the trace directory reached 1.1 GB, and
        a per-adapter pass cost 5 s each (enough to time out a dashboard
        request);
      * a regex rather than `json.loads` — these lines carry whole
        conversations, and parsing them all was the bulk of the cost. Only the
        two fields are read, and the escapes are left as written because a
        session id is compared to a path or an agent id, never parsed.

    The files, not the database, remain the fallback: coverage has to work
    before (or without) an index, and must not be silenced by a stale or empty
    one — catching "nothing was captured" is the whole point.
    """
    out: dict[str, set[str]] = {}
    for root in trace_dirs:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.jsonl")):
            try:
                fh = path.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"session_id"' not in line:
                        continue
                    agent = _AGENT_RE.search(line)
                    session = _SESSION_ID_RE.search(line)
                    if agent and session and session.group(1):
                        out.setdefault(agent.group(1), set()).add(session.group(1))
    return out


def store_session_provider(db: Path) -> Callable[[], dict[str, set[str]]]:
    """A traced-session reader backed by the index instead of the raw files.

    The indexer already records (session_id, adapter_id) for every event it
    ingests, so the coverage question — "do we hold traces for this session?" —
    is one indexed SELECT. Reading 1.1 GB of JSONL to answer it was the wrong
    instrument, and it is why the dashboard's adapters page started hanging
    once the trace history grew.
    """
    import sqlite3

    def provider() -> dict[str, set[str]]:
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return {}
        try:
            rows = conn.execute(
                "SELECT adapter_id, session_id FROM sessions"
                " WHERE session_id IS NOT NULL").fetchall()
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        out: dict[str, set[str]] = {}
        for adapter_id, session_id in rows:
            out.setdefault(str(adapter_id), set()).add(str(session_id))
        return out

    return provider


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _install_line(adapter_id: str, marker: Path) -> tuple[float | None, str]:
    """When this adapter's install is deemed to have happened.

    The marker's mtime alone is not enough: reinstalling, or merely touching
    settings.json, moves it forward and reclassifies real gaps as "historical",
    which silently suppresses the warning this whole module exists to raise.
    The earliest line ever observed is therefore persisted and used — the
    install happened at least that long ago, and a later touch cannot erase it.

    The trade-off is stated rather than hidden: on a machine that has never run
    this check before a reinstall, the earliest observation is already the
    reinstall, so a gap from before it reads as historical.
    """
    if not marker.exists():
        return None, "unknown"
    try:
        current = marker.stat().st_mtime
    except OSError:
        return None, "unknown"
    import time
    state = _read_state()
    key = _state_key(adapter_id, marker)
    previous = state.get(key)
    # Bound it: a value in the future is nonsense (a hand-edited or corrupted
    # state file must not be able to suppress the warning for months), and a
    # value AHEAD of the marker is ignored in favour of the marker itself.
    prev = previous if isinstance(previous, (int, float)) else None
    usable = prev is not None and 0 < float(prev) <= min(current, time.time())
    if usable and prev is not None and float(prev) < current:
        # We are looking at a marker that has moved forward since the line was
        # stored — a reinstall or a touch, observed. Only then is "earliest
        # observed" a true description of where this value came from; calling a
        # merely-stored value "earliest observed" overstates it.
        return float(prev), "persisted (a later marker was observed)"
    origin = ("persisted (not corroborated)" if usable else "marker mtime")
    if state.get(key) != current:
        state[key] = current
        _write_state(state)
    return current, origin


def _state_key(adapter_id: str, marker: Path) -> str:
    """The state key for one install marker.

    Keyed by adapter AND marker path: the install line is a property of that
    file, and keying by adapter alone meant a fixture marker (a test, a second
    install) silently rewrote the line used for the real one — which then
    reclassified a year of sessions as "started after the install".
    """
    return f"{adapter_id}|{marker}"


def _state_path() -> Path:
    override = os.environ.get("AGENTTRACE_PLATFORM_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".agenttrace"
    return base / "coverage.json"


def _read_state() -> dict:
    import json
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    import json
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass          # an unwritable cache must not break a diagnostic


#: The dashboard refetches on every SSE change (once a second while an agent is
#: working), and a coverage pass walks every session artefact on disk plus the
#: trace files. A short TTL keeps that off the request path without letting the
#: number go stale in any way a human would notice.
_CACHE: dict[str, tuple[float, list["Coverage"]]] = {}
CACHE_TTL_SECONDS = 30.0


def cached(trace_dirs: list[Path] | None = None,
           traced_provider: Callable[[], dict[str, set[str]]] | None = None
           ) -> list[Coverage]:
    """Cached form for the request path (the dashboard refetches per SSE event).

    ONE slot, deliberately. An earlier version keyed the cache by data source,
    which let the file-backed and index-backed answers sit side by side and —
    for up to a TTL — report different verdicts for the same question on two
    surfaces at the same moment. The source is chosen by the CALLER (index when
    there is one, files otherwise), and the cache caches the answer.
    """
    import time
    now = time.monotonic()
    entry = _CACHE.get("rows")
    if isinstance(entry, tuple) and now - entry[0] < CACHE_TTL_SECONDS:
        return entry[1]
    rows = check(trace_dirs=trace_dirs, traced_provider=traced_provider)
    _CACHE["rows"] = (now, rows)
    return rows


def _clear_cache() -> None:
    _CACHE.clear()


def check(sources_: list[SessionSource] | None = None,
          trace_dirs: list[Path] | None = None,
          traced_provider: Callable[[], dict[str, set[str]]] | None = None
          ) -> list[Coverage]:
    """Compare each adapter's session inventory against its traces.

    `traced_provider` returns {adapter_id: session ids}; the server passes the
    index-backed one, and the default reads the trace files in a single pass.
    """
    if trace_dirs is None:
        from core.storage.discovery import default_trace_dirs
        trace_dirs = default_trace_dirs()

    index_empty = False
    if traced_provider is not None:
        traced_by_adapter = traced_provider()
        # An index that knows no sessions at all is not evidence that nothing
        # was captured — it is an index that has not been built yet (a fresh
        # start, or a rebuild in progress). Reporting "not capturing" there is a
        # false alarm, and false alarms are how a warning stops being read.
        index_empty = not traced_by_adapter
    else:
        traced_by_adapter = scan_trace_files(trace_dirs)

    out: list[Coverage] = []
    for source in sources_ or sources():
        rows, inv_error = _inventory(source)

        cov = Coverage(adapter_id=source.adapter_id, label=source.label,
                       source=str(source.db if source.kind == "sqlite"
                                  else source.root),
                       backfill_hint=source.backfill_hint)
        if not rows and not inv_error:
            # Silence here would be indistinguishable from a clean bill of
            # health, which is the one answer this must never give by accident.
            source_path = source.db if source.kind == "sqlite" else source.root
            cov.note = (f"no session artefacts at {source_path}"
                        + ("" if source_path is not None and source_path.exists()
                           else " (path does not exist)"))
        if inv_error:
            cov.error = inv_error
            out.append(cov)
            continue
        if index_empty and source.kind != "sqlite":
            # Count the inventory anyway: reporting 0 known sessions here hid a
            # real inventory behind a transient condition. The verdict is what
            # is withheld, not the data.
            cov.sessions_known = len(rows)
            cov.undecided = True
            cov.note = (f"the index holds no sessions yet (it may still be "
                        f"building) — {len(rows)} session artefact(s) found but "
                        "not judged; this is not evidence that nothing was "
                        "captured")
            out.append(cov)
            continue
        traced = traced_by_adapter.get(source.adapter_id, set())
        cov.sessions_known = len(rows)

        installed_mtime: float | None = None
        if source.install_marker:
            installed_mtime, origin = _install_line(source.adapter_id,
                                                   source.install_marker)
            if installed_mtime is not None:
                cov.installed_at = _iso(installed_mtime)
                if origin != "persisted (a later marker was observed)":
                    # Without a persisted line the comparison rests on an mtime a
                    # reinstall can move (or a state file that is missing or
                    # corrupt). Say so rather than presenting it as equivalent.
                    cov.note = (cov.note or
                                f"install line from {origin}; reinstalling can "
                                "reclassify earlier gaps")
                cov.install_source = origin

        for key, started, mtime in rows:
            # EXACT match only. A trace names a pi session by its file path and
            # a codex/hermes session by the agent's own id, so the comparison is
            # exact by construction; a fuzzy match would hide the gap this
            # exists to find.
            # Canonicalised for path-keyed sources, like the importer does: a
            # symlinked sessions root (or /var vs /private/var on macOS) would
            # otherwise report a gap for a session whose trace exists.
            matched = (_canon_path(key) in _canon_set(traced)
                       if source.key == "path" else key in traced)
            if matched:
                cov.sessions_covered += 1
                continue
            if installed_mtime is None:
                cov.untraced_unknown_age += 1
            elif mtime < installed_mtime:
                cov.untraced_historical += 1
            else:
                cov.untraced_recent.append((Path(key).name, started))
        cov.sessions_traced_only = len(traced) - cov.sessions_covered
        # Oldest first: a warning should lead with how long this has been true.
        cov.untraced_recent.sort(key=lambda r: r[1])
        out.append(cov)
    return out


def warnings(covs: list[Coverage] | None = None) -> list[str]:
    return [w for w in (c.warning() for c in (covs or cached())) if w]
