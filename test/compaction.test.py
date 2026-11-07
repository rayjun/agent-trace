#!/usr/bin/env python3
"""The projection gives deleted pages back.

This store deletes in bulk: `clear_archive` on every rebuild, `drop_files` when a
trace file is removed. Without auto-vacuum those pages stay in the file forever —
measured on this machine, 4.24 GiB of a 9.18 GiB database was freelist after
three rebuilds, and the code had never compacted.

The checks below are the properties that make the fix real: a fresh index never
leaks, a rebuild's clear-then-refill is reclaimable, a deletion shrinks the file,
and the reported numbers are measured (not derived from a rewrite that is still
sitting in the WAL — the first version of `compact()` reported a NEGATIVE reclaim
on a database that had just shrunk).

Run: python3 test/compaction.test.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402

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


def on_disk(db: Path) -> int:
    return sum(Path(str(db) + suffix).stat().st_size
               for suffix in ("", "-wal", "-shm")
               if Path(str(db) + suffix).exists())


def make_db(records: int = 1500) -> tuple[Path, TraceStore, Path]:
    td = Path(tempfile.mkdtemp(prefix="compact-"))
    db = td / "t.db"
    traces = td / "traces"
    traces.mkdir()
    (traces / "a.jsonl").write_text("\n".join(
        json.dumps({"v": 1,
                    "ts": f"2026-11-04T10:{i // 60:02d}:{i % 60:02d}.000Z",
                    "agent": "pi", "event": "llm_request",
                    "session_id": f"s{i % 5}", "turn_id": str(i % 9),
                    "model": "m", "request": {"content": "x" * 3000}})
        for i in range(records)) + "\n")
    return db, TraceStore(db), traces


# ---------------------------------------------------------------------------
section("a fresh index does not leak")
# ---------------------------------------------------------------------------

db, store, traces = make_db()
Indexer(store, [traces]).scan_once()
fresh_free = store.freelist_bytes()
fresh_size = on_disk(db)
check("indexing leaves no free pages behind", fresh_free == 0, str(fresh_free))
check("...and the file is proportional to what it holds",
      fresh_size > 1_000_000, f"{fresh_size / 2**20:.1f} MiB")

# ---------------------------------------------------------------------------
section("auto-vacuum is armed by the first compact()")
# ---------------------------------------------------------------------------

check("a new database starts with auto-vacuum off",
      store.one("PRAGMA auto_vacuum")[0] == 0,
      str(store.one("PRAGMA auto_vacuum")[0]))
prior, now = store.compact()
check("compact() enables FULL auto-vacuum",
      store.one("PRAGMA auto_vacuum")[0] == 1,
      str(store.one("PRAGMA auto_vacuum")[0]))
check("the reported sizes are measured, never negative",
      prior > 0 and now > 0 and prior - now >= 0,
      f"{prior} -> {now}")
check("...and `now` matches the file on disk", now == on_disk(db),
      f"reported {now}, on disk {on_disk(db)}")

# ---------------------------------------------------------------------------
section("a bulk delete returns the space, without another VACUUM")
# ---------------------------------------------------------------------------

before_clear = on_disk(db)
store.clear_archive()
store.compact(full=False)          # checkpoint only: no rewrite
after_clear = on_disk(db)
check("clearing the archive shrinks the file",
      after_clear < before_clear / 4,
      f"{before_clear / 2**20:.1f} MiB -> {after_clear / 2**20:.1f} MiB")
check("...and leaves no free pages", store.freelist_bytes() == 0,
      str(store.freelist_bytes()))
store.close()

# ---------------------------------------------------------------------------
section("a rebuild compacts itself and says what it reclaimed")
# ---------------------------------------------------------------------------

db, store, traces = make_db()
Indexer(store, [traces]).scan_once()
first = Indexer(store, [traces]).full_rebuild()
check("the rebuild reports the file size after compaction",
      first.db_bytes > 0 and first.db_bytes == on_disk(db),
      f"{first.db_bytes} vs {on_disk(db)}")
check("...and a reclaim figure that is not negative",
      first.reclaimed_bytes >= 0, str(first.reclaimed_bytes))

# With FULL auto-vacuum armed, a deletion is reclaimed at commit — there is no
# backlog for the next rebuild to find. That is the point of choosing FULL over
# INCREMENTAL: nothing has to remember to vacuum.
size_before_delete = on_disk(db)
conn = sqlite3.connect(db)
conn.execute("DELETE FROM events WHERE rowid % 3 = 0")
conn.commit()
conn.close()
store.compact(full=False)          # checkpoint: no rewrite
check("deleting a third of the events shrinks the file by itself",
      on_disk(db) < size_before_delete * 0.95,
      f"{size_before_delete / 2**20:.1f} -> {on_disk(db) / 2**20:.1f} MiB")
check("...with nothing left on the freelist", store.freelist_bytes() == 0,
      str(store.freelist_bytes()))
second = Indexer(store, [traces]).full_rebuild()
check("a rebuild still reports a sane reclaim (0 when nothing was free)",
      second.reclaimed_bytes >= 0 and second.db_bytes == on_disk(db),
      f"reclaimed {second.reclaimed_bytes}, db {second.db_bytes}")
check("...and leaves no free pages", store.freelist_bytes() == 0,
      str(store.freelist_bytes()))
store.close()

# ---------------------------------------------------------------------------
section("dropping a file's rows shrinks the projection too")
# ---------------------------------------------------------------------------

db, store, traces = make_db()
Indexer(store, [traces]).scan_once()
store.compact()                     # arm auto-vacuum
filled = on_disk(db)
store.drop_files([str(traces / "a.jsonl")])
store.compact(full=False)           # checkpoint
emptied = on_disk(db)
check("removing a deleted trace file returns its space",
      emptied < filled / 4, f"{filled / 2**20:.1f} MiB -> {emptied / 2**20:.1f} MiB")
store.close()

print()
if FAILS:
    print(f"FAILED — {FAILS} check(s)")
    sys.exit(1)
print("compaction OK")
