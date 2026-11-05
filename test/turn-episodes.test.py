#!/usr/bin/env python3
"""Turn identity: one task per HUMAN turn, not per `turn_id` value.

`turn_id` is not unique within a session. An agent restarts its counter at every
user prompt (pi counts 0,1,2… per human turn; measured 15 restarts across three
trace files), so keying a task on (adapter, session, turn_id) merged every run of
"turn 0" into one span. The projection then took min/max over all of them:

    agenttrace session <id>   ->  112 turns, wall 443.5h, "excluded 107
                                  interrupted turns"
    the same session's span   ->  54 minutes

Every per-turn percentile, every trace-duration bar and every "share of time
spent working" built on that span was wrong. This suite pins the fix:

  * a task is identified by (adapter, session, turn_id, episode), where the
    episode is the start ts of the human turn;
  * a boundary only opens a new episode when its turn_id DIFFERS from the last
    one seen — agents write user-role messages mid-turn to replay tool results
    (all 177 codex user_prompts are that kind) and splitting there cuts a span
    in half;
  * an incremental tail produces the SAME ids a full read of the same file does,
    which is what keeps a running dashboard from minting a second copy of a turn.

Run: python3 test/turn-episodes.test.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.convert.v1_to_v2 import (  # noqa: E402
    EPISODE_EVENTS, EpisodeTracker, convert_file, convert_record,
    convert_stream, synthesize_task_spans,
)
from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import DB_SCHEMA_VERSION, TraceStore  # noqa: E402

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


def ts(sec: float) -> str:
    base = 1_800_000_000
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime(base + sec)) + ".000Z"


def v1(event: str, sec: float, turn: str | None, **kw) -> dict:
    rec = {"v": 1, "ts": ts(sec), "agent": "pi", "event": event,
           "session_id": "sess", "turn_id": turn, "model": "m"}
    rec.update(kw)
    return rec


def _episodes(records: list[dict]) -> list[str | None]:
    from core.convert.v1_to_v2 import EpisodeTracker
    tracker = EpisodeTracker()
    return [tracker.step(r) for r in records]


def llm_pair(turn: str, at: float, request_id: str) -> list[dict]:
    return [v1("llm_request", at, turn, request_id=request_id),
            v1("llm_response", at + 2, turn, request_id=request_id,
               response={"content": "ok", "usage": {"input_tokens": 10,
                                                    "output_tokens": 5}})]


# ---------------------------------------------------------------------------
section("the converter: two runs of turn 0 are two tasks")
# ---------------------------------------------------------------------------

# Human turn 1: prompt, then turns 0 and 1. Second run: the counter restarts.
records = [
    v1("user_prompt", 0, "0", request={"messages": [{"role": "user", "content": "a"}]}),
    *llm_pair("0", 1, "r0"),
    *llm_pair("1", 10, "r1"),
    v1("user_prompt", 100, "0", request={"messages": [{"role": "user", "content": "b"}]}),
    *llm_pair("0", 101, "r2"),
]
episode_of = {0: None}          # first prompt opens episode 0
cur: str | None = None
last_turn: str | None = None
converted = []
for rec in records:
    if rec["event"] in EPISODE_EVENTS and (last_turn is None or rec["turn_id"] != last_turn):
        cur = rec["ts"]
    if rec["turn_id"] is not None:
        last_turn = rec["turn_id"]
    converted.append(convert_record(rec, cur))

by_span = {c["span_id"]: c for c in converted if c and c.get("span")}
check("two runs of turn_id 0 land on two different traces",
      by_span["llm:r0"]["trace_id"] != by_span["llm:r2"]["trace_id"],
      f"{by_span['llm:r0']['trace_id']} vs {by_span['llm:r2']['trace_id']}")
check("turns 0 and 1 inside one run stay on separate traces",
      by_span["llm:r0"]["trace_id"] != by_span["llm:r1"]["trace_id"])
again = []
cur2: str | None = None
last2: str | None = None
for rec in records:
    if rec["event"] in EPISODE_EVENTS and (last2 is None or rec["turn_id"] != last2):
        cur2 = rec["ts"]
    if rec["turn_id"] is not None:
        last2 = rec["turn_id"]
    again.append(convert_record(rec, cur2))
check("ids are stable when the same input is converted twice",
      [c["event_id"] for c in converted] == [c["event_id"] for c in again])
# A user_prompt carries its turn's id, so it lands WITH that turn rather than in
# a single-event trace of its own (which is what the first version did).
prompts = [c for c in converted
           if c.get("type") == "event" and c["event"]["name"] == "user_prompt"]
check("a user_prompt shares the trace of the turn it introduces",
      len(prompts) == 2
      and prompts[0]["trace_id"] == by_span["llm:r0"]["trace_id"]
      and prompts[1]["trace_id"] == by_span["llm:r2"]["trace_id"],
      ", ".join(f"{p['trace_id'][:8]}" for p in prompts)
      + f" vs {by_span['llm:r0']['trace_id'][:8]}/{by_span['llm:r2']['trace_id'][:8]}")

# a mid-turn user message (codex replays tool results this way) must NOT split
mid = [
    *llm_pair("t1", 0, "a"),
    v1("user_prompt", 1, "t1", request={"messages": [{"role": "user", "content": "tool out"}]}),
    *llm_pair("t1", 5, "b"),
]
mid_traces = set()
for rec in mid:
    c = convert_record(rec, "episode-1")
    if c and c.get("trace_id"):
        mid_traces.add(c["trace_id"])
check("a user_prompt carrying the SAME turn_id does not start a new episode",
      len(mid_traces) == 1, str(mid_traces))

# task spans group by the identity the converter mints, so no duplicate roots
tasks = list(synthesize_task_spans(records))
roots = [t for t in tasks if t["type"] == "span_start"]
check("synthesized task roots are unique per identity",
      len({t["span_id"] for t in roots}) == len(roots), str([t["span_id"] for t in roots]))
check("every task root's trace_id equals its span_id",
      all(t["trace_id"] == t["span_id"] for t in roots))


# ---------------------------------------------------------------------------
section("the indexer: an incremental tail matches a full read")
# ---------------------------------------------------------------------------

full = [
    v1("user_prompt", 0, "0", request={"messages": [{"role": "user", "content": "a"}]}),
    *llm_pair("0", 1, "r0"),
    *llm_pair("1", 10, "r1"),
    v1("user_prompt", 100, "0", request={"messages": [{"role": "user", "content": "b"}]}),
    *llm_pair("0", 101, "r2"),
]
tail = full[5:]        # everything from the second prompt on


def index(records_chunks: list[list[dict]], name: str) -> tuple[list[str], TraceStore]:
    """Index the chunks in order (each append triggers one scan)."""
    td = Path(tempfile.mkdtemp(prefix=f"episodes-{name}-"))
    traces = td / "traces"
    traces.mkdir()
    path = traces / "live.jsonl"
    store = TraceStore(td / "db.sqlite")
    idx = Indexer(store, [traces])
    for chunk in records_chunks:
        with path.open("a") as fh:
            for rec in chunk:
                fh.write(json.dumps(rec) + "\n")
        idx.scan_once()
    traces_ = [r["trace_id"] for r in store.query(
        "SELECT DISTINCT trace_id FROM spans WHERE trace_id IS NOT NULL"
        " ORDER BY trace_id")]
    return traces_, store


one_go, s1 = index([full], "full")
s1.close()
two_go, s2 = index([full[:5], tail], "split")
s2.close()
check("indexing a whole file and appending the same records produce the same traces",
      one_go == two_go, f"{len(one_go)} vs {len(two_go)} traces")
check("...and that is more than one trace (the turn counter restarted)",
      len(one_go) >= 3, str(one_go))

# the tracker state survives a restart of the process, which is what lets a tail
# continue the right human turn
td = Path(tempfile.mkdtemp(prefix="episodes-persist-"))
tr = td / "traces"
tr.mkdir()
(tr / "live.jsonl").write_text("\n".join(json.dumps(r) for r in full[:5]) + "\n")
store = TraceStore(td / "db.sqlite")
Indexer(store, [tr]).scan_once()
seeds = store.session_seeds()
check("the projection records the tracker state per session",
      ("pi", "sess") in seeds, str(list(seeds)))
check("...including the block it is inside",
      seeds[("pi", "sess")][1] == "1", str(seeds))
store.close()

store2 = TraceStore(td / "db.sqlite")          # a NEW process opening the same db
with (tr / "live.jsonl").open("a") as fh:
    for rec in full[5:]:
        fh.write(json.dumps(rec) + "\n")
Indexer(store2, [tr]).scan_once()
after = store2.session_episodes()
check("the tail continues the human turn the previous process was in",
      after[("pi", "sess")][0] == ts(100), str(after))
traces_after = {r["trace_id"] for r in store2.query(
    "SELECT DISTINCT trace_id FROM spans WHERE trace_id IS NOT NULL")}
check("...so the restarted run does not collide with the first one",
      len(traces_after) >= 3, str(traces_after))
store2.close()


# ---------------------------------------------------------------------------
section("cases the reviewers found (each failed before the fix)")
# ---------------------------------------------------------------------------

# (a) codex: llm_request, user_prompt with the SAME turn id, llm_response. Every
# converter entry point must keep that span on ONE trace, not just the indexer.
codex_rows = [
    v1("llm_request", 0, "abc", request_id="r1"),
    v1("user_prompt", 1, "abc",
       request={"messages": [{"role": "user", "content": "tool result: 42"}]}),
    v1("llm_response", 2, "abc", request_id="r1",
       response={"content": "done", "usage": {"output_tokens": 1}}),
]
td = Path(tempfile.mkdtemp(prefix="episodes-codex-"))
path = td / "codex.jsonl"
path.write_text("\n".join(json.dumps(r) for r in codex_rows) + "\n")
file_events = list(convert_file(path))
llm = [e for e in file_events if (e.get("span") or {}).get("kind") == "llm"]
check("convert_file keeps a mid-turn user_prompt inside the span",
      len({e["trace_id"] for e in llm}) == 1, str([e["trace_id"][:8] for e in llm]))
check("...and the span is closed", any(e["type"] == "span_end" for e in llm))
stream_events = list(convert_stream([path]))
llm_s = [e for e in stream_events if (e.get("span") or {}).get("kind") == "llm"]
check("convert_stream agrees with convert_file",
      {e["trace_id"] for e in llm} == {e["trace_id"] for e in llm_s},
      f"{len({e['trace_id'] for e in llm})} vs {len({e['trace_id'] for e in llm_s})}")
tasks_c = list(synthesize_task_spans(codex_rows))
check("...and so does the task synthesis",
      {t["trace_id"] for t in tasks_c if t["type"] == "span_start"}
      == {e["trace_id"] for e in llm},
      f"{len({t['trace_id'] for t in tasks_c})} task traces")

# (b) pi: a new human turn restarts at turn 0 after a session_end — the id is
# unchanged, so "turn_id differs" cannot see it. No open span, so it is a boundary.
pi_rows = [
    v1("user_prompt", 0, "0", request={"messages": [{"role": "user", "content": "first"}]}),
    *llm_pair("0", 1, "a"),
    v1("session_end", 5, None, note="done"),
    v1("user_prompt", 10, "0", request={"messages": [{"role": "user", "content": "second"}]}),
    *llm_pair("0", 11, "b"),
]
pi_events = [convert_record(r, e) for r, e in zip(pi_rows, _episodes(pi_rows))]
pi_traces = [c["trace_id"] for c in pi_events if c and c.get("span")
             and c["span_id"].startswith("llm:")]
check("a turn restarting at the same turn_id after session_end gets its own trace",
      len(set(pi_traces)) == 2, str([t[:8] for t in pi_traces]))

# (c) a span split across two files: request on day 1, response on day 2. Both an
# incremental scan and a full rebuild must keep it on one trace.
td = Path(tempfile.mkdtemp(prefix="episodes-split-"))
traces = td / "traces"
traces.mkdir()
(traces / "day1.jsonl").write_text(
    json.dumps(v1("user_prompt", 0, "0",
                  request={"messages": [{"role": "user", "content": "go"}]})) + "\n"
    + json.dumps(v1("llm_request", 1, "0", request_id="r")) + "\n")
(traces / "day2.jsonl").write_text(
    json.dumps(v1("llm_response", 2, "0", request_id="r",
                  response={"content": "x", "usage": {"output_tokens": 1}})) + "\n")


def span_state(build: str, name: str) -> tuple[int, int]:
    d = Path(tempfile.mkdtemp(prefix=f"episodes-{name}-"))
    store = TraceStore(d / "db.sqlite")
    idx = Indexer(store, [traces])
    idx.full_rebuild() if build == "full" else idx.scan_once()
    rows = store.query("SELECT trace_id, ended FROM spans WHERE span_id LIKE 'llm:%'")
    traces_ = len({r[0] for r in store.query(
        "SELECT DISTINCT trace_id FROM spans WHERE trace_id IS NOT NULL")})
    store.close()
    return len({r["trace_id"] for r in rows}), traces_


for build in ("scan", "full"):
    distinct, total = span_state(build, build)
    check(f"{build}: a span split across two files stays on one trace",
          distinct == 1, f"{distinct} trace ids for the llm span")

# (d) a backfill file beside the live one holds the same records: both copies must
# land on the same ids, or the store counts them twice.
td = Path(tempfile.mkdtemp(prefix="episodes-dup-"))
traces = td / "traces"
traces.mkdir()
rows = [v1("user_prompt", 0, "0",
           request={"messages": [{"role": "user", "content": "go"}]}),
        *llm_pair("0", 1, "r0")]
for name in ("live.jsonl", "backfill.jsonl"):
    (traces / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
store = TraceStore(td / "db.sqlite")
report = Indexer(store, [traces]).full_rebuild()
dup_traces = len({r[0] for r in store.query(
    "SELECT DISTINCT trace_id FROM spans WHERE trace_id IS NOT NULL")})
check("the same records in two files dedup instead of duplicating",
      dup_traces == 1 and report.events_deduped > 0,
      f"{dup_traces} traces, {report.events_deduped} deduped")
store.close()

# (e) an old index must keep saying "rebuild" even after a reader opens it.
td = Path(tempfile.mkdtemp(prefix="episodes-old-"))
traces = td / "traces"
traces.mkdir()
(traces / "a.jsonl").write_text("\n".join(json.dumps(r) for r in full) + "\n")
plat = td / "plat"
env = {**os.environ, "HOME": str(td / "h"), "AGENTTRACE_REDACT": "0",
       "AGENTTRACE_PLATFORM_DIR": str(plat)}
(td / "h").mkdir()


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "cli" / "agenttrace.py"),
                           *args], env=env, cwd=str(ROOT),
                          capture_output=True, text=True, timeout=300)


run("index", str(traces))
conn = sqlite3.connect(plat / "agenttrace.db")
conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
conn.commit()
conn.close()
before = run("doctor", str(traces), "--fast").stdout
store = TraceStore(plat / "agenttrace.db")     # what the dashboard does on scan
store.close()
after = run("doctor", str(traces), "--fast").stdout
check("doctor warns about an old index before a reader opens it",
      "rebuild with: agenttrace index --full" in before, before[-200:])
check("...and still warns AFTER one (the marker survives the open)",
      "rebuild with: agenttrace index --full" in after, after[-200:])


# ---------------------------------------------------------------------------
section("schema: the identity change is announced")
# ---------------------------------------------------------------------------

check("DB_SCHEMA_VERSION was bumped (an older index needs a rebuild)",
      DB_SCHEMA_VERSION == 5, str(DB_SCHEMA_VERSION))

td = Path(tempfile.mkdtemp(prefix="episodes-doctor-"))
traces = td / "traces"
traces.mkdir()
(traces / "a.jsonl").write_text("\n".join(json.dumps(r) for r in full) + "\n")
env = {**os.environ, "HOME": str(td / "h"), "AGENTTRACE_REDACT": "0",
       "AGENTTRACE_PLATFORM_DIR": str(td / "plat")}
(td / "h").mkdir()
subprocess.run([sys.executable, str(ROOT / "cli" / "agenttrace.py"),
                "index", str(traces)], env=env, cwd=str(ROOT),
               capture_output=True, text=True, timeout=300)
conn = sqlite3.connect(td / "plat" / "agenttrace.db")
conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
conn.commit()
conn.close()
p = subprocess.run([sys.executable, str(ROOT / "cli" / "agenttrace.py"),
                    "doctor", str(traces), "--fast"],
                   env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=300)
# The advice is printed under the check line, so search the whole report.
check("doctor tells an older index to rebuild, not merely reindex",
      "rebuild with: agenttrace index --full" in p.stdout.replace("\n", " "),
      p.stdout[-400:])

# ---------------------------------------------------------------------------
section("one writer: a rebuild must exclude the dashboard's scan loop")
# ---------------------------------------------------------------------------

# Found the hard way: with the daemon running, `index --full` cleared the archive
# while the daemon's scan loop was re-ingesting, and the database ended up with
# 52,000 events where 35,000 belong plus 1,800 id conflicts.
sys.path.insert(0, str(ROOT / "common"))
import agenttrace_common as common  # noqa: E402

td = Path(tempfile.mkdtemp(prefix="episodes-lock-"))
plat = td / "plat"
plat.mkdir()
tr = td / "traces"
tr.mkdir()
(tr / "a.jsonl").write_text("\n".join(json.dumps(r) for r in full) + "\n")
env = {**os.environ, "HOME": str(td / "h"), "AGENTTRACE_REDACT": "0",
       "AGENTTRACE_PLATFORM_DIR": str(plat)}
(td / "h").mkdir()
lock = common.index_lock_path(plat)

check("no lock means no writer", common.index_locked(plat) is None)
lock.write_text(str(os.getpid()))
check("a live pid is reported as the holder", common.index_locked(plat) == os.getpid())


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "cli" / "agenttrace.py"),
                           *args], env=env, cwd=str(ROOT),
                          capture_output=True, text=True, timeout=300)


p = run_cli("index", str(tr))
check("index refuses to run while a rebuild holds the lock",
      p.returncode == 2 and "another process is indexing" in p.stderr,
      f"rc={p.returncode} {p.stderr[:80]}")
lock.unlink()

dead = subprocess.Popen([sys.executable, "-c", "pass"])
dead.wait()
lock.write_text(str(dead.pid))
check("a dead pid's lock is stale, not a deadlock",
      common.index_locked(plat) is None)
p = run_cli("index", str(tr))
check("...and the next run steals it instead of failing",
      p.returncode == 0 and "events_added" in p.stdout, f"rc={p.returncode}")
check("...and cleans up after itself", not lock.exists())

print()
if FAILS:
    print(f"FAILED — {FAILS} check(s)")
    sys.exit(1)
print("turn episodes OK")
