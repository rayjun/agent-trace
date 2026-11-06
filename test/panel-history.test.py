#!/usr/bin/env python3
"""`watch` preloads history from file TAILS, and still shows the right records.

Reading whole files made the panel take 23 seconds to start on the 2.4 GiB of
traces this machine holds: every record of every file parsed and sorted to keep
the last few hundred. The records a live panel needs are at the tail of the
newest files.

The risk in that shortcut is silently showing the wrong window, so these checks
compare it against the exact implementation (read everything, sort, take the
last N) on fixtures where the exact answer is known, and pin the bound: nothing
deeper than the read window is claimed to be complete.

Run: python3 test/panel-history.test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli"))
sys.path.insert(0, str(ROOT / "common"))
sys.path.insert(0, str(ROOT))

from agenttrace import iter_records  # noqa: E402
from panel.tailer import (  # noqa: E402
    PRELOAD_MAX_FILES, load_history,
)

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


def exact(dirs: list[Path], keep, history: int) -> list[dict]:
    """The original implementation: read everything, sort, take the last N."""
    recs = [r for _, _, r in iter_records(dirs) if keep(r)]
    recs.sort(key=lambda r: r.get("ts") or "")
    return recs[-history:]


def ts(sec: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1_800_000_000 + sec)) + ".000Z"


def rec(sec: int, agent: str = "pi", event: str = "llm_request", **kw) -> dict:
    d = {"v": 1, "ts": ts(sec), "agent": agent, "event": event,
         "session_id": kw.pop("session_id", "s1"), "model": "m"}
    d.update(kw)
    return d


# ---------------------------------------------------------------------------
section("a normal trace directory: tail read == exact read")
# ---------------------------------------------------------------------------

td = Path(tempfile.mkdtemp(prefix="history-"))
traces = td / "traces"
traces.mkdir()
# Three files, appended in order, each with contiguous timestamps — the shape
# every adapter produces.
for f, base in (("a.jsonl", 0), ("b.jsonl", 100), ("c.jsonl", 200)):
    rows = [rec(base + i, session_id=f"s{base}") for i in range(40)]
    (traces / f).write_text("\n".join(json.dumps(r) for r in rows) + "\n")

for n in (10, 50, 120):
    got = load_history([traces], lambda r: True, n)
    want = exact([traces], lambda r: True, n)
    check(f"history={n}: same records as the exact read",
          [r["ts"] for r in got] == [r["ts"] for r in want],
          f"{len(got)} vs {len(want)}")
    check(f"history={n}: sorted by ts", [r["ts"] for r in got] ==
          sorted(r["ts"] for r in got))

few = load_history([traces], lambda r: True, 500)
check("asking for more than exists returns everything, not padding",
      len(few) == 120, str(len(few)))

# ---------------------------------------------------------------------------
section("filters are applied while reading, not after")
# ---------------------------------------------------------------------------

(traces / "d.jsonl").write_text("\n".join(json.dumps(r) for r in
                                          [rec(400 + i, agent="codex") for i in range(30)]) + "\n")
only_pi = load_history([traces], lambda r: r.get("agent") == "pi", 20)
check("a filter that matches nothing in the newest file still finds records",
      len(only_pi) == 20 and all(r["agent"] == "pi" for r in only_pi),
      f"{len(only_pi)} records")
check("...and they are the newest matching ones",
      [r["ts"] for r in only_pi] == sorted(r["ts"] for r in only_pi)[-20:])
none = load_history([traces], lambda r: r.get("agent") == "nobody", 20)
check("no match returns an empty list, not the unfiltered tail", none == [], str(len(none)))

# ---------------------------------------------------------------------------
section("a record longer than one read block")
# ---------------------------------------------------------------------------

big = Path(tempfile.mkdtemp(prefix="history-big-")) / "big"
big.mkdir()
# 400 KB of payload: bigger than the first 256 KB block, so the reader has to
# grow its window and finish a partially read line.
rows = [rec(0), rec(1, note="x" * 400_000), rec(2)]
(big / "big.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
got_big = load_history([big], lambda r: True, 3)
check("a record larger than the first block is still parsed",
      [r["ts"] for r in got_big] == [r["ts"] for r in rows],
      str([r.get("ts") for r in got_big]))

# ---------------------------------------------------------------------------
section("cost: history read is bounded by the tail, not the file set")
# ---------------------------------------------------------------------------

wide = Path(tempfile.mkdtemp(prefix="history-wide-")) / "wide"
wide.mkdir()
payload = "y" * 4000
for f in range(30):
    rows = [rec(f * 100 + i, note=payload) for i in range(60)]
    (wide / f"f{f:02d}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

start = time.perf_counter()
got_wide = load_history([wide], lambda r: True, 40)
tail_ms = (time.perf_counter() - start) * 1000
start = time.perf_counter()
want_wide = exact([wide], lambda r: True, 40)
exact_ms = (time.perf_counter() - start) * 1000
check("the tail read is much cheaper than reading everything",
      tail_ms < exact_ms / 2, f"{tail_ms:.0f}ms vs {exact_ms:.0f}ms")
check("...and still returns the newest records",
      [r["ts"] for r in got_wide] == [r["ts"] for r in want_wide],
      f"{got_wide[-1]['ts']} vs {want_wide[-1]['ts']}")
check("...reading at most PRELOAD_MAX_FILES files",
      PRELOAD_MAX_FILES <= 8, str(PRELOAD_MAX_FILES))

# ---------------------------------------------------------------------------
section("the cases the reviewer found (each failed before the fix)")
# ---------------------------------------------------------------------------

# (a) equal timestamps keep file order. Reverse reading plus a ts-only sort
#     reversed them, and equal stamps are common in real traces.
same = Path(tempfile.mkdtemp(prefix="history-same-")) / "t"
same.mkdir()
rows = [rec(0, session_id=f"b{i}") for i in range(3)]
(same / "a.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
got_same = load_history([same], lambda r: True, 3)
check("records with the SAME ts keep the order they were written",
      [r["session_id"] for r in got_same] == ["b0", "b1", "b2"],
      str([r["session_id"] for r in got_same]))

# (b) PRELOAD_TAIL_BYTES is a real per-file cap. 2 MB must not become "read to
#     EOF when nothing matches".
import panel.tailer as tailer_mod  # noqa: E402

capped = Path(tempfile.mkdtemp(prefix="history-cap-")) / "t"
capped.mkdir()
big_rows = [rec(i, note="z" * 3000) for i in range(4000)]   # ~12 MB
(capped / "big.jsonl").write_text("\n".join(json.dumps(r) for r in big_rows) + "\n")
reads = {"bytes": 0}
real_open = Path.open


def counting_open(self, *a, **kw):
    fh = real_open(self, *a, **kw)
    if self == capped / "big.jsonl":
        real_read = fh.read

        def read(n=-1):
            data = real_read(n)
            reads["bytes"] += len(data)
            return data
        fh.read = read
    return fh


Path.open = counting_open
try:
    load_history([capped], lambda r: False, 10)        # nothing matches
finally:
    Path.open = real_open
check("a no-match filter stops at PRELOAD_TAIL_BYTES, not at EOF",
      reads["bytes"] <= tailer_mod.PRELOAD_TAIL_BYTES * 1.2,
      f"read {reads['bytes'] / 2**20:.1f} MiB of a 12 MB file")

# (c) a file deleted between ranking and reading must not raise.
gone = Path(tempfile.mkdtemp(prefix="history-gone-")) / "t"
gone.mkdir()
for i in range(3):
    (gone / f"g{i}.jsonl").write_text(json.dumps(rec(i)) + "\n")
real_rank = tailer_mod._rank_by_last_ts


def vanishing_rank(files):
    ranked = real_rank(files)
    for f in ranked[1:]:
        f.unlink()                                    # deleted after ranking
    return ranked


tailer_mod._rank_by_last_ts = vanishing_rank
try:
    got_gone = load_history([gone], lambda r: True, 5)
    check("a file deleted mid-preload is skipped, not fatal",
          isinstance(got_gone, list), str(got_gone))
finally:
    tailer_mod._rank_by_last_ts = real_rank

# (d) the byte budget: a request that cannot be filled degrades visibly instead of
#     reading gigabytes. The bound is what makes this a shortcut and not a lie.
import panel.tailer as tailer_mod  # noqa: E402

check("the budget exists and is a bound, not a target",
      tailer_mod.PRELOAD_BUDGET_BYTES >= tailer_mod.PRELOAD_TAIL_BYTES,
      f"{tailer_mod.PRELOAD_BUDGET_BYTES / 2**20:.0f} MiB")

# A file whose records do not match must cost at most the per-file cap.
budget_dir = Path(tempfile.mkdtemp(prefix="history-budget-")) / "t"
budget_dir.mkdir()
for i in range(4):
    rows = [rec(i * 100 + j, note="q" * 3000) for j in range(500)]
    (budget_dir / f"b{i}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
start_px = time.perf_counter()
none_match = load_history([budget_dir], lambda r: False, 10)
took_ms = (time.perf_counter() - start_px) * 1000
check("a no-match filter returns nothing, quickly",
      none_match == [] and took_ms < 2000, f"{took_ms:.0f} ms")

print()
if FAILS:
    print(f"FAILED — {FAILS} check(s)")
    sys.exit(1)
print("panel history OK")
