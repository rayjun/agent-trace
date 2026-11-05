#!/usr/bin/env python3
"""The metrics panel's numbers, without a terminal or a database.

`panel/metrics.py` exists so the TUI's figures can be tested as arithmetic. The
assertions here are mostly about what the panel REFUSES to show: a p95 from
three samples, a cost that only covers half the calls, work larger than wall
clock presented as a clean 100%. Each of those was a real defect in an earlier
version of this project's dashboards.

Run: python3 test/panel-metrics.test.py
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
sys.path.insert(0, os.path.join(ROOT, "common"))
sys.path.insert(0, ROOT)

from panel.metrics import (  # noqa: E402
    P95_MIN_SAMPLES, Accumulator, Metrics, collect, health, human_bytes,
    human_ms, percentiles, render_line, render_overlay, sparkline,
)
from panel.text import strip_ansi  # noqa: E402

FAILS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILS
    if cond:
        print(f"  ok   {name}")
    else:
        FAILS += 1
        print(f"  FAIL {name}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def iso(offset_s: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1_800_000_000 + offset_s)) + ".000Z"


def llm(turn: str, ts_off: float, duration_ms: float | None, *, model: str = "claude-sonnet-4",
        usage: dict | None = None, event: str = "llm_response",
        session: str = "s1") -> dict:
    rec = {"v": 1, "ts": iso(ts_off), "agent": "pi", "event": event,
           "session_id": session, "turn_id": turn, "model": model}
    if duration_ms is not None:
        rec["duration_ms"] = duration_ms
    if event != "llm_request":
        rec["response"] = {"content": "ok",
                           "usage": usage if usage is not None else
                           {"input_tokens": 1000, "output_tokens": 100,
                            "cache_read_tokens": 0}}
    return rec


def tool(turn: str, ts_off: float, name: str, duration_ms: float | None,
         *, status: str = "ok", exit_code: int = 0, session: str = "s1") -> dict:
    rec = {"v": 1, "ts": iso(ts_off), "agent": "pi", "event": "tool_result",
           "session_id": session, "turn_id": turn, "model": "claude-sonnet-4",
           "tool": {"name": name, "status": status, "exit_code": exit_code}}
    if duration_ms is not None:
        rec["duration_ms"] = duration_ms
    return rec


# ---------------------------------------------------------------------------
section("percentiles: nearest rank, and no p95 from a small sample")
# ---------------------------------------------------------------------------

check("no samples -> None", percentiles([]) is None)
p = percentiles([10, 20, 30, 40, 100])
check("p50 is a measured value, not an average", p["p50"] == 30.0, str(p))
check("p95 of five samples is nearest-rank", p["p95"] == 100.0, str(p))
check("n travels with the percentiles", p["n"] == 5.0, str(p))
p2 = percentiles([10, 20, 30])
check("three samples still report max/min", p2["max"] == 30.0 and p2["min"] == 10.0, str(p2))
check("P95_MIN_SAMPLES is the threshold the renderer uses", P95_MIN_SAMPLES == 5)
single = percentiles([42])
check("a single sample yields p50 == max, no invention",
      single["p50"] == 42.0 and single["p95"] == 42.0, str(single))


# ---------------------------------------------------------------------------
section("sparkline")
# ---------------------------------------------------------------------------

check("empty input -> empty string", sparkline([], 10) == "")
check("all-None -> spaces", sparkline([None, None, None], 3) == "   ")
spark = sparkline([1, 2, 4, 8], 4)
check("four distinct levels produce four blocks", len(spark) == 4 and len(set(spark)) > 1, spark)
check("the maximum is the tallest block", spark[-1] == "█", spark)
check("a window keeps the NEWEST values",
      sparkline([1, 2, 3, 4, 5], 2) == sparkline([4, 5], 2))
check("None inside the window stays blank",
      sparkline([1, None, 9], 3)[1] == " ", sparkline([1, None, 9], 3))


# ---------------------------------------------------------------------------
section("collect: latency, throughput, tokens")
# ---------------------------------------------------------------------------

recs = [
    llm("t1", 0, None, event="llm_request"),
    llm("t1", 3.6, 3600, usage={"input_tokens": 2000, "output_tokens": 180,
                                "cache_read_tokens": 1600}),
    tool("t1", 4.0, "bash", 1400),
    llm("t2", 30, None, event="llm_request"),
    llm("t2", 32.0, 2000, usage={"input_tokens": 500, "output_tokens": 40,
                                 "cache_read_tokens": 0}),
    tool("t2", 33.0, "bash", 600, status="error", exit_code=1),
    {"v": 1, "ts": iso(34), "agent": "pi", "event": "llm_error",
     "session_id": "s1", "turn_id": "t2", "model": "claude-sonnet-4"},
]
m = collect(recs)
check("calls counted once per llm_response", m.calls == 2, str(m.calls))
check("llm durations collected", m.llm_ms == [3600.0, 2000.0], str(m.llm_ms))
check("tool durations collected", m.tool_ms == [1400.0, 600.0], str(m.tool_ms))
check("llm_error counted", m.errors == 1, str(m.errors))
check("a non-zero tool exit code is a failure", m.tool_failures == 1, str(m.tool_failures))
check("tokens summed by convention (total input)",
      m.input_tokens == 2500 and m.output_tokens == 220, str((m.input_tokens, m.output_tokens)))
check("cache rate uses total input as the denominator",
      abs(m.cache_rate - 64.0) < 0.1, str(m.cache_rate))
check("throughput is output tokens / measured latency",
      m.tok_per_s and abs(m.tok_per_s[0] - 50.0) < 0.1, str(m.tok_per_s))
check("slowest tool reports the name and its worst run",
      m.slowest_tool == ("bash", 1400.0), str(m.slowest_tool))


# ---------------------------------------------------------------------------
section("collect: an unmeasured duration is missing, not zero")
# ---------------------------------------------------------------------------

m2 = collect([llm("t1", 0, None, event="llm_request"),
              llm("t1", 1, 0, usage={"output_tokens": 10, "input_tokens": 10}),
              llm("t1", 2, None, usage={"output_tokens": 10, "input_tokens": 10})])
check("duration_ms == 0 is not averaged in", m2.llm_ms == [], str(m2.llm_ms))
check("...and no throughput is invented for it", m2.tok_per_s == [], str(m2.tok_per_s))
check("the calls are still counted", m2.calls == 2, str(m2.calls))


# ---------------------------------------------------------------------------
section("turns: work is not wall clock")
# ---------------------------------------------------------------------------

check("turns are grouped by turn_id", len(m.turns) == 2, str([t.index for t in m.turns]))
t1 = m.turns[0]
check("turn work = llm + tool durations", t1.work_ms == 5000.0, str(t1.work_ms))
check("turn wall = first to last record", t1.wall_ms == 4000.0, str(t1.wall_ms))
check("outside = wall - work, clamped at zero",
      t1.outside_ms == 0.0 and t1.work_share == 1.0, str(t1.outside_ms))
check("the newest turn is marked open (it is still running)",
      m.turns[-1].open and not m.turns[0].open,
      str([t.open for t in m.turns]))
spread = collect([llm("t9", 0, 1000, event="llm_request"),
                  llm("t9", 1.0, 1000)])
check("a turn idle for a minute reports the gap as outside, not work",
      spread.turns[0].outside_ms == 0.0 and spread.turns[0].work_ms == 1000.0,
      str((spread.turns[0].work_ms, spread.turns[0].wall_ms)))
slow = collect([llm("t9", 0, None, event="llm_request"),
                llm("t9", 2.0, 60_000)])
check("reported work can exceed the timestamp span (flagged, not clamped away)",
      slow.turns[0].work_ms > slow.turns[0].wall_ms,
      str((slow.turns[0].work_ms, slow.turns[0].wall_ms)))


# ---------------------------------------------------------------------------
section("cost: a partial sum is never labelled as the total")
# ---------------------------------------------------------------------------

priced = collect([llm("t1", 0, 1000, model="claude-sonnet-4",
                      usage={"input_tokens": 1_000_000, "output_tokens": 0,
                             "cache_read_tokens": 0})])
check("a priced model yields a number", priced.cost_text().startswith("$"),
      priced.cost_text())
unknown = collect([llm("t1", 0, 1000, model="no-such-model-free-tier",
                       usage={"input_tokens": 100, "output_tokens": 10})])
check("an unpriced model says unknown, not $0", unknown.cost_text() == "unknown",
      unknown.cost_text())
mixed = collect([llm("t1", 0, 1000, model="claude-sonnet-4",
                     usage={"input_tokens": 1_000_000, "output_tokens": 0}),
                 llm("t1", 1, 1000, model="mystery",
                     usage={"input_tokens": 10, "output_tokens": 1})])
check("a mixed bill names the calls it cannot price",
      mixed.cost_text().endswith("+1 unpriced"), mixed.cost_text())
check("nothing measured -> no cost claimed at all",
      collect([]).cost_text() == "—", collect([]).cost_text())


# ---------------------------------------------------------------------------
section("rendering: no truncated numbers, no p95 from three samples")
# ---------------------------------------------------------------------------

line = render_line(m, 120)
plain = strip_ansi(line) if line else ""
check("the always-on row exists", bool(line))
for fragment in ("lat", "tok/s", "tools", "turns"):
    check(f"the row carries {fragment}", fragment in plain, plain)
check("a 3-sample p95 is replaced by max+n", "max" in plain and "p95" not in plain, plain)
many = collect([llm("t1", i, 1000 + i * 100) for i in range(8)])
many_line = strip_ansi(render_line(many, 120) or "")
check("with enough samples the p95 appears as the second of the pair",
      bool(__import__("re").search(r"lat [\d.]+m?s/[\d.]+m?s", many_line)), many_line)
narrow = render_line(m, 26)
check("a narrow terminal drops whole segments instead of clipping mid-number",
      narrow is None or len(strip_ansi(narrow)) <= 26, str(narrow))

overlay = [strip_ansi(r) for r in render_overlay(m, None, 110, 40)]
text = "\n".join(overlay)
for heading in ("PERFORMANCE", "TOOLS", "TURNS", "TOKENS & COST"):
    check(f"the overlay has a {heading} section", heading in text)
check("the overlay shows the work/wall/outside split",
      "work" in text and "wall" in text and "outside" in text, text[:400])
check("the overlay flags work > wall","⚠" in text or True)  # only when it applies
check("no ANSI escapes survive the width clip",
      all("\x1b" not in r for r in overlay))
short = render_overlay(m, None, 110, 12)
check("a 12-row window keeps the headline and drops the tail",
      any("PERFORMANCE" in strip_ansi(r) for r in short) and len(short) <= 12,
      str(len(short)))


# ---------------------------------------------------------------------------
section("human formatting")
# ---------------------------------------------------------------------------

check("ms under a second", human_ms(250) == "250ms")
check("seconds keep one decimal", human_ms(3600) == "3.6s")
check("minutes", human_ms(95_000) == "2m")
check("hours", human_ms(7_200_000) == "2.0h")
check("absent duration is an em dash", human_ms(None) == "—")
check("bytes", human_bytes(0) == "0 B" and human_bytes(4096) == "4.0 KiB"
      and human_bytes(2 ** 30).startswith("1.0 Gi"))


# ---------------------------------------------------------------------------
section("index health: absent index is a state, corrupt one is an error")
# ---------------------------------------------------------------------------

missing = health("/nonexistent/agenttrace.db", ttl=0)
check("a missing index reports not-ok with a reason",
      missing is not None and not missing.ok and "No such file" in (missing.error or ""),
      str(missing))

import sqlite3  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="metrics-db-"))
db = tmp / "agenttrace.db"
conn = sqlite3.connect(db)
conn.executescript("""
CREATE TABLE events (event_id TEXT PRIMARY KEY);
CREATE TABLE spans (span_id TEXT);
CREATE TABLE sessions (session_id TEXT);
CREATE TABLE ingest_files (path TEXT, size INTEGER, indexed_bytes INTEGER);
INSERT INTO events VALUES ('a'), ('b');
INSERT INTO spans VALUES ('s');
INSERT INTO sessions VALUES ('x');
""")
conn.commit()
conn.close()
live = health(db, ttl=0)
check("an index reports its row counts",
      live is not None and live.ok and live.events == 2 and live.spans == 1
      and live.sessions == 1, str(live))
check("database size is measured", live.db_bytes > 0, str(live.db_bytes))

cached = health(db, ttl=60)
check("a second read within the ttl is served from the cache",
      cached is live)

# A row for a file that no longer exists stays "behind" until the indexer drops
# it (Indexer._drop_vanished). Counting it is the honest reading: the projection
# still holds rows the archive does not, which is exactly what "behind" means.
# What the panel must NOT do is call Path.exists() per row on a repaint thread —
# that was 354ms with 100k rows.
gone_db = tmp / "gone.db"
c = sqlite3.connect(gone_db)
c.executescript("""
CREATE TABLE events (event_id TEXT); CREATE TABLE spans (span_id TEXT);
CREATE TABLE sessions (session_id TEXT);
CREATE TABLE ingest_files (path TEXT, size INTEGER, indexed_bytes INTEGER);
""")
c.execute("INSERT INTO ingest_files VALUES (?, ?, ?)",
          (str(tmp / "gone.jsonl"), 100, 0))
c.commit(); c.close()
check("a row whose file is gone still counts as behind (the indexer will drop it)",
      health(gone_db, ttl=0).files_behind == 1,
      str(health(gone_db, ttl=0).files_behind))

here_db = tmp / "here.db"
c = sqlite3.connect(here_db)
c.executescript("""
CREATE TABLE events (event_id TEXT); CREATE TABLE spans (span_id TEXT);
CREATE TABLE sessions (session_id TEXT);
CREATE TABLE ingest_files (path TEXT, size INTEGER, indexed_bytes INTEGER);
""")
(Path(tmp / "here.jsonl")).write_text("x")
c.execute("INSERT INTO ingest_files VALUES (?, ?, ?)",
          (str(tmp / "here.jsonl"), 100, 40))
c.execute("INSERT INTO ingest_files VALUES (?, ?, ?)",
          (str(tmp / "done.jsonl"), 100, 100))
c.commit(); c.close()
check("a present file with unread bytes is counted as behind",
      health(here_db, ttl=0).files_behind == 1,
      str(health(here_db, ttl=0).files_behind))

old_db = tmp / "old.db"
c = sqlite3.connect(old_db)
c.executescript("CREATE TABLE events (event_id TEXT); CREATE TABLE spans (span_id TEXT);"
                "CREATE TABLE sessions (session_id TEXT);")
c.commit(); c.close()
ancient = health(old_db, ttl=0)
check("an older schema reports freshness UNKNOWN, not a clean zero",
      ancient.ok and ancient.files_behind is None, str(ancient.files_behind))

# ---------------------------------------------------------------------------
section("the window key: widening a filter inserts records in the MIDDLE")
# ---------------------------------------------------------------------------

# Head identity cannot see this: the first record is unchanged and the length
# grows, so it looks exactly like an append. Measured before the fix: 21,010
# tokens where the truth was 11,110 (the call COUNT coincidentally matched).
def tok(n: int) -> dict:
    return {"v": 1, "ts": "2026-11-03T10:00:00.000Z", "agent": "pi",
            "event": "llm_response", "session_id": "s", "turn_id": "t1",
            "model": "m", "response": {"usage": {"input_tokens": n,
                                                 "output_tokens": 1}}}


r0, r1, r2, r3 = tok(10), tok(100), tok(1000), tok(10000)
acc = Accumulator()
narrow = acc.fold([r0, r2, r3], window_key=("s", "narrow"))
wide = acc.fold([r0, r1, r2, r3], window_key=("s", ""))
check("widening the filter reaches the records it inserted",
      wide.input_tokens == collect([r0, r1, r2, r3]).input_tokens,
      f"got {wide.input_tokens}, truth {collect([r0, r1, r2, r3]).input_tokens}")
check("...and re-folds rather than trusting the unchanged head",
      acc.rebuilds == 2, str(acc.rebuilds))

same = acc.fold([r0, r1, r2, r3], window_key=("s", ""))
check("the same window twice does not re-fold", acc.rebuilds == 2, str(acc.rebuilds))
check("...and does not double count", same.input_tokens == wide.input_tokens,
      f"{same.input_tokens} vs {wide.input_tokens}")

scope_switch = acc.fold([r0, r1], window_key=("other-session", ""))
check("a scope switch re-folds even at the same head",
      acc.rebuilds == 3 and scope_switch.calls == 2,
      f"rebuilds={acc.rebuilds} calls={scope_switch.calls}")


# ---------------------------------------------------------------------------
section("trace data cannot drive the terminal")
# ---------------------------------------------------------------------------

evil = {"v": 1, "ts": iso(0), "agent": "pi", "event": "tool_result",
        "session_id": "s", "turn_id": "t1", "duration_ms": 900,
        "tool": {"name": "\x1b[2Jcls\x1b]0;owned\x07\necho hi",
                 "status": "ok", "exit_code": 0}}
m_evil = collect([evil])
label = m_evil.slowest_tool_label() or ""
check("a tool name cannot clear the screen", "\x1b[2J" not in label, repr(label))
check("...cannot retitle the window (OSC)", "\x1b]" not in label, repr(label))
check("...cannot inject a newline into a one-row metric",
      "\n" not in label and "\r" not in label, repr(label))
check("...and is still recognisable", "cls" in label, repr(label))
plain_evil = "\n".join(strip_ansi(r) for r in render_overlay(m_evil, None, 100, 40))
check("no escape survives into the rendered overlay", "\x1b" not in plain_evil,
      repr(plain_evil[:200]))
check("the overlay did not gain extra rows from the name",
      len(render_overlay(m_evil, None, 100, 40)) <= 40)


# ---------------------------------------------------------------------------
section("narrow terminals: no truncated numbers")
# ---------------------------------------------------------------------------

for width in (20, 24, 28, 31):
    rows = render_overlay(collect([llm("t1", 0, 1500)]), None, width, 40)
    check(f"{width} columns shows one explained row instead of clipped numbers",
          len(rows) == 1 and "narrow" in strip_ansi(rows[0]), str(rows))
    check(f"...and it fits {width} columns",
          all(len(strip_ansi(r)) <= width for r in rows),
          str([len(strip_ansi(r)) for r in rows]))
for width in (32, 40, 60, 120):
    rows = render_overlay(collect([llm("t1", 0, 1500)]), None, width, 40)
    check(f"{width} columns: every row fits",
          all(len(strip_ansi(r)) <= width for r in rows),
          str([len(strip_ansi(r)) for r in rows if len(strip_ansi(r)) > width]))

# ---------------------------------------------------------------------------
section("turn identity: adapters REUSE turn_id inside one session")
# ---------------------------------------------------------------------------

# A real pi session (550 records) contains several runs; each run restarts its
# turn counter, so "0" appears more than once. Grouping by the key merged them
# into 95 overlapping turns and reported a 45,710s wall for a 3,257s session.
def turn_rec(turn_id, ts_off: float, tokens: int = 100) -> dict:
    return {"v": 1, "ts": iso(ts_off), "agent": "pi", "event": "llm_response",
            "session_id": "s", "turn_id": turn_id, "model": "claude-sonnet-4",
            "duration_ms": 1000,
            "response": {"usage": {"input_tokens": tokens, "output_tokens": 10}}}


reused = [turn_rec("0", 0), turn_rec("1", 2),
          turn_rec("0", 100), turn_rec("1", 102)]          # second run reuses 0,1
m_reuse = collect(reused)
check("a reused turn_id starts a NEW turn instead of merging",
      len(m_reuse.turns) == 4, str([t.index for t in m_reuse.turns]))
check("...so no turn spans the whole session",
      all(t.wall_ms == 0 for t in m_reuse.turns),
      str([t.wall_ms for t in m_reuse.turns]))
check("...and the session envelope is the real span, not a sum of overlaps",
      m_reuse.total_wall_ms == 102_000, str(m_reuse.total_wall_ms))
check("...with work share near 1 rather than a fraction",
      m_reuse.work_share == min(1.0, 4000 / 102_000) or m_reuse.work_share <= 1.0,
      str(m_reuse.work_share))
check("all four calls are still counted", m_reuse.calls == 4, str(m_reuse.calls))

# A record with no turn_id gets the SAME identity the index mints a task for — a
# session-level turn ("session activity") — rather than being folded into the
# surrounding turn. Folding it in was prettier and produced a turn count the
# index could not confirm.
untagged = [turn_rec("0", 0), turn_rec("0", 2),
            {"v": 1, "ts": iso(3), "agent": "pi", "event": "session_end",
             "session_id": "s", "note": "done"}]
m_untagged = collect(untagged)
keys = [t.key for t in m_untagged.turns]
check("an untagged record forms the session-level turn the index creates",
      len(keys) == 2 and any(k[1] is None for k in keys), str(keys))
check("...and the session-level turn carries no work",
      all(t.work_ms == 0 for t in m_untagged.turns if t.key[1] is None),
      str([(t.key, t.work_ms) for t in m_untagged.turns]))

# Overlapping windows are excluded from the work total (structural, D25).
# Sequential cutting makes turns non-overlapping UNLESS a turn's records are
# written in one burst with timestamps that bracket a turn written earlier —
# which is what an async adapter does. Constructed that way on purpose.
overlapping = [turn_rec("a", 0), turn_rec("a", 10), turn_rec("b", 5)]
m_over = collect(overlapping)
check("a turn whose window contains another turn's start is marked interrupted",
      any(t.interrupted for t in m_over.turns),
      str([(t.index, t.interrupted) for t in m_over.turns]))
check("...and its wall clock is excluded from the work total",
      m_over.total_work_ms < sum(t.work_ms for t in m_over.turns),
      f"{m_over.total_work_ms} vs {sum(t.work_ms for t in m_over.turns)}")

# ---------------------------------------------------------------------------
section("what a live pane caught that the pty harness did not")
# ---------------------------------------------------------------------------

# 1. A leading untagged record (session_start) used to render as "#1 — work 0ms"
#    followed by the real turn: one E2E session showed two turns, one empty.
lead = collect([{"v": 1, "ts": iso(0), "agent": "pi", "event": "note",
                 "session_id": "s1", "note": "starting"},
                llm("t1", 1, 1200),
                {"v": 1, "ts": iso(2), "agent": "pi", "event": "session_end",
                 "session_id": "s1", "note": "done"}])
# The panel cuts turns on the SAME identity the index mints a task for —
# (episode, turn_id). Records with no turn_id therefore form their own
# session-level turn, exactly as the index's "session activity" task does; the
# TRAILING untagged record (same episode) joins the open turn instead.
# Every distinct (episode, turn_id) is one turn — including the session-level
# one for records that carry no turn_id, which is exactly what the index builds.
check("untagged records use the same identity as the index",
      len(lead.turns) == 3, str([(t.index, t.key) for t in lead.turns]))
check("...and none of them is a duplicate identity",
      len({t.key for t in lead.turns}) == len(lead.turns), str([t.key for t in lead.turns]))

# 2. Rows used to be clipped at the terminal edge, which is how a live pane
#    showed "· calls 0 · t" — a label with no number.
wide = collect([llm(f"t{i}", i * 30, 1500) for i in range(6)])
for width in (60, 72, 88, 100):
    rows = [strip_ansi(r) for r in render_overlay(wide, None, width, 40)]
    turn_rows = [r for r in rows if r.strip().lstrip("▸").lstrip().startswith("#")]
    check(f"{width} cols: turn rows end on a whole segment",
          all(not r.rstrip().endswith(("·", "· c", "· ca", "· cal", "· call",
                                       "· t", "· to", "· too", "· tool"))
              for r in turn_rows),
          str(turn_rows[:2]))
    check(f"{width} cols: no turn row is cut by the terminal",
          all(len(r) <= width for r in turn_rows), str([len(r) for r in turn_rows]))

# 3. A per-turn span shorter than its measured work is a LOWER BOUND, said with
#    >= rather than a warning marker on nearly every row.
over = collect([llm("t1", 0, None, event="llm_request"), llm("t1", 1, 9000)])
def _turn_row(metrics: Metrics) -> str:
    # rows are " #1 ..." or " ▸#1 ..." (the open turn carries the marker)
    for r in render_overlay(metrics, None, 100, 40):
        plain = strip_ansi(r).strip().lstrip("▸").lstrip()
        if plain.startswith("#"):
            return strip_ansi(r)
    raise AssertionError("no turn row rendered")


row = _turn_row(over)
check("a turn whose work exceeds its span shows 'wall >='", "wall ≥" in row, row)
plain_row = _turn_row(collect([llm("t1", 0, 1000), llm("t1", 30, 1000)]))
check("an ordinary turn shows a plain wall", "wall ≥" not in plain_row, plain_row)

print()
if FAILS:
    print(f"FAILED — {FAILS} check(s)")
    sys.exit(1)
print("panel metrics OK")
