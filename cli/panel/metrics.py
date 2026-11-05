"""agenttrace watch — the numbers that say whether a run is healthy.

Split out like the rest of `panel/`: pure functions over the records already in
scope, plus one small cached reader for the index's own health. Nothing here
touches a terminal, so its tests need neither a pty nor a database.

Two rules, both learned from this project's own review rounds:

* **No invented numbers.** A percentile is nearest-rank and carries its `n`;
  with three samples there is no p95, and the renderer shows the max instead.
  Cost is `None` when any call's model has no configured price, and the panel
  says "unpriced" rather than adding a confident $0.
* **Work is not wall clock.** A turn's wall time includes the human reading the
  diff and the process being gone. `work_ms` sums the durations the records
  actually measured, so a session that sat open overnight cannot read as busy.
  Same definitions as `agenttrace session` (D25); the difference is only the
  source — this one reads the live record stream, that one the index.
"""

from __future__ import annotations

import copy
import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from panel.text import (C_AI, C_BOLD, C_DIM, C_MAGENTA, C_RED, C_RESET,
                        C_YELLOW, _bar, _fmt_tok, clip_plain, sane,
                        strip_ansi, vlen)

from agenttrace_common import cache_hit_rate, total_input_tokens
from core.convert.v1_to_v2 import EpisodeTracker, _digest
from core.pricing import cost_usd

#: Below this many samples a percentile is a lie dressed as statistics.
P95_MIN_SAMPLES = 5
_BLOCKS = "▁▂▃▄▅▆▇█"


# --------------------------------------------------------------------------
# small numeric helpers
# --------------------------------------------------------------------------

