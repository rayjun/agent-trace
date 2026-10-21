"""agenttrace doctor — one command that answers "why is nothing showing up?".

The failure this exists for is not a crash. It is the quiet one: the adapter
is installed but not loaded, the deployed plugin is a stale copy, the index is
behind the files, the disk is full so every append returns None, or the agent
writes traces into a directory the reader does not scan. Each of those looks
identical from the outside — an empty list — and each has a different fix.

So the checks are ordered by how far a problem is from the user's question:

    trace dirs   → is there anywhere to write, and can we write to it?
    disk         → is there room?
    capture      → has anything landed recently, and is the file intact?
    schema       → which record version are those records?
    ingest faults→ has the capture path been DROPPING events?
    index        → is the queryable projection present, healthy, current?
    adapters     → is the deployed adapter the one in this repo?
    coverage     → did an agent run sessions we have no traces for?

`status` is deliberately four-valued: `ok`, `warn` (a real defect), `fail`
(capture cannot work at all) and `info` (nothing wrong, but the answer is
"not installed / not built yet"). `info` never fails the command — a user who
only installed the Codex hook should not get a red exit for not having Pi.

Exit code: 0 when every check is ok/info, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.storage.discovery import iter_trace_files
from core import ingest_faults

#: How many trailing lines of the newest file to inspect. The check is about
#: "did the last write survive", not about auditing history — the indexer owns
#: the full scan and its quarantine counts.
SAMPLE_LINES = 2000
#: A day without a single record is a defect on a machine whose agent runs.
STALE_CAPTURE_H = 24.0
#: Below this, the next append is likely to be the one that fails.
MIN_FREE_BYTES = 1 << 30
#: A span still `running` after this long did not finish; the agent died.
ABANDONED_SPAN_H = 1.0


@dataclass
class Check:
    name: str
    status: str          # ok | warn | fail | info
    summary: str
    detail: str = ""
    #: One actionable line, shown under "Warnings" when the status is not ok.
    advice: str = ""


@dataclass
class Report:
    version: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(c.status in ("warn", "fail") for c in self.checks)

    @property
    def warnings(self) -> list[str]:
        # `info` is an answer, not a defect ("not installed", "not built yet").
        # Its advice would read as a problem the user must fix, so it is not
        # listed here.
        return [c.advice for c in self.checks
                if c.advice and c.status in ("warn", "fail")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "ok": self.ok,
            "generated_at": _now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "faults_path": str(ingest_faults.path()),
            "checks": [
                {"name": c.name, "status": c.status, "summary": c.summary,
                 "detail": c.detail, "advice": c.advice}
                for c in self.checks
            ],
            "warnings": self.warnings,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _version() -> str:
    try:
        return (_repo_root() / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def human_age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.0f} h"
    return f"{seconds / 86400:.0f} d"


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------

def _probe_writable(directory: Path) -> tuple[bool, str]:
    """Actually write, don't trust os.access.

    os.access answers for the *real* uid; a sandbox, a read-only mount or a
    full filesystem all return "writable" and then fail on write. The probe is
    the only honest answer, and it is one empty file.
    """
    probe = directory / f".agenttrace-doctor-{os.getpid()}.tmp"
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _trace_dirs_check(dirs: list[Path], files: list[Path]) -> Check:
    if not dirs:
        return Check(
            "Trace dirs", "fail", "none of the known trace directories exists",
            detail="; ".join(str(d) for d in dirs) or "(none discovered)",
            advice="start an agent once, or pass a directory: agenttrace doctor <dir>")
    total = 0
    newest = 0.0
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        total += st.st_size
        newest = max(newest, st.st_mtime)
    unwritable: list[tuple[Path, str]] = []
    for d in dirs:
        ok, why = _probe_writable(d)
        if not ok:
            unwritable.append((d, why))
    summary = (f"{len(dirs)} dir(s), {len(files)} file(s), {human_bytes(total)}"
               + (f", newest {human_age(max(0.0, _now().timestamp() - newest))} ago"
                  if newest else ""))
    if unwritable:
        first, why = unwritable[0]
        return Check("Trace dirs", "fail", summary,
                     detail=f"{first}: {why}",
                     advice=f"{first} is not writable ({why}) — every event written "
                            "there is being dropped")
    return Check("Trace dirs", "ok", summary)


def _disk_check(dirs: list[Path]) -> Check:
    if not dirs:
        return Check("Disk", "info", "no trace directory to measure")
    usage = []
    for d in dirs:
        try:
            usage.append((d, shutil.disk_usage(d)))
        except OSError:
            continue
    if not usage:
        return Check("Disk", "info", "free space unavailable")
    tightest, used = min(usage, key=lambda pair: pair[1].free)
    summary = f"{human_bytes(used.free)} free on {tightest}"
    if used.free < MIN_FREE_BYTES:
        return Check("Disk", "warn", summary,
                     advice=f"only {human_bytes(used.free)} free on the filesystem "
                            f"holding {tightest}; appends will start failing")
    return Check("Disk", "ok", summary)


def _tail(path: Path, max_lines: int = SAMPLE_LINES) -> tuple[list[dict], int]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            # deque(maxlen) keeps the window bounded: the newest trace file is
            # 35 MB today and will not stay that small, and a diagnostic must
            # not read a file into memory to look at its end.
            lines = list(deque(fh, maxlen=max_lines))
    except OSError:
        return [], 0
    recs: list[dict] = []
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(rec, dict):
            recs.append(rec)
    return recs, bad


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _capture_check(files: list[Path]) -> tuple[Check, set[Any]]:
    if not files:
        return (Check("Capture", "warn", "no trace records on disk yet",
                      advice="run the agent once — adapters load at process start, "
                             "so a session already running when they were installed "
                             "writes nothing"), set())
    newest = max(files, key=_mtime)
    recs, bad = _tail(newest)
    versions = {r.get("v") for r in recs}
    last_ts = None
    last_agent = None
    for rec in reversed(recs):
        if isinstance(rec.get("ts"), str):
            # v1 records carry `agent`, v2 carry `adapter_id`; a file can hold
            # both during a migration.
            last_ts = rec["ts"]
            last_agent = rec.get("agent") or rec.get("adapter_id")
            break
    age_s = None
    if last_ts:
        try:
            when = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc)
            age_s = (_now() - when).total_seconds()
        except ValueError:
            age_s = None
    if age_s is None:
        summary = f"{newest.name}: no timestamped record in the last {len(recs)} lines"
    else:
        summary = f"last event {human_age(age_s)} ago ({last_agent}) in {newest.name}"

    problems = []
    if bad:
        problems.append(
            f"{bad} unparseable line(s) in the last {SAMPLE_LINES} lines of "
            f"{newest.name} — a crash mid-write or a redactor that consumed a "
            "JSON escape leaves one; everything before it is still readable")
    if age_s is not None and age_s > STALE_CAPTURE_H * 3600:
        problems.append(f"no events for {human_age(age_s)}")
    if problems:
        return (Check("Capture", "warn", summary, detail=problems[0],
                      advice=problems[0]), versions)
    return Check("Capture", "ok", summary), versions


def _schema_check(versions: set[Any]) -> Check:
    known = {v for v in versions if v in (1, 2)}
    unknown = versions - known
    if not versions:
        return Check("Schema", "info", "no records to judge")
    if unknown:
        return Check("Schema", "warn", f"unknown record version(s): {sorted(map(str, unknown))}",
                     advice="records carry a schema version this build does not read — "
                            "upgrade agent-trace, or they are not trace records")
    if 1 in known and 2 in known:
        return Check("Schema", "ok", "v1 and v2 records (v1 read through the converter)")
    if 1 in known:
        return Check("Schema", "ok", "v1 records (read as v2 through the converter)")
    return Check("Schema", "ok", "v2 canonical events")


def _faults_check() -> Check:
    if not ingest_faults.enabled():
        return Check("Ingest faults", "info", "fault log disabled (AGENTTRACE_FAULTS=0)")
    groups = ingest_faults.summarize()
    if not groups:
        return Check("Ingest faults", "ok", "none")
    lines = sum(int(g["lines"]) for g in groups)
    newest = groups[0]
    try:
        when = datetime.strptime(str(newest["last_ts"]), "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        age = human_age(max(0.0, (_now() - when).total_seconds()))
    except ValueError:
        age = "?"
    summary = (f"{lines} fault line(s), {len(groups)} adapter/kind pair(s), "
               f"newest {age} ago")
    advice = (f"{newest['adapter_id']}/{newest['kind']}: {newest['last_error']}"
              f" (last {newest['last_ts']})")
    return Check("Ingest faults", "warn", summary,
                 detail=str(ingest_faults.path()), advice=advice)


def _index_check(db_path: Path | None, files: list[Path]) -> Check:
    if db_path is None or not Path(db_path).exists():
        return Check(
            "Index", "info", "not built yet — the CLI is scanning files directly",
            detail=f"build it with: agenttrace index ({db_path or 'no path'})")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return Check("Index", "fail", "cannot open the index",
                     detail=str(exc),
                     advice=f"the index at {db_path} cannot be opened ({exc}); "
                            "rebuild it with: agenttrace index --full")
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()
        quick_ok = bool(quick) and quick[0] == "ok"
        schema = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        schema_version = int(schema[0]) if schema else 0
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        spans = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
        indexed = {str(r[0]): (int(r[1]), int(r[2])) for r in conn.execute(
            "SELECT path, size, indexed_bytes FROM ingest_files")}
        corrupt = conn.execute(
            "SELECT COALESCE(SUM(corrupt_lines), 0) FROM ingest_files").fetchone()[0]
        cutoff = _iso(_now() - timedelta(hours=ABANDONED_SPAN_H))
        open_spans = conn.execute(
            "SELECT COUNT(*) FROM spans WHERE status='running' AND start_ts < ?",
            (cutoff,)).fetchone()[0]
    except sqlite3.Error as exc:
        conn.close()
        return Check("Index", "fail", "the index is unreadable",
                     detail=str(exc),
                     advice=f"{db_path}: {exc} — rebuild with: agenttrace index --full")
    conn.close()

    pending = 0
    for path in files:
        # The indexer keys `ingest_files.path` by str(path), not by inode or
        # realpath — matching that exactly is what makes "behind" mean
        # "newer than its indexed offset" instead of "unknown".
        entry = indexed.get(str(path))
        if entry is None:
            pending += 1
            continue
        try:
            if path.stat().st_size > entry[1]:
                pending += 1
        except OSError:
            continue

    from core.storage.sqlite_store import DB_SCHEMA_VERSION

    problems = []
    if not quick_ok:
        problems.append(f"quick_check reported: {quick[0] if quick else 'nothing'}")
    if schema_version != DB_SCHEMA_VERSION:
        problems.append(f"schema_version {schema_version} != {DB_SCHEMA_VERSION}")
    if corrupt:
        problems.append(f"{corrupt} unparseable line(s) quarantined by the indexer")
    if pending:
        problems.append(f"{pending} trace file(s) not indexed yet")
    if open_spans:
        problems.append(f"{open_spans} span(s) never closed (shown as incomplete)")

    summary = f"{events:,} events, {spans:,} spans"
    if pending:
        summary += f", {pending} file(s) behind"
    if problems:
        return Check("Index", "warn", summary, detail=problems[0],
                     advice="; ".join(problems) + " — run: agenttrace index")
    return Check("Index", "ok", summary)


def _adapters_check(home: Path, repo: Path) -> Check:
    from adapter_sdk import compatibility, discovery

    entries = discovery.discover()
    verified = 0
    for entry in entries:
        compat = compatibility.for_adapter(entry.adapter_id)
        if compat is not None and compat.status == "verified":
            verified += 1
    states: list[str] = []
    warnings: list[str] = []

    deployed = home / ".hermes" / "plugins" / "agent-trace"
    if deployed.is_dir():
        stale = []
        comparable = 0
        for src, name in ((repo / "hermes" / "__init__.py", "__init__.py"),
                          (repo / "hermes" / "plugin.yaml", "plugin.yaml"),
                          (repo / "common" / "agenttrace_common.py",
                           "agenttrace_common.py")):
            dest = deployed / name
            if not src.exists():
                continue          # a packaged install has no checkout to compare
            comparable += 1
            try:
                if not dest.exists():
                    stale.append(f"missing {name}")
                elif src.read_bytes() != dest.read_bytes():
                    stale.append(f"stale {name}")
            except OSError:
                stale.append(f"unreadable {name}")
        if stale:
            states.append("hermes STALE")
            warnings.append(
                f"the deployed hermes plugin does not match this checkout "
                f"({', '.join(stale)}) — run: bash install.sh hermes default")
        elif comparable:
            states.append("hermes deployed")
        else:
            states.append("hermes deployed (source not in this install)")
    else:
        states.append("hermes not deployed")

    codex_hook = home / ".codex" / "hooks.json"
    if codex_hook.exists():
        try:
            wired = "codex_import.py" in codex_hook.read_text(encoding="utf-8")
        except OSError:
            wired = False
        states.append("codex wired" if wired else "codex hook not wired")
    else:
        states.append("codex not installed")

    pi_settings = home / ".pi" / "agent" / "settings.json"
    if pi_settings.exists():
        try:
            registered = "agent-trace" in pi_settings.read_text(encoding="utf-8")
        except OSError:
            registered = False
        states.append("pi registered" if registered else "pi not registered")
    else:
        states.append("pi not installed")

    summary = f"{len(entries)} known ({verified} verified by test); " + ", ".join(states)
    if warnings:
        return Check("Adapters", "warn", summary, advice=warnings[0])
    return Check("Adapters", "ok", summary)


def _coverage_check() -> Check:
    try:
        from core import coverage as cov_mod
        rows = cov_mod.cached()
    except Exception as exc:  # noqa: BLE001 - an optional check must not fail doctor
        return Check("Coverage", "info", f"check unavailable: {type(exc).__name__}")
    warnings = [w for w in (r.warning() for r in rows) if w]
    if warnings:
        return Check("Coverage", "warn",
                     f"{len(warnings)} adapter(s) with recent capture gaps",
                     advice=warnings[0])
    return Check("Coverage", "ok",
                 f"{len(rows)} adapter source(s), no recent gaps")


# --------------------------------------------------------------------------

def run(dirs: list[Path], db_path: Path | None = None, fast: bool = False,
        home: Path | None = None) -> Report:
    """Run every check. `home` is injectable so tests never touch the real one."""
    home = Path(home).expanduser() if home else Path.home()
    dirs = [Path(d).expanduser() for d in dirs]
    existing = [d for d in dirs if d.is_dir()]
    files = list(iter_trace_files(existing)) if existing else []

    report = Report(version=_version())
    report.checks.append(_trace_dirs_check(existing, files))
    report.checks.append(_disk_check(existing))
    capture, versions = _capture_check(files)
    report.checks.append(capture)
    report.checks.append(_schema_check(versions))
    report.checks.append(_faults_check())
    report.checks.append(_index_check(db_path, files))
    report.checks.append(_adapters_check(home, _repo_root()))
    if not fast:
        report.checks.append(_coverage_check())
    return report


def render(report: Report, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(report.to_dict(), indent=2)
    width = max(len(c.name) for c in report.checks) + 2
    lines = [f"agenttrace doctor {report.version}", ""]
    for c in report.checks:
        lines.append(f"  {c.name:<{width}}{c.status:<7}{c.summary}")
        if c.detail and c.status != "ok":
            lines.append(f"  {'':<{width}}{'':<7}{c.detail}")
    if report.warnings:
        lines += ["", "Warnings:"]
        lines += [f"  - {w}" for w in report.warnings]
    else:
        lines += ["", "No problems found."]
    return "\n".join(lines)
