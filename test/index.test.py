#!/usr/bin/env python3
"""The trace index, and the two `ls`/`sessions` bugs it was built alongside.

Two classes of assertion, both end-to-end through the real CLI:

1. EQUIVALENCE. Every command must produce byte-identical output whether it is
   served from the index or from a full body scan (`AGENTTRACE_INDEX=0`). That
   is the entire safety argument for a cache: it may only ever change how fast
   the answer arrives.

   Covered across: append (incremental), truncation (invalidation), a corrupt
   state file (rebuild), an unwritable cache dir (fall back), `--contains`
   (bypass), and a multi-day corpus where `ls` is allowed to skip days.

2. The two bugs found while doing this work:
   * `agenttrace ls` sorted ascending and took `[:limit]`, selecting the
     OLDEST records — on any history longer than --limit the recent rows the
     README calls "recent records" were exactly the ones never printed.
   * `agenttrace sessions` ignored every filter, while the README promises
     "all commands" accept --agent/--model/--event/--session/--since.

Plus a timing check: if the index is not actually faster, the complexity
earns nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "cli" / "agenttrace.py"
fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


def run(args: list[str], index_dir: Path, **env_extra) -> subprocess.CompletedProcess:
    env = dict(os.environ, AGENTTRACE_INDEX_DIR=str(index_dir))
    env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, env=env,
                          cwd=str(ROOT), timeout=300)


def both_ways(args: list[str], index_dir: Path):
    """Run once through the index and once with it switched off."""
    return run(args, index_dir), run(args, index_dir, AGENTTRACE_INDEX="0")


def stamps(proc: subprocess.CompletedProcess) -> list[str]:
    """The time column of `agenttrace ls` output (preview() prints HH:MM:SS)."""
    return [line.split()[0] for line in proc.stdout.splitlines() if line.strip()]


def make_traces(root: Path) -> None:
    """Three days x two agents.

    Each day gets its own hour band (28 -> 00-05, 29 -> 06-11, 30 -> 12-17) so
    the HH:MM:SS column `preview()` prints is enough to tell days apart — the
    sort key is the full timestamp, but assertions can only see the preview.
    """
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for day in (28, 29, 30):
        hour0 = (day - 28) * 6
        for agent in ("pi", "hermes"):
            for i in range(6):
                n = day * 100 + i
                hour = hour0 + i
                sid, rid = f"sess-{agent}-{day}", f"req-{n}"
                base = {"v": 1, "agent": agent, "cwd": f"/work/{day}",
                        "session_id": sid, "turn_id": f"t{n}",
                        "request_id": rid, "model": f"{agent}-model"}
                rows.append({**base, "ts": f"2026-09-{day}T{hour:02d}:00:00.000Z",
                             "event": "user_prompt",
                             "request": {"messages": [
                                 {"role": "user", "content": f"prompt {n}"}]}})
                rows.append({**base, "ts": f"2026-09-{day}T{hour:02d}:00:01.000Z",
                             "event": "llm_request",
                             "request": {"message_count": 3, "tool_count": 2,
                                         "system_prompt": "SYS " * 10}})
                rows.append({**base, "ts": f"2026-09-{day}T{hour:02d}:00:02.000Z",
                             "event": "llm_response", "duration_ms": 1000 + n,
                             "response": {"content": f"reply {n}",
                                          "finish_reason": "stop",
                                          "usage": {"input_tokens": 1000 + n,
                                                    "output_tokens": 10,
                                                    "cache_read_tokens": 800 + n}}})
    for day in (28, 29, 30):
        subset = [r for r in rows if r["ts"].startswith(f"2026-09-{day}")]
        (root / f"mixed-202609{day}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in subset), encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="index-test-"))
    traces, idx = tmp / "traces", tmp / "cache"
    make_traces(traces)
    dirs = [str(traces)]

    print("1. index output == body-scan output")
    for name, args in (
        ("ls default", ["ls", *dirs]),
        ("ls --reverse", ["ls", "--reverse", *dirs]),
        ("ls --limit 7", ["ls", "--limit", "7", *dirs]),
        ("ls --agent pi", ["ls", "--agent", "pi", *dirs]),
        ("ls --since", ["ls", "--since", "2026-09-29", *dirs]),
        ("ls --session", ["ls", "--session", "sess-pi-30", *dirs]),
        ("sessions", ["sessions", *dirs]),
        ("sessions --agent pi", ["sessions", "--agent", "pi", *dirs]),
        ("stats", ["stats", *dirs]),
        ("stats --agent hermes", ["stats", "--agent", "hermes", *dirs]),
        ("show", ["show", "req-3005", *dirs]),
    ):
        on, off = both_ways(args, idx)
        check(f"{name}: same exit code", on.returncode == off.returncode,
              f"{on.returncode} vs {off.returncode} {on.stderr[-200:]}")
        check(f"{name}: same stdout", on.stdout == off.stdout,
              f"--- on ---\n{on.stdout[:500]}\n--- off ---\n{off.stdout[:500]}")

    print("2. `show` really resolved the record")
    on = run(["show", "req-3005", *dirs], idx)
    check("show exits 0", on.returncode == 0, on.stderr[-300:])
    check("show prints the response body", "reply 3005" in on.stdout, on.stdout[:400])

    print("3. --contains bypasses the index (the index keeps no content)")
    on, off = both_ways(["search", "reply 2903", *dirs], idx)
    check("search finds the record", "reply 2903" in on.stdout, on.stdout[:400])
    # Both agents emit a record for the same synthetic turn, so the count is 2;
    # assert it AGREES with the printed rows rather than hard-coding a number.
    try:
        reported = int(on.stderr.split("match(es)")[0].split()[-1])
    except (ValueError, IndexError):
        reported = -1
    printed = len([l for l in on.stdout.splitlines() if l.strip()])
    check("search reports a match count", reported > 0, on.stderr)
    check("reported count == rows printed", reported == printed,
          f"{reported} vs {printed}")
    check("search ignores the index entirely", on.stdout == off.stdout,
          "the indexed and unindexed runs must be the same code path")

    print("4. `ls` selects the NEWEST N (the bug)")
    on = run(["ls", "--limit", "1000", *dirs], idx, AGENTTRACE_INDEX="0")
    everything = stamps(on)
    check("corpus is 108 records", len(everything) == 108, str(len(everything)))
    check("records sort oldest -> newest", everything == sorted(everything),
          str(everything[:3]))

    picked = stamps(run(["ls", *dirs], idx, AGENTTRACE_INDEX="0"))  # default 40
    check("default limit honoured", len(picked) == 40, str(len(picked)))
    check("ls --limit N == the LAST N of the full listing",
          picked == everything[-40:],
          f"got {picked[:3]}... want {everything[-40:][:3]}...")
    check("the oldest records were dropped",
          picked[0] == everything[-40],
          f"first shown {picked[0]}, full listing position -40 is {everything[-40]}")

    rev = stamps(run(["ls", "--limit", "7", "--reverse", *dirs], idx,
                     AGENTTRACE_INDEX="0"))
    check("--reverse shows the same 7, newest first",
          rev == everything[-7:][::-1], str(rev))

    print("5. `sessions` honours filters (the bug)")
    all_sess = run(["sessions", *dirs], idx, AGENTTRACE_INDEX="0").stdout
    only_pi = run(["sessions", "--agent", "pi", *dirs], idx,
                  AGENTTRACE_INDEX="0").stdout
    check("unfiltered sessions lists both agents",
          " pi " in all_sess and " hermes " in all_sess, all_sess)
    check("--agent pi drops hermes rows",
          "pi" in only_pi and "hermes" not in only_pi, only_pi)
    check("filtering reduced the row count",
          len(only_pi.splitlines()) < len(all_sess.splitlines()),
          f"{len(only_pi.splitlines())} vs {len(all_sess.splitlines())}")
    since = run(["sessions", "--since", "2026-09-30", *dirs], idx,
                AGENTTRACE_INDEX="0").stdout
    check("--since narrows the sessions shown",
          len(since.splitlines()) < len(all_sess.splitlines()),
          f"{len(since.splitlines())} vs {len(all_sess.splitlines())}")

    print("6. incremental append")
    new = {"v": 1, "ts": "2026-09-30T23:00:00.000Z", "agent": "pi",
           "session_id": "sess-pi-new", "event": "llm_request",
           "request_id": "req-new", "model": "pi-model",
           "request": {"message_count": 1}}
    with (traces / "mixed-20260930.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(new) + "\n")
    on, off = both_ways(["ls", "--limit", "5", *dirs], idx)
    check("appended record is visible", on.stdout == off.stdout,
          f"--- on ---\n{on.stdout}\n--- off ---\n{off.stdout}")
    check("appended record is the newest", "23:00:00" in on.stdout, on.stdout)

    print("7. truncation invalidates offsets instead of reading stale bytes")
    path = traces / "mixed-20260930.jsonl"
    kept = path.read_text(encoding="utf-8").splitlines(True)[:12]
    path.write_text("".join(kept), encoding="utf-8")
    on, off = both_ways(["ls", *dirs], idx)
    check("truncated file gives identical results", on.stdout == off.stdout,
          f"--- on ---\n{on.stdout[:400]}\n--- off ---\n{off.stdout[:400]}")
    check("truncated file no longer offers the dropped rows",
          "23:00:00" not in on.stdout, on.stdout[:400])

    print("8. a corrupt index is rebuilt, never trusted")
    for state in idx.glob("*.json"):
        state.write_text("{ this is not json", encoding="utf-8")
    on, off = both_ways(["ls", *dirs], idx)
    check("corrupt state -> identical results", on.stdout == off.stdout,
          f"--- on ---\n{on.stdout[:400]}\n--- off ---\n{off.stdout[:400]}")
    check("corrupt state -> recovered, not an error", on.returncode == 0,
          on.stderr[-400:])

    print("9. an unwritable cache dir falls back to scanning bodies")
    locked = tmp / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        p = run(["ls", "--limit", "5", *dirs], locked)
        check("read-only cache still answers", p.returncode == 0, p.stderr[-400:])
        p0 = run(["ls", "--limit", "5", *dirs], locked, AGENTTRACE_INDEX="0")
        check("same answer as a full scan", p.stdout == p0.stdout, p.stdout[:300])
    finally:
        os.chmod(locked, 0o700)

    print("10. the index is actually faster")
    big = tmp / "big"
    big.mkdir()
    filler = "x" * 150_000
    lines = [json.dumps({
        "v": 1, "ts": f"2026-09-30T{i // 60:02d}:{i % 60:02d}:00.000Z",
        "agent": "pi", "event": "llm_response", "session_id": "s",
        "request_id": f"r{i}", "model": "m", "duration_ms": 100,
        "response": {"content": filler,
                     "usage": {"input_tokens": 100, "output_tokens": 5}},
    }) for i in range(300)]
    big_file = big / "pi-20260930.jsonl"
    big_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    size = big_file.stat().st_size
    big_idx = tmp / "big-cache"

    t0 = time.perf_counter()
    off = run(["stats", str(big)], big_idx, AGENTTRACE_INDEX="0")
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    run(["stats", str(big)], big_idx)          # builds the index
    build = time.perf_counter() - t0
    t0 = time.perf_counter()
    warm = run(["stats", str(big)], big_idx)
    warm_s = time.perf_counter() - t0

    print(f"       {size / 1e6:.0f} MB · full scan {cold:.2f}s · "
          f"index build {build:.2f}s · warm {warm_s:.2f}s")
    check("warm index output == full scan", warm.stdout == off.stdout,
          f"--- warm ---\n{warm.stdout}\n--- scan ---\n{off.stdout}")
    check("warm index is faster than a full scan", warm_s < cold,
          f"warm {warm_s:.2f}s vs scan {cold:.2f}s")
    index_bytes = sum(p.stat().st_size for p in big_idx.glob("*.json"))
    check("index is far smaller than the data it indexes",
          index_bytes < size / 50, f"{index_bytes} vs {size}")

    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print(f"\nFAILED: {', '.join(fails)}", file=sys.stderr)
        return 1
    print("PASS — index and full scan agree everywhere, and the index is faster")
    return 0


if __name__ == "__main__":
    sys.exit(main())
