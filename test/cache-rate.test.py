#!/usr/bin/env python3
"""Cache hit rate must never exceed 100%.

The bug: two incompatible meanings of `input_tokens` were reaching the same
reader.

  * hermes writes OpenAI's `prompt_tokens` — the TOTAL prompt, cache included,
    so `cache_read_tokens` is a subset of it.
  * the Pi adapter wrote pi-ai's `usage.input`, which pi-ai computes as
    `promptTokens - cacheRead - cacheWrite` (openai-completions.js:1171,
    mistral-conversations.js:431, anthropic-messages.js:423) — i.e. the part
    the cache did NOT serve.

So `cache_read / input_tokens` could run away: measured on this machine, 157 of
2050 llm_response records (7.7%, all from `pi`) read >100%, the worst single
record was 420 input vs 768 cache-read (182.9%), and the per-session aggregate
came out at 2703.86%.

Fixed on both ends:
  * pi/agent-trace.ts now writes `input + cacheRead + cacheWrite`.
  * the reader repairs records already on disk (they cannot be rewritten) and
    clamps the rendered rate.

This test pins both halves, and proves the on-disk history still renders sanely.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "common"))
sys.path.insert(0, str(ROOT / "cli"))

import agenttrace_common as common  # noqa: E402
from agenttrace_watch import Formatter, session_stats, strip_ansi  # noqa: E402

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


# A record exactly as the buggy Pi adapter wrote it: `input_tokens` excludes
# the cache. This pair is lifted from the real traces on this machine.
LEGACY_PI = {
    "v": 1,
    "ts": "2026-09-27T10:00:00.000Z",
    "agent": "pi",
    "event": "llm_response",
    "session_id": "sess-pi",
    "model": "kimi-k2.6",
    "response": {
        "content": "ok",
        "usage": {
            "input_tokens": 420,
            "output_tokens": 34,
            "cache_read_tokens": 768,
            "cache_write_tokens": 0,
        },
    },
    "duration_ms": 1500,
}

# What hermes writes: `input_tokens` already includes the cache.
HERMES = {
    "v": 1,
    "ts": "2026-09-27T10:00:01.000Z",
    "agent": "hermes",
    "event": "llm_response",
    "session_id": "sess-hermes",
    "model": "space-bunny-free",
    "response": {
        "content": "391",
        "usage": {"input_tokens": 13318, "output_tokens": 2,
                  "cache_read_tokens": 13298},
    },
    "duration_ms": 1500,
}

PCT = re.compile(r"(\d+(?:\.\d+)?)%")


def percentages(text: str) -> list[float]:
    return [float(m) for m in PCT.findall(text)]


def main() -> int:
    legacy = LEGACY_PI["response"]["usage"]
    hermes = HERMES["response"]["usage"]

    print("1. denominator repair (common.total_input_tokens)")
    check("legacy pi: 420 + 768 + 0 -> 1188",
          common.total_input_tokens(legacy) == 1188,
          repr(common.total_input_tokens(legacy)))
    check("hermes: inclusive input untouched",
          common.total_input_tokens(hermes) == 13318,
          repr(common.total_input_tokens(hermes)))
    check("missing usage -> 0", common.total_input_tokens({}) == 0)
    check("non-dict usage -> 0", common.total_input_tokens(None) == 0)
    check("absurd cache_write folded in when exclusive",
          common.total_input_tokens(
              {"input_tokens": 10, "cache_read_tokens": 500,
               "cache_write_tokens": 30}) == 540)

    print("2. the rate itself (common.cache_hit_rate)")
    check("legacy pi rate is 64.7%, not 182.9%",
          round(common.cache_hit_rate(768, common.total_input_tokens(legacy)), 1) == 64.6,
          repr(common.cache_hit_rate(768, common.total_input_tokens(legacy))))
    check("hermes rate unchanged (99.8%)",
          round(common.cache_hit_rate(13298, 13318), 1) == 99.8,
          repr(common.cache_hit_rate(13298, 13318)))
    check("zero denominator -> None", common.cache_hit_rate(10, 0) is None)
    check("clamp: 5000/100 cannot render as 5000%",
          common.cache_hit_rate(5000, 100) == 100.0,
          repr(common.cache_hit_rate(5000, 100)))
    check("no cache at all -> 0.0, not None",
          common.cache_hit_rate(0, 1000) == 0.0,
          repr(common.cache_hit_rate(0, 1000)))

    print("3. the spend row (panel.stats.session_stats)")
    for name, rec in (("legacy pi", LEGACY_PI), ("hermes", HERMES)):
        line = strip_ansi(session_stats([rec]) or "")
        rates = [p for p in percentages(line)]
        check(f"{name}: renders a rate", bool(rates), line)
        check(f"{name}: every rate <= 100", all(p <= 100 for p in rates), line)
    # The whole point: the legacy record must not come out at 182.9%.
    line = strip_ansi(session_stats([LEGACY_PI]) or "")
    check("legacy pi renders ~64.6%",
          any(abs(p - 64.6) < 0.1 for p in percentages(line)), line)
    # Aggregated with itself the figure must stay put, not compound.
    agg = strip_ansi(session_stats([LEGACY_PI] * 5) or "")
    check("5 legacy records still ~64.6%",
          any(abs(p - 64.6) < 0.1 for p in percentages(agg)), agg)

    print("4. the reply meta line (panel.formatter.Formatter)")
    for name, rec in (("legacy pi", LEGACY_PI), ("hermes", HERMES)):
        rendered = strip_ansi("\n".join(Formatter(width=100).render(rec)))
        rates = [p for p in percentages(rendered)]
        check(f"{name}: meta renders a rate", bool(rates), rendered)
        check(f"{name}: every rate <= 100", all(p <= 100 for p in rates), rendered)
    rendered = strip_ansi("\n".join(Formatter(width=100).render(LEGACY_PI)))
    check("legacy pi reply renders ~64.6%",
          any(abs(p - 64.6) < 0.1 for p in percentages(rendered)), rendered)
    # `in` must show the repaired total, and tok = in + out must match it.
    check("legacy pi reports in 1188, not in 420", "in 1188" in rendered, rendered)
    check("legacy pi tok = 1188 + 34 = 1222", "tok 1222" in rendered, rendered)

    print("5. `agenttrace stats` end to end")
    with tempfile.TemporaryDirectory(prefix="cache-rate-") as tmp:
        (Path(tmp) / "pi-20260927.jsonl").write_text(
            json.dumps(LEGACY_PI) + "\n" + json.dumps(HERMES) + "\n",
            encoding="utf-8")
        p = subprocess.run(
            [sys.executable, str(ROOT / "cli" / "agenttrace.py"), "stats", tmp],
            capture_output=True, text=True)
        check("stats exits 0", p.returncode == 0, p.stderr[-500:])
        # The table is fixed-width (`{'in_tok':>10s}`), so read the column by
        # its header offset rather than by splitting — a model name containing
        # a space would shift every index after it.
        lines = [l for l in p.stdout.splitlines() if l.strip()]
        header, rows = lines[0], lines[1:]
        col = header.index("in_tok")
        in_tok = {l[:8].strip(): l[col:col + 10].strip() for l in rows}
        check("stats in_tok for pi is the repaired 1188",
              in_tok.get("pi") == "1188", str(in_tok))
        check("stats in_tok for hermes is unchanged 13318",
              in_tok.get("hermes") == "13318", str(in_tok))

    if fails:
        print(f"\nFAILED: {', '.join(fails)}", file=sys.stderr)
        return 1
    print("PASS — cache hit rate stays within 0-100% for both conventions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