def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ms(value: Any) -> float | None:
    """A duration in ms, or None when the record did not measure one.

    `0` is treated as reported-but-unusable: a call that reports 0ms did not
    answer instantly, it failed to record a duration, and averaging it in drags
    the median toward zero.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _ts_ms(ts: Any) -> float | None:
    """Epoch milliseconds from a record's ISO-8601 `ts` (always UTC, ends in Z).

    Sliced, not `strptime`: that call cost 4.3µs and was the whole cost of
    building the panel's numbers (43ms of 55ms over 10k records). The format is
    fixed by the schema, and a malformed value returns None rather than raising
    inside a render loop.
    """
    if not isinstance(ts, str) or len(ts) < 19:
        return None
    try:
        y, mo, d, h, mi, sec = (int(ts[0:4]), int(ts[5:7]), int(ts[8:10]),
                                int(ts[11:13]), int(ts[14:16]), int(ts[17:19]))
        frac = 0
        if len(ts) > 20 and ts[19] == ".":
            tail = ts[20:].rstrip("Z")
            if tail:
                frac = int((tail + "000000")[:6])
    except (ValueError, IndexError):
        return None
    try:
        from datetime import datetime, timezone
        return datetime(y, mo, d, h, mi, sec, frac,
                        tzinfo=timezone.utc).timestamp() * 1000.0
    except ValueError:          # e.g. month 13 in a corrupt record
        return None


def percentiles(values: Iterable[float]) -> dict[str, float] | None:
    """p50/p95/max by nearest rank, plus the sample count.

    Nearest rank (not interpolation): an interpolated p95 over five measurements
    reports a duration that never happened, and this panel is read to decide
    whether something is slow. `n` is returned so callers can decline to show a
    p95 that the sample cannot support.
    """
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)

    def rank(q: float) -> float:
        """Nearest rank: the ceil(q*n)-th smallest value, 1-indexed."""
        idx = min(n - 1, max(0, math.ceil(q * n) - 1))
        return vals[idx]

    return {"p50": rank(0.50), "p95": rank(0.95), "max": vals[-1],
            "min": vals[0], "n": float(n)}


def sparkline(values: list[float | None], width: int) -> str:
    """A block sparkline, newest last, scaled to its own maximum."""
    if not values or width <= 0:
        return ""
    window = values[-width:]
    known = [v for v in window if v is not None]
    if not known:
        return " " * len(window)
    top = max(known) or 1.0
    out = []
    for v in window:
        if v is None:
            out.append(" ")
            continue
        level = int(round((v / top) * (len(_BLOCKS) - 1)))
        out.append(_BLOCKS[max(0, min(level, len(_BLOCKS) - 1))])
    return "".join(out)


def human_ms(ms: float | None) -> str:
    """A duration at a width that stays comparable in a column."""
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes = ms / 60_000
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{minutes / 60:.1f}h"


# --------------------------------------------------------------------------
# what the records say
# --------------------------------------------------------------------------

@dataclass
class Turn:
    """One conversation turn, measured the way `agenttrace session` measures it."""

    index: int
    #: the `turn_id` this block was cut on (None for a block of untagged records)
    #: (episode, turn_id): the same identity the index mints a task for
    key: object = None
    first_ts: str | None = None
    last_ts: str | None = None
    wall_ms: float | None = None
    work_ms: float = 0.0
    calls: int = 0
    tools: int = 0
    errors: int = 0
    models: list[str] = field(default_factory=list)
    open: bool = False
    #: another turn starts inside this one's window. Structural, not a
    #: threshold — the same test `agenttrace session` uses (D25) — and it means
    #: this turn's wall clock is partly a gap, so it is excluded from the totals.
    interrupted: bool = False

    @property
    def outside_ms(self) -> float:
        # Never negative: source clocks are not guaranteed monotone, and a
        # negative "outside" reads as a bug in the agent, not the clock.
        if self.wall_ms is None:
            return 0.0
        return max(0.0, self.wall_ms - self.work_ms)

    @property
    def work_share(self) -> float | None:
        if not self.wall_ms:
            return None
        return min(1.0, self.work_ms / self.wall_ms)


@dataclass
class Metrics:
    events: int = 0
    calls: int = 0
    llm_ms: list[float] = field(default_factory=list)
    tool_ms: list[float] = field(default_factory=list)
    tool_names: dict[str, float] = field(default_factory=dict)
    tool_failures: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_calls: int = 0
    tok_per_s: list[float] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    #: records skipped because an identical copy was already folded. The same
    #: session is written by a live adapter AND a backfill, so one conversation
    #: can appear several times on disk; the index dedups by event identity, and
    #: a panel that did not would report a turn count nobody can reconcile.
    duplicate_records: int = 0
    #: newest record timestamp, kept as a STRING (lexicographic == chronological)
    newest_ts: str | None = None
    #: first record to last: the session's window (see _finish)
    span_ms: float | None = None

    @property
    def newest_ts_ms(self) -> float | None:
        return _ts_ms(self.newest_ts)

    # -- derived ---------------------------------------------------------
    @property
    def latency(self) -> dict[str, float] | None:
        return percentiles(self.llm_ms)

    @property
    def tool_latency(self) -> dict[str, float] | None:
        return percentiles(self.tool_ms)

    @property
    def cache_rate(self) -> float | None:
        if not self.input_tokens:
            return None
        return cache_hit_rate(self.cached_tokens, self.input_tokens)

    @property
    def slowest_tool(self) -> tuple[str, float] | None:
        if not self.tool_names:
            return None
        name = max(self.tool_names, key=lambda k: self.tool_names[k])
        return name, self.tool_names[name]

    def slowest_tool_label(self) -> str | None:
        """The slowest tool's name, safe to put on a terminal.

        A tool name comes from trace data, i.e. from whatever an agent decided to
        call. `sane()` strips control and escape sequences; rendering it raw
        would let a trace clear the screen, retitle the window, or write to the
        clipboard through an OSC sequence.
        """
        slow = self.slowest_tool
        if not slow:
            return None
        # sane() keeps tab and newline (prose fields need them); a one-line
        # metric must not gain a line break from a tool name.
        return " ".join(sane(slow[0]).split())

    @property
    def trusted_turns(self) -> list[Turn]:
        """Turns whose wall clock is not partly a gap (see Turn.interrupted)."""
        return [t for t in self.turns if not t.interrupted]

    @property
    def total_work_ms(self) -> float:
        """Measured work. A floor, not an estimate: only measured spans count."""
        return sum(t.work_ms for t in self.trusted_turns)

    @property
    def total_wall_ms(self) -> float:
        """The session's own envelope, first record to last.

        NOT the sum of per-turn spans: they can overlap, and summing them
        reported a 14x longer wall than the session itself.
        """
        return self.span_ms or 0.0

    @property
    def outside_ms(self) -> float:
        return max(0.0, self.total_wall_ms - self.total_work_ms)

    @property
    def work_share(self) -> float | None:
        """Share of the session window spent working, capped at 1.

        Reported durations can add up to more than the timestamps span (clock
        rounding, a resumed clock). The cap keeps the bar honest; the caller
        shows a ⚠ next to it rather than pretending the parts fit.
        """
        if not self.total_wall_ms:
            return None
        return min(1.0, self.total_work_ms / self.total_wall_ms)

    @property
    def work_exceeds_wall(self) -> bool:
        return self.total_work_ms > self.total_wall_ms + 50

    def cost_text(self) -> str:
        """`$0.42`, or an explicit "unknown" — never a confident zero."""
        if not self.calls:
            return "—"
        priced_calls = self.calls - self.unpriced_calls
        if priced_calls <= 0:
            return "unknown"
        text = f"${self.cost_usd:.2f}" if self.cost_usd >= 0.01 else \
            f"${self.cost_usd:.4f}"
        if self.unpriced_calls:
            # A partial sum labelled as a total is the same lie as a fabricated
            # number: say how much of the bill is missing from it.
            text += f" +{self.unpriced_calls} unpriced"
        return text


def _fold(m: Metrics, turns: dict[tuple[Any, Any], Turn],
          tracker: EpisodeTracker, rec: dict[str, Any],
          seen: set[str] | None = None) -> None:
    """Fold ONE record into the metrics. The only place a record is interpreted.

    Timestamps stay STRINGS here: the format is fixed and lexicographic order is
    chronological order, so the per-turn wall clock needs two conversions per
    turn rather than one per record.

    Turns are cut SEQUENTIALLY, from the record stream, not by grouping on
    `turn_id`. Adapters restart their turn counter (a new run, a compaction, a
    resumed process), so one session legitimately contains several turns called
    "0". Grouping by key merged them: on a real 550-record session it produced 95
    turns whose windows overlapped 1,462 ways, a "wall" of 45,710s against a
    session span of 3,257s, and a work share of 7% where the truth was 99%.
    """
    if seen is not None and rec.get("v") == 1:
        digest = _digest(rec)
        if digest in seen:
            m.duplicate_records += 1
            return
        seen.add(digest)
    m.events += 1
    ts = rec.get("ts")
    if isinstance(ts, str) and (m.newest_ts is None or ts > m.newest_ts):
        m.newest_ts = ts

    # The identity the INDEX uses: (episode, turn_id). Reading only `turn_id`
    # made the panel report 3,417 turns for a session the index called 591 —
    # same records, a different definition of "turn", two numbers nobody can
    # reconcile.
    episode = tracker.step(rec)
    turn_id = rec.get("turn_id")
    key = (episode, turn_id)
    turn = turns.get(key)
    if turn is None:
        # Turns are keyed by the IDENTITY the index mints a task for, not by
        # adjacency: a session split across daily files visits the same turn id
        # in several files, and cutting on adjacency produced 639 turns where the
        # index has 598 tasks for the same records — a number nobody could
        # reconcile with any other surface.
        turn = Turn(index=len(turns) + 1, key=key)
        turns[key] = turn
    if isinstance(ts, str):
        if turn.first_ts is None or ts < turn.first_ts:
            turn.first_ts = ts
        if turn.last_ts is None or ts > turn.last_ts:
            turn.last_ts = ts

    model = rec.get("model")
    if isinstance(model, str) and model and model not in turn.models:
        turn.models.append(model)

    ev = rec.get("event")
    if ev == "llm_response":
        m.calls += 1
        turn.calls += 1
        usage = (rec.get("response") or {}).get("usage") or {}
        in_tok = total_input_tokens(usage)
        out_tok = _int(usage.get("output_tokens"))
        cached = _int(usage.get("cache_read_tokens"))
        m.input_tokens += in_tok
        m.output_tokens += out_tok
        m.cached_tokens += cached
        value = cost_usd(model if isinstance(model, str) else None, in_tok,
                         out_tok, cached)
        if value is None:
            m.unpriced_calls += 1
        else:
            m.cost_usd += value
        latency = _ms(rec.get("duration_ms"))
        if latency is not None:
            m.llm_ms.append(latency)
            turn.work_ms += latency
            if out_tok:
                m.tok_per_s.append(out_tok / (latency / 1000.0))
        # No measured duration means the throughput is unknown; appending
        # nothing keeps the median honest instead of pulling it to zero.
    elif ev == "tool_result":
        turn.tools += 1
        tool = rec.get("tool") or {}
        name = tool.get("name") if isinstance(tool, dict) else None
        duration = _ms(rec.get("duration_ms"))
        if duration is not None:
            m.tool_ms.append(duration)
            turn.work_ms += duration
            if isinstance(name, str):
                m.tool_names[name] = max(m.tool_names.get(name, 0.0), duration)
        status = tool.get("status") if isinstance(tool, dict) else None
        exit_code = tool.get("exit_code") if isinstance(tool, dict) else None
        failed = status in ("error", "failed", "timeout") or \
            (isinstance(exit_code, int) and exit_code != 0)
        if failed:
            m.tool_failures += 1
            turn.errors += 1
    elif ev in ("llm_error", "error"):
        m.errors += 1
        turn.errors += 1


def _finish(m: Metrics, turns: dict[tuple[Any, Any], Turn]) -> Metrics:
    """Derive the per-turn wall clock and the open marker. O(turns), not O(events).

    Rebuilt on every call so the result is a pure function of the folded
    records: an incremental caller must be able to ask for the numbers again
    without the counters drifting.
    """
    m.turns = []
    for turn in turns.values():
        turn.wall_ms = None
        if turn.first_ts and turn.last_ts:
            start, end = _ts_ms(turn.first_ts), _ts_ms(turn.last_ts)
            if start is not None and end is not None and end >= start:
                turn.wall_ms = end - start
        turn.open = False
        turn.interrupted = False
        m.turns.append(turn)
    if m.turns:
        # The turn that started most recently is still running: its wall clock
        # grows as you watch, and presenting it as finished makes "outside"
        # meaningless.
        newest = max(m.turns, key=lambda t: t.first_ts or "")
        newest.open = True
    starts = [t.first_ts for t in turns.values() if t.first_ts]
    for turn in turns.values():
        begin, finish = turn.first_ts, turn.last_ts
        if begin and finish:
            turn.interrupted = any(begin < other < finish for other in starts)

    # One envelope for the session, not the SUM of per-turn spans: spans can
    # overlap (a resumed run, a late record), and summing them double-counts the
    # overlap — it read 45,710s against a 3,257s session on real data.
    firsts = [t.first_ts for t in turns.values() if t.first_ts]
    lasts = [t.last_ts for t in turns.values() if t.last_ts]
    m.span_ms = None
    if firsts and lasts:
        a, b = _ts_ms(min(firsts)), _ts_ms(max(lasts))
        if a is not None and b is not None and b >= a:
            m.span_ms = b - a
    return m


def collect(recs: Iterable[dict[str, Any]]) -> Metrics:
    """Metrics for a whole window, from scratch."""
    m = Metrics()
    turns: dict[tuple[Any, Any], Turn] = {}
    tracker = EpisodeTracker()
    seen: set[str] = set()
    for rec in recs:
        _fold(m, turns, tracker, rec, seen)
    return _finish(m, turns)


class Accumulator:
    """Metrics that survive a redraw, so an appended record costs O(1).

    The panel repaints several times a second and the window can hold thousands
    of records; re-walking it each time cost 55ms per pass at 10k records (more
    than two cores at 4Hz). The window is append-only in the common case, so the
    prefix is left folded and only the new tail is visited.

    Correctness rests on the caller's `window_key`: anything that changes the
    COMPOSITION of the window (the scoped session, the filter text) must be part
    of it. Head identity alone is not enough, and the failure is silent: widening
    a filter inserts records in the MIDDLE of the window, so the first record is
    unchanged and the length grows — indistinguishable from an append. The fold
    then re-counts the tail and never sees the inserted records (measured: 21,010
    tokens where the truth was 11,110, with the call COUNT coincidentally right).
    """

    def __init__(self) -> None:
        self._m = Metrics()
        self._turns: dict[tuple[Any, Any], Turn] = {}
        self._tracker = EpisodeTracker()
        self._seen: set[str] = set()
        self._head: dict[str, Any] | None = None
        self._key: object = None
        self._n = 0
        self.rebuilds = 0

    def _reset(self) -> None:
        self._m = Metrics()
        self._turns = {}
        self._tracker = EpisodeTracker()
        self._seen = set()
        self._n = 0
        self.rebuilds += 1

    def fold(self, recs: list[dict[str, Any]], *, window_key: object = None) -> Metrics:
        """Return the metrics for `recs`.

        `window_key` identifies the window's COMPOSITION (session + filter). A
        change to it, to the first record, or a shrinking window all restart the
        fold; anything else is treated as an append.

        The result is a SHALLOW COPY of the accumulator's state: the scalar
        counters are frozen per call, so a caller that keeps one frame's metrics
        does not watch them change under it (the first test written for this
        class did exactly that and read a mutated total). The sample lists are
        shared and append-only, which is what keeps this O(turns) per frame
        instead of O(records).
        """
        if not recs:
            if self._head is not None:
                self._reset()
                self._head = None
                self._key = None
            return copy.copy(_finish(self._m, self._turns))
        if (self._head is None or self._key != window_key
                or recs[0] is not self._head or len(recs) < self._n):
            self._reset()
        self._head = recs[0]
        self._key = window_key
        for rec in recs[self._n:]:
            _fold(self._m, self._turns, self._tracker, rec, self._seen)
        self._n = len(recs)
        return copy.copy(_finish(self._m, self._turns))


# --------------------------------------------------------------------------
# the index's own health (cheap, cached, read-only)
# --------------------------------------------------------------------------

@dataclass
class Health:
    ok: bool = False
    events: int = 0
    spans: int = 0
    sessions: int = 0
    #: None means "could not tell" (an older schema without ingest_files) — a
    #: clean zero would claim the index is current when freshness is unknown.
    files_behind: int | None = 0
    db_bytes: int = 0
    error: str | None = None


#: One entry, not a dict keyed by path: the panel watches one index, and a
#: map here grew by one entry per distinct AGENTTRACE_DB a long-lived process
#: ever saw (measured: 55 entries after 50 paths) with no eviction.
_HEALTH_CACHE: tuple[str, float, Health] | None = None


def health(db_path: Path | str | None, *, ttl: float = 2.0) -> Health | None:
    """Read the projection's size and freshness, at most once every `ttl`.

    The panel repaints several times a second; querying SQLite at that rate
    would make the observer part of the load being observed. `ttl` keeps it at
    "a couple of times a second is plenty" while still following a live index.

    A busy database is reported as unavailable rather than waited on: the
    default 1s lock timeout froze the whole panel for a second whenever the
    indexer held a write transaction, which is precisely when a user is looking
    at the numbers.
    """
    if db_path is None:
        return None
    global _HEALTH_CACHE
    key = str(db_path)
    now = time.monotonic()
    cached = _HEALTH_CACHE
    if cached and cached[0] == key and now - cached[1] < ttl:
        return cached[2]
    result = Health()
    path = Path(db_path)
    try:
        result.db_bytes = path.stat().st_size
        # 50ms, not 1000: a locked index must not stall a repaint. A false
        # "unavailable" is cheap (it is cached for the ttl and the next pass
        # retries), a one-second freeze is not.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.05)
        try:
            result.events = int(conn.execute(
                "SELECT COUNT(*) FROM events").fetchone()[0])
            result.spans = int(conn.execute(
                "SELECT COUNT(*) FROM spans").fetchone()[0])
            result.sessions = int(conn.execute(
                "SELECT COUNT(*) FROM sessions").fetchone()[0])
            # One aggregate, no per-row syscalls: the panel is on a repaint
            # thread, and Path.exists() per row cost 354ms with 100k rows. A row
            # for a deleted file counts as behind until the next scan drops it
            # (Indexer._drop_vanished), which is the honest reading anyway: the
            # projection still holds rows the archive no longer has.
            try:
                result.files_behind = int(conn.execute(
                    "SELECT COUNT(*) FROM ingest_files"
                    " WHERE size > indexed_bytes").fetchone()[0])
            except sqlite3.Error:
                result.files_behind = None
            result.ok = True
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        result.error = str(exc)
        result.ok = False
    _HEALTH_CACHE = (key, now, result)
    return result


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _pct_bar(pct: float, cells: int = 8) -> str:
    return _bar(pct, cells)


def _wrap(text: str, width: int, *, indent: str = "", colour: str = "") -> list[str]:
    """Wrap prose to the terminal, so an explanation is never cut mid-word."""
    import textwrap
    body_w = max(12, width - len(indent))
    lines = textwrap.wrap(text, body_w) or [text]
    return [f"{indent}{colour}{ln}{C_RESET}" for ln in lines]


def _label(text: str, width: int) -> str:
    line = f" {C_BOLD}{text}{C_RESET}"
    return clip_plain(line, width)


def render_line(m: Metrics, width: int) -> str | None:
    """The always-on row: is it fast, is it spending, is it keeping up.

    Record-derived figures only. Anything that changes with the CLOCK (ingest
    lag, "N seconds ago") lives in the `m` view: a row that repaints on a timer
    would make the idle panel write to the terminal every second, which is the
    flicker regression the pty test forbids.

    Segments drop from the right as the terminal narrows, in reverse order of
    importance. A hard clip mid-number is worse than a missing segment: `p95 1.`
    reads as a measurement.
    """
    bits: list[str] = []
    lat = m.latency
    if lat:
        hue = C_AI if lat["p50"] < 4000 else (C_YELLOW if lat["p50"] < 15000 else C_RED)
        seg = f"{C_DIM}lat {hue}{human_ms(lat['p50'])}{C_RESET}"
        if lat["n"] >= P95_MIN_SAMPLES:
            seg += f"{C_DIM}/{human_ms(lat['p95'])}{C_RESET}"
        else:
            seg += f"{C_DIM} (max {human_ms(lat['max'])}, n={int(lat['n'])}){C_RESET}"
        bits.append(seg)
    if m.tok_per_s:
        med = percentiles(m.tok_per_s)["p50"]
        bits.append(f"{C_DIM}{med:.0f} tok/s{C_RESET}")
    # Tokens and cache rate are the stats row's job (session_stats); repeating
    # them here spent the width that latency and cost needed.
    cost = m.cost_text()
    if cost != "—":
        bits.append(f"{C_DIM}{C_BOLD}{cost}{C_RESET}")
    if m.tool_ms:
        tl = percentiles(m.tool_ms)
        seg = f"{C_YELLOW}tools {int(tl['n'])}{C_RESET}{C_DIM} p50 {human_ms(tl['p50'])}{C_RESET}"
        if tl["n"] >= P95_MIN_SAMPLES:
            seg += f"{C_DIM} p95 {human_ms(tl['p95'])}{C_RESET}"
        if m.tool_failures:
            seg += f" {C_RED}{m.tool_failures}✗{C_RESET}"
        bits.append(seg)
    if m.turns:
        share = m.work_share
        if share is not None:
            hue = C_AI if share >= 0.5 else C_YELLOW
            warn = f"{C_YELLOW}⚠{C_RESET}" if m.work_exceeds_wall else ""
            bits.append(f"{C_DIM}turns {len(m.turns)} {hue}{share * 100:.0f}%"
                        f"{C_DIM} work{C_RESET}{warn}")
        else:
            bits.append(f"{C_DIM}turns {len(m.turns)}{C_RESET}")
    while bits:
        line = f"  {C_DIM}" + " · ".join(bits) + C_RESET
        if vlen(strip_ansi(line)) <= width:
            return line
        bits.pop()
    return None


def _turn_rows(m: Metrics, width: int, limit: int) -> list[str]:
    """One row per turn, fitted by dropping whole segments.

    `clip_plain` cuts at the terminal edge, which is how a live pane showed
    "· calls 0 · t" — a label with no number. The bar shrinks first, then the
    trailing counters drop, and `work`/`wall` (the reason the row exists) are the
    last thing to go.
    """
    rows: list[str] = []
    # A turn with no measured activity and no duration is an artifact of the
    # record stream, not something a reader can act on.
    shown = [t for t in m.turns if t.calls or t.tools or t.work_ms or t.wall_ms]
    if not shown:
        shown = m.turns[-limit:] if m.turns else []
    for turn in shown[-limit:]:
        share = turn.work_share
        pct = "  —" if share is None else f"{share * 100:3.0f}%"
        open_mark = f"{C_MAGENTA}▸{C_RESET}" if turn.open else " "
        over = turn.wall_ms is not None and turn.work_ms > turn.wall_ms + 50
        wall_txt = ("≥" if over else "") + human_ms(turn.wall_ms)
        badge = f" {C_RED}{turn.errors}✗{C_RESET}" if turn.errors else ""
        lead = f" {open_mark}{C_BOLD}#{turn.index}{C_RESET}"
        parts = [
            f" {C_DIM}work {human_ms(turn.work_ms)} · wall "
            f"{wall_txt}{C_RESET}",
            f"{C_DIM} · outside {human_ms(turn.outside_ms)}{C_RESET}",
            f"{C_DIM} · calls {turn.calls}{C_RESET}",
            f"{C_DIM} · tools {turn.tools}{C_RESET}",
        ]
        line = ""
        for span in (28, 22, 16, 12, 8, 5):
            if share is None:
                bar = "·" * span
            else:
                filled = int(round(share * span))
                bar = "█" * filled + "░" * (span - filled)
            line = f"{lead} {bar} {pct}{parts[0]}"
            for part in parts[1:]:
                if vlen(strip_ansi(line + part + badge)) <= width:
                    line += part
            if vlen(strip_ansi(line + badge)) <= width:
                break
        rows.append(clip_plain(line + badge, width))
    return rows


def render_overlay(m: Metrics, health_: Health | None, width: int,
                   height: int) -> list[str]:
    """The `m` view: every measured number, in priority order.

    Assembled as whole sections so a short terminal loses the LAST section
    rather than half of one, and clipped to `height`: the caller pads the body
    to the screen, so an over-long list would wrap and push the footer off.
    """
    # 20, not 40: laying out a 20-column terminal as 40 (and clipping to 39)
    # makes the terminal soft-wrap every row, which pushes the footer off the
    # screen — worse than dropping segments.
    w = max(20, width - 1)
    if w < 32:
        # Below this, every number would be clipped mid-digit, and `p50 1.5` is
        # not a smaller measurement — it is a wrong one. Say so instead.
        # The word "narrow" has to survive the clip at 20 columns, or the
        # message itself becomes the truncated-text bug it warns about.
        msg = (" terminal too narrow for metrics — widen to 32+"
               if w >= 26 else " too narrow for metrics")
        return [clip_plain(f"{C_DIM}{msg}{C_RESET}", w)]

    perf: list[str] = [_label("PERFORMANCE", w)]
    lat = m.latency
    if lat:
        p95 = (f"p95 {human_ms(lat['p95'])}"
               if lat["n"] >= P95_MIN_SAMPLES
               else f"n={int(lat['n'])} — p95 needs {P95_MIN_SAMPLES}")
        perf.append(f"  {C_DIM}calls{C_RESET} {C_BOLD}{m.calls}{C_RESET}"
                    f"{C_DIM} · p50 {C_RESET}{C_BOLD}{human_ms(lat['p50'])}{C_RESET}"
                    f"{C_DIM} · {p95} · max {human_ms(lat['max'])}{C_RESET}")
        spark_w = min(48, max(8, w - 12))
        spark = sparkline(m.llm_ms[-spark_w:], spark_w)
        if spark:
            perf.append(f"  {C_DIM}latency{C_RESET} {C_AI}{spark}{C_RESET}"
                        f"{C_DIM} newest right{C_RESET}")
    else:
        perf.append(f"  {C_DIM}no completed call yet{C_RESET}")
    if m.tok_per_s:
        tps = percentiles(m.tok_per_s)
        perf.append(f"  {C_DIM}throughput{C_RESET} {C_BOLD}{tps['p50']:.0f}{C_RESET}"
                    f"{C_DIM} tok/s median · max {tps['max']:.0f}{C_RESET}")
    if m.errors or m.tool_failures:
        perf.append(f"  {C_RED}errors{C_RESET} {C_BOLD}{m.errors}{C_RESET}"
                    f"{C_DIM} llm · {C_RESET}{C_BOLD}{m.tool_failures}{C_RESET}"
                    f"{C_DIM} tool{C_RESET}")

    tools: list[str] = ["", _label("TOOLS", w)]
    tl = m.tool_latency
    if tl:
        p95 = (f" · p95 {human_ms(tl['p95'])}" if tl["n"] >= P95_MIN_SAMPLES else "")
        slow = m.slowest_tool
        label = m.slowest_tool_label()
        slow_txt = f" · slowest {label} {human_ms(slow[1])}" if slow else ""
        tools.append(f"  {C_YELLOW}{int(tl['n'])}{C_RESET}{C_DIM} calls · p50 "
                     f"{human_ms(tl['p50'])}{p95} · max {human_ms(tl['max'])}"
                     f"{slow_txt}{C_RESET}")
    else:
        tools.append(f"  {C_DIM}no tool calls in this session{C_RESET}")

    turn_rows = _turn_rows(m, w, 8)
    turns: list[str] = ["", _label("TURNS", w)] + turn_rows
    if m.work_exceeds_wall:
        turns.extend(_wrap(
            "⚠ measured work exceeds the session span: durations were reported for "
            "time the records' timestamps do not cover (clock rounding, or a "
            "resumed clock). The turn spans above are lower bounds.",
            w, indent="  ", colour=C_YELLOW))
    turns.append(f"  {C_DIM}counted from {m.events:,} record(s) in the trace files"
                 f"{' + ' + format(m.duplicate_records, ',') + ' duplicate copies folded' if m.duplicate_records else ''}"
                 f"; the index dedups by event id and can differ{C_RESET}")
    if m.turns:
        share = m.work_share
        turns.append(f"  {C_DIM}work {C_BOLD}{human_ms(m.total_work_ms)}{C_RESET}"
                     f"{C_DIM} · wall {human_ms(m.total_wall_ms)}"
                     f" · outside {human_ms(m.outside_ms)}"
                     + (f" · {share * 100:.0f}% work" if share is not None else "")
                     + C_RESET)

    tokens: list[str] = ["", _label("TOKENS & COST", w)]
    rate = m.cache_rate
    cache_txt = "—"
    if rate is not None:
        hue = C_AI if rate >= 80 else (C_YELLOW if rate >= 50 else C_RED)
        cache_txt = f"{hue}{rate:.1f}%{C_RESET} {hue}{_pct_bar(rate)}{C_RESET}"
    tokens.append(f"  {C_DIM}in {C_RESET}{C_BOLD}{_fmt_tok(m.input_tokens)}{C_RESET}"
                  f"{C_DIM} · out {C_RESET}{C_BOLD}{_fmt_tok(m.output_tokens)}{C_RESET}"
                  f"{C_DIM} · cache {cache_txt}")
    tokens.append(f"  {C_DIM}cost {C_RESET}{C_BOLD}{m.cost_text()}{C_RESET}"
                  f"{C_DIM}"
                  + (f" · {m.unpriced_calls}/{m.calls} calls have no configured price"
                     if m.unpriced_calls else "")
                  + C_RESET)

    ingest: list[str] = ["", _label("INGEST", w)]
    if health_ is None:
        ingest.append(f"  {C_DIM}index not checked{C_RESET}")
    elif not health_.ok:
        if health_.error and "No such file" in health_.error:
            ingest.append(f"  {C_DIM}no index yet — build it with "
                          f"`agenttrace index`{C_RESET}")
        else:
            ingest.append(f"  {C_YELLOW}index unreadable{C_RESET}"
                          f"{C_DIM} — {health_.error or 'unknown reason'}{C_RESET}")
    else:
        age = None
        if m.newest_ts_ms:
            age = max(0.0, time.time() - m.newest_ts_ms / 1000.0)
        behind = health_.files_behind
        behind_hue = C_AI if behind == 0 else C_YELLOW
        behind_txt = "?" if behind is None else str(behind)
        ingest.append(f"  {C_DIM}index {C_RESET}{C_BOLD}{health_.events:,}{C_RESET}"
                      f"{C_DIM} events · {health_.spans:,} spans · "
                      f"{health_.sessions:,} sessions{C_RESET}")
        ingest.append(f"  {C_DIM}{human_bytes(health_.db_bytes)} · "
                      f"{behind_hue}{behind_txt} file(s) behind{C_RESET}"
                      f"{C_DIM} · newest event "
                      + (f"{age:.0f}s ago" if age is not None else "—")
                      + C_RESET)

    out: list[str] = []
    for block in (perf, tools, turns, tokens, ingest):
        room = height - len(out)
        if room <= 0:
            break
        if len(block) <= room:
            out.extend(block)
            continue
        # Keep the heading and as many rows of THIS block as fit; a heading
        # without its numbers is just noise, so two rows is the minimum.
        if room >= 3:
            out.extend(block[:room])
        break
    return [clip_plain(r, w) for r in out[:max(1, height)]]


def human_bytes(n: int | float) -> str:
    value = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"        # pragma: no cover - unreachable
