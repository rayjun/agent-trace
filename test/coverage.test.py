#!/usr/bin/env python3
"""Coverage: is an installed adapter actually capturing?

"Installed" and "capturing" are different facts, and only the first one was
ever visible. The Pi adapter is a live hook loaded at PROCESS START, so every
session that began before it was registered is invisible no matter how healthy
the registry looks — 53 of 63 sessions on the machine that motivated this, found
by eye from an empty dashboard.

What is pinned here:
  * a session AFTER the adapter's install with no traces is a RECENT gap and
    raises a warning; sessions before it are historical and must not, because a
    warning that fires on every fresh install is a warning nobody reads;
  * the inventory is per SESSION, not per file (Hermes writes one artefact per
    request);
  * the comparison is exact, and a source that cannot be read reports "unknown"
    rather than a false all-clear;
  * callers can read it through the cached path the dashboard uses.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import coverage as cov  # noqa: E402

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="coverage-"))
# Before ANY check(): the persisted install line lives in the platform dir, and
# an earlier version of this test wrote its fixture's fake install time into the
# real ~/.agenttrace/coverage.json — which then reclassified a year of the
# user's sessions as "started after the install".
os.environ["AGENTTRACE_PLATFORM_DIR"] = str(tmp / "platform")
sessions = tmp / "pi-sessions"
traces = tmp / "traces"
sessions.mkdir(), traces.mkdir()

# three sessions: one before the install, two after; only the middle one traced
old = sessions / "2026-01-01T00-00-00-000Z_old.jsonl"
new_a = sessions / "2026-06-02T00-00-00-000Z_traced.jsonl"
new_b = sessions / "2026-06-03T00-00-00-000Z_untraced.jsonl"
# Each is a real transcript: a header AND at least one message entry. A
# header-only file is NOT a session (there is nothing to trace in it, so
# counting it raises a warning that can never be cleared).
def session_file(started: str, text: str) -> str:
    return "\n".join([
        json.dumps({"type": "session", "version": 3, "id": started,
                    "timestamp": f"{started}T00:00:00.000Z", "cwd": "/tmp"}),
        json.dumps({"type": "message", "id": "m1", "parentId": None,
                    "timestamp": f"{started}T00:00:01.000Z",
                    "message": {"role": "user",
                                "content": [{"type": "text", "text": text}]}}),
    ]) + "\n"


for path, day in ((old, "2026-01-01"), (new_a, "2026-06-02"), (new_b, "2026-06-03")):
    path.write_text(session_file(day, day), encoding="utf-8")

marker = tmp / "settings.json"
marker.write_text("{}", encoding="utf-8")
# the install happened between `old` and the other two
install_time = time.mktime(time.strptime("2026-03-01", "%Y-%m-%d"))
os.utime(old, (install_time - 86400, install_time - 86400))
os.utime(marker, (install_time, install_time))
os.utime(new_a, (install_time + 86400, install_time + 86400))
os.utime(new_b, (install_time + 2 * 86400, install_time + 2 * 86400))

# the live adapter wrote records for new_a only
(traces / "pi-x.jsonl").write_text("\n".join([
    json.dumps({"v": 1, "ts": "2026-06-02T00:00:01.000Z", "agent": "pi",
                "event": "llm_request", "session_id": str(new_a)}),
    # a compact line, the way the TypeScript adapter writes it: the reader must
    # not depend on JSON spacing
    '{"v":1,"ts":"2026-06-02T00:00:02.000Z","agent":"pi","event":"llm_response",'
    f'"session_id":"{new_a}"}}',
]) + "\n", encoding="utf-8")

source = cov.SessionSource(adapter_id="pi", label="pi", root=sessions,
                           key="path", install_marker=marker)
rows = cov.check([source], trace_dirs=[traces])
check("one result per source", len(rows) == 1)
c = rows[0]
check("all three session files are in the inventory", c.sessions_known == 3,
      str(c.sessions_known))
check("the traced session counts as covered", c.sessions_covered == 1,
      str(c.sessions_covered))
check("only the session AFTER the install is a recent gap",
      len(c.untraced_recent) == 1 and c.untraced_recent[0][0] == new_b.name,
      str(c.untraced_recent))
check("the session BEFORE the install is historical",
      c.untraced_historical == 1, str(c.untraced_historical))
check("a recent gap makes the adapter not-ok", c.ok is False)
check("the warning names the adapter, the count and the fix",
      c.warning() and "5" not in (c.warning() or "")
      and "pi" in (c.warning() or ""), str(c.warning()))

# the install marker is what splits the two, so removing it changes the verdict
c_nomarker = cov.check([cov.SessionSource(adapter_id="pi", label="pi",
                                          root=sessions, key="path")],
                       trace_dirs=[traces])[0]
# Without a marker the age is unknowable, and "we cannot tell" must not be
# reported as "broken now": doing that would make every fresh install warn
# about its own history, and the warning would be skimmed past exactly when it
# matters.
# a header-only file is not a session
header_only = sessions / "2026-06-04T00-00-00-000Z_headeronly.jsonl"
header_only.write_text(json.dumps({"type": "session", "version": 3,
                                   "id": "h", "timestamp": "2026-06-04T00:00:00.000Z"}) + "\n",
                       encoding="utf-8")
os.utime(header_only, (install_time + 3 * 86400, install_time + 3 * 86400))
c_header = cov.check([source], trace_dirs=[traces])[0]
check("a header-only transcript is not counted as a missing session",
      c_header.sessions_known == 3 and len(c_header.untraced_recent) == 1
      and not any("headeronly" in n for n, _ in c_header.untraced_recent),
      f"known={c_header.sessions_known} recent={[n[:24] for n, _ in c_header.untraced_recent]}")

# ...and a source that yields nothing says so instead of a silent all-clear
empty_dir = tmp / "empty-sessions"
empty_dir.mkdir()
c_empty = cov.check([cov.SessionSource(adapter_id="pi", label="pi",
                                       root=empty_dir, key="path")],
                    trace_dirs=[traces])[0]
check("the persisted install line is keyed by the marker it belongs to",
      all("|" in k for k in cov._read_state()), str(list(cov._read_state())))

check("a source with no artefacts reports a note, not a silent 'ok'",
      c_empty.sessions_known == 0 and c_empty.note and c_empty.ok,
      f"note={c_empty.note!r}")

# the install line must not move forward when the marker is merely touched
marker.write_text("{} ", encoding="utf-8")   # mtime moves forward now
os.utime(marker, (install_time + 10 * 86400, install_time + 10 * 86400))
c_touched = cov.check([source], trace_dirs=[traces])[0]
check("touching the install marker cannot reclassify a real gap as historical",
      len(c_touched.untraced_recent) == 1, f"recent={len(c_touched.untraced_recent)}")
# ...but a hand-edited state file pointing into the future must not suppress it
import json as _json  # noqa: E402
poison = tmp / "platform" / "coverage.json"
poison.parent.mkdir(parents=True, exist_ok=True)
poison.write_text(_json.dumps({"pi|" + str(marker): time.time() + 86400 * 365}),
                  encoding="utf-8")
c_poison = cov.check([source], trace_dirs=[traces])[0]
check("a poisoned future install line cannot suppress a real gap",
      len(c_poison.untraced_recent) == 1, f"recent={len(c_poison.untraced_recent)}")
poison.write_text("{ not json", encoding="utf-8")
c_corrupt = cov.check([source], trace_dirs=[traces])[0]
check("a corrupt state file degrades visibly instead of silently",
      len(c_corrupt.untraced_recent) == 1
      and "marker mtime" in (c_corrupt.note or "")
      and c_corrupt.install_source == "marker mtime",
      f"recent={len(c_corrupt.untraced_recent)} note={c_corrupt.note!r} "
      f"src={c_corrupt.install_source!r}")
# a line that was merely stored is not described as "observed": the metadata
# must not overstate where the value came from
check("a stored-but-uncorroborated line says so",
      c_corrupt.install_source != "persisted (a later marker was observed)")
poison.unlink()
os.utime(marker, (install_time, install_time))

check("without an install marker nothing is 'recent' (no false alarm)",
      not c_nomarker.untraced_recent
      and c_nomarker.untraced_unknown_age == 2
      and c_nomarker.untraced_historical == 0,
      f"recent={len(c_nomarker.untraced_recent)} "
      f"unknown={c_nomarker.untraced_unknown_age} "
      f"historical={c_nomarker.untraced_historical}")
check("...so it does not warn", c_nomarker.warning() is None)

# a COPIED old session keeps its own start time (from the file name), so a
# fresh mtime cannot make it look like a recent gap
copy = sessions / "2026-01-02T00-00-00-000Z_copied.jsonl"
copy.write_text(session_file("2026-01-02", "copied"), encoding="utf-8")
now = time.time()
os.utime(copy, (now, now))          # written just now, but it is an old session
c_copy = cov.check([source], trace_dirs=[traces])[0]
check("a copied old session is judged by its own start, not its mtime",
      not any("copied" in n for n, _ in c_copy.untraced_recent)
      and c_copy.untraced_historical >= 1,
      f"recent={[n[:20] for n, _ in c_copy.untraced_recent]}")
copy.unlink()

# exact matching: a trace for a DIFFERENT session must not cover anything
(traces / "pi-other.jsonl").write_text(json.dumps(
    {"v": 1, "ts": "2026-06-03T00:00:01.000Z", "agent": "pi",
     "event": "llm_request", "session_id": "/somewhere/else.jsonl"}) + "\n",
    encoding="utf-8")
c2 = cov.check([source], trace_dirs=[traces])[0]
check("a trace for another session does not count as coverage",
      c2.sessions_covered == 1 and len(c2.untraced_recent) == 1,
      f"covered={c2.sessions_covered} recent={len(c2.untraced_recent)}")
check("a traced session with no artefact is reported separately",
      c2.sessions_traced_only == 1, str(c2.sessions_traced_only))

# ---- sqlite sources (hermes keeps sessions as rows) ------------------------
db = tmp / "state.db"
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, api_call_count INT)")
conn.executemany("INSERT INTO sessions VALUES (?,?,?)", [
    ("20260101_000000_aaaaaa", install_time - 86400, 3),
    ("20260610_000000_bbbbbb", install_time + 86400, 7),
    ("20260611_000000_cccccc", install_time + 2 * 86400, 0),   # no API calls
])
conn.commit()
conn.close()
(traces / "hermes-x.jsonl").write_text(json.dumps(
    {"v": 1, "ts": "2026-06-10T00:00:01.000Z", "agent": "hermes",
     "event": "llm_request", "session_id": "20260610_000000_bbbbbb"}) + "\n",
    encoding="utf-8")
sql_source = cov.SessionSource(
    adapter_id="hermes", label="hermes", kind="sqlite", db=db,
    query="SELECT id, started_at FROM sessions WHERE COALESCE(api_call_count,0) > 0",
    time_is_epoch=True, install_marker=marker)
rows = cov.check([sql_source], trace_dirs=[traces])
c3 = rows[0]
check("a sqlite source inventories sessions, not files", c3.sessions_known == 2,
      str(c3.sessions_known))
check("a session with no API calls is not counted as missing",
      all("cccccc" not in s for s, _ in c3.untraced_recent) and c3.missing == 1,
      str(c3.missing))
check("the traced sqlite session is covered", c3.sessions_covered == 1)
check("a pre-install sqlite session is historical",
      c3.untraced_historical == 1 and not c3.untraced_recent)
check("sqlite coverage does not warn", c3.warning() is None)

# an unreadable/absent database is 'unknown', never a false all-clear
missing_db = cov.SessionSource(adapter_id="hermes", label="hermes",
                               kind="sqlite", db=tmp / "nope.db",
                               query="SELECT id, started_at FROM sessions")
check("a missing database is not an error (adapter simply not installed)",
      cov.check([missing_db], trace_dirs=[traces])[0].error is None)
broken = cov.SessionSource(adapter_id="hermes", label="hermes", kind="sqlite",
                           db=db, query="SELECT id, started_at FROM nonexistent")
c4 = cov.check([broken], trace_dirs=[traces])[0]
# Unknown is NOT a defect: conflating the two makes a transient condition (a
# locked database, an index still building) read as "the adapter is broken",
# and a warning that fires on that is one nobody reads.
check("a schema this check does not know is reported as unknown, not a crash",
      c4.error is not None and "nonexistent" in (c4.error or ""), str(c4.error))
check("...and 'unknown' does not count as a known defect (no exit-code flap)",
      c4.ok is True and c4.warning() is not None
      and "coverage unknown" in (c4.warning() or ""), str(c4.warning()))

# an index that knows no sessions is "not ready", never "not captured"
def empty_provider():
    return {}


c5 = cov.check([source], trace_dirs=[traces], traced_provider=empty_provider)[0]
check("an empty index reports 'not ready' instead of a false 'not capturing'",
      c5.ok is True and not c5.untraced_recent and c5.undecided
      and "still be building" in (c5.note or ""),
      f"recent={len(c5.untraced_recent)} undecided={c5.undecided} note={c5.note!r}")
# ...and it must still report the inventory it read (a real defect used to hide
# behind a transient condition, and the counts were zeroed on that path)
check("an undecided verdict still carries the inventory it found",
      c5.sessions_known == 3, f"known={c5.sessions_known}")
check("...and it is not dressed up as a warning",
      c5.warning() is None, str(c5.warning()))

# ---- the cached path the dashboard uses -----------------------------------
cov._clear_cache()
t0 = time.perf_counter()
first = cov.cached(trace_dirs=[traces])
cold = time.perf_counter() - t0
t0 = time.perf_counter()
second = cov.cached(trace_dirs=[traces])
warm = time.perf_counter() - t0
check("cached() returns the same objects on a warm hit", first is second)
check("a warm hit is far cheaper than a cold pass",
      warm < cold / 5 or warm < 0.001, f"cold={cold*1000:.1f}ms warm={warm*1000:.3f}ms")
check("check() bypasses the cache",
      cov.check(trace_dirs=[traces])[0] is not second[0])

# ONE cache slot on purpose. An earlier version keyed it by data source, which
# let the file-backed and index-backed answers sit side by side and report
# different verdicts for the same question on two surfaces at the same moment.
import inspect as _inspect  # noqa: E402
check("cached() takes no data-source key (one slot, one verdict)",
      "cache_key" not in _inspect.signature(cov.cached).parameters,
      str(list(_inspect.signature(cov.cached).parameters)))
check("a provider-backed call shares that slot",
      cov.cached(trace_dirs=[traces],
                 traced_provider=lambda: {"pi": set()}) is second)

# ---- a provider replaces the file scan ------------------------------------
calls: list[int] = []


def provider() -> dict[str, set[str]]:
    calls.append(1)
    return {"pi": {str(new_a), str(new_b)}}   # claims BOTH are traced


prov_rows = cov.check([source], trace_dirs=[traces], traced_provider=provider)
check("a traced_provider replaces the file scan", calls == [1], str(calls))
check("the provider's answer is what gets compared",
      prov_rows[0].sessions_covered == 2 and not prov_rows[0].untraced_recent,
      f"covered={prov_rows[0].sessions_covered} recent={len(prov_rows[0].untraced_recent)}")

# ---- the single-pass scanner reads every adapter at once ------------------
(traces / "hermes-y.jsonl").write_text(json.dumps(
    {"v": 1, "ts": "2026-06-10T00:00:01.000Z", "agent": "hermes",
     "event": "llm_request", "session_id": "20260610_000000_bbbbbb"}) + "\n",
    encoding="utf-8")
scanned = cov.scan_trace_files([traces])
check("scan_trace_files returns a bucket per adapter",
      {"pi", "hermes"} <= set(scanned), str(sorted(scanned)))
check("...collecting session ids per adapter",
      str(new_a) in scanned["pi"] and "20260610_000000_bbbbbb" in scanned["hermes"])
check("...and it does not depend on JSON spacing (the TS adapter writes compact)",
      str(new_a) in scanned["pi"])

# ---- the CLI surface ------------------------------------------------------
import subprocess  # noqa: E402
env = dict(os.environ, AGENTTRACE_PI_SESSIONS=str(sessions),
           AGENTTRACE_CODEX_HOME=str(tmp / "none"), AGENTTRACE_HOME=str(traces),
           HERMES_HOME=str(tmp / "none"))
res = subprocess.run([sys.executable, str(ROOT / "cli" / "agenttrace.py"),
                      "adapter", "coverage", "--json"],
                     capture_output=True, text=True, env=env, timeout=120)
check("adapter coverage --json runs", res.returncode in (0, 1), res.stderr[-200:])
try:
    payload = json.loads(res.stdout)
    check("--json emits one object per adapter",
          isinstance(payload, list) and all("adapter_id" in p for p in payload))
    check("--json carries the recent gaps and the hint",
          all({"untraced_recent", "backfill_hint"} <= set(p) for p in payload))
except json.JSONDecodeError as exc:
    check("--json emits parseable JSON", False, str(exc))

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("coverage OK")
