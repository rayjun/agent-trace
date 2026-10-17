"""Trajectory evaluators — how the agent moved, not just where it landed.

These are the coding-agent differentiators (proposal §7.2/§8.2). Each emits
evidence + confidence, never a composite "efficiency score": repeated calls
aren't automatically waste (a retry can be the correct recovery), so the
score says how clean the trajectory looks and the evidence says WHERE to
look before disagreeing with it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from evaluation.model import Example, Score, TraceView

_KIND = "trajectory"


def _args_key(span: dict[str, Any]) -> str:
    args = (span.get("input") or {}).get("args")
    try:
        return hashlib.sha1(json.dumps(args, sort_keys=True,
                                       default=str).encode()).hexdigest()[:12]
    except (TypeError, ValueError):
        return "?"


class RetryLoops:
    """Same tool + same args, N+ consecutive times, all failing.

    Confidence scales with repetition: two failures are a retry, five are a
    loop. Passes cleanly when no such run exists.
    """

    evaluator_id = "traj:retry_loops"
    kind = _KIND
    version = "1"
    description = "identical failing tool calls repeated consecutively"

    def __init__(self, config: dict | None = None) -> None:
        self.threshold = int((config or {}).get("threshold", 3))

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        if not trace.has_activity:
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         ["trace has no spans or events"], confidence=0.0)
        loops: list[str] = []
        run: list[dict[str, Any]] = []
        for span in trace.tool_spans + [None]:  # sentinel flushes the tail
            key = (span and span.get("name"), span and _args_key(span))
            prev = (run[-1].get("name"), _args_key(run[-1])) if run else None
            if span is not None and key == prev:
                run.append(span)
            else:
                if (len(run) >= self.threshold
                        and all(s.get("status") == "error" for s in run)):
                    loops.append(
                        f"{run[0]['name']} x{len(run)} failing identically "
                        f"({run[0]['span_id']}..{run[-1]['span_id']})")
                run = [span] if span is not None else []
        if loops:
            return Score(self.evaluator_id, self.kind, 0.0, False, loops,
                         confidence=min(1.0, 0.4 + 0.2 * len(loops)))
        return Score(self.evaluator_id, self.kind, 1.0, True,
                     ["no identical failing call runs"])


class RepeatedCalls:
    """Same (tool, args) used many times anywhere in the trace."""

    evaluator_id = "traj:repeated_calls"
    kind = _KIND
    version = "1"
    description = "same tool with same args called repeatedly (non-consecutive too)"

    def __init__(self, config: dict | None = None) -> None:
        self.threshold = int((config or {}).get("threshold", 4))

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        counts: dict[tuple[str, str], list[str]] = {}
        for span in trace.tool_spans:
            key = (span.get("name") or "?", _args_key(span))
            counts.setdefault(key, []).append(span["span_id"])
        repeats = [f"{name} x{len(ids)} ({', '.join(ids[:4])})"
                   for (name, _), ids in counts.items()
                   if len(ids) >= self.threshold]
        if repeats:
            return Score(self.evaluator_id, self.kind, 0.5, False, repeats,
                         confidence=0.6)
        return Score(self.evaluator_id, self.kind, 1.0, True,
                     ["no tool+args combination repeated above threshold"])


class ErrorRecovery:
    """Of the tool errors that happened, how many were followed by a
    DIFFERENT approach that succeeded — the recovery signal, vs. flailing."""

    evaluator_id = "traj:error_recovery"
    kind = _KIND
    version = "1"
    description = "tool errors followed by a successful different call"

    def __init__(self, config: dict | None = None) -> None:
        pass  # uniform constructor for the registry factory; no knobs yet

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        tools = trace.tool_spans
        total = recovered = 0
        evidence: list[str] = []
        for i, span in enumerate(tools):
            if span.get("status") != "error":
                continue
            total += 1
            key = (span.get("name"), _args_key(span))
            for later in tools[i + 1:]:
                if later.get("status") == "ok" and (later.get("name"), _args_key(later)) != key:
                    recovered += 1
                    evidence.append(
                        f"{span['span_id']} failed → recovered by "
                        f"{later['span_id']} ({later.get('name')})")
                    break
            else:
                evidence.append(f"{span['span_id']} ({span.get('name')}): "
                                "no subsequent different approach succeeded")
        if total == 0:
            return Score(self.evaluator_id, self.kind, 1.0, True,
                         ["no tool errors to recover from"], confidence=0.8)
        score = recovered / total
        return Score(self.evaluator_id, self.kind, score, score >= 0.5,
                     evidence, confidence=0.9)


class ContextGrowth:
    """Input-token growth from first to last llm call. Growth is inherent
    (conversation accrues); the flag is growth without corresponding new
    spans — tokens spent, nothing done."""

    evaluator_id = "traj:context_growth"
    kind = _KIND
    version = "1"
    description = "input-token growth across llm calls vs. work done"

    def __init__(self, config: dict | None = None) -> None:
        self.ratio = float((config or {}).get("ratio", 8.0))

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        calls = [s for s in trace.llm_spans
                 if (s.get("tokens") or {}).get("input")]
        if len(calls) < 3:
            return Score(self.evaluator_id, self.kind, 1.0, True,
                         ["fewer than 3 llm calls — growth not meaningful"],
                         confidence=0.4)
        first, last = calls[0]["tokens"]["input"], calls[-1]["tokens"]["input"]
        if not first:
            return Score(self.evaluator_id, self.kind, 0.5, True,
                         ["first call has no token count"], confidence=0.2)
        ratio = last / first
        evidence = [f"input tokens {first} → {last} (x{ratio:.1f}) "
                    f"over {len(calls)} calls, {len(trace.tool_spans)} tool calls"]
        if ratio > self.ratio:
            return Score(self.evaluator_id, self.kind, 0.5, False, evidence,
                         confidence=0.5)
        return Score(self.evaluator_id, self.kind, 1.0, True, evidence)


class FirstActionLatency:
    """Time from trace start to the first NON-llm span — long thinking
    before touching anything is fine sometimes, but it's a visible cost."""

    evaluator_id = "traj:first_action_latency"
    kind = _KIND
    version = "1"
    description = "ms from trace start to first tool/subagent span"

    def __init__(self, config: dict | None = None) -> None:
        self.warn_ms = int((config or {}).get("warn_ms", 60_000))

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        from core.storage.sqlite_store import _duration_ms
        start = trace.trace.get("start")
        actions = [s for s in trace.spans
                   if s.get("kind") in ("tool", "agent") and s.get("start")]
        if not actions:
            # A trace where nothing was attempted at all is absent data, not a
            # negative verdict; a trace that talked but never acted is a real
            # (and important) negative.
            if not trace.llm_spans:
                return Score(self.evaluator_id, self.kind, 0.0, False,
                             ["no model calls or actions in trace — nothing "
                              "to measure"], confidence=0.0)
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         [f"no tool/subagent action after {len(trace.llm_spans)} "
                          "model call(s)"], confidence=0.8)
        first = min(actions, key=lambda s: s["start"])
        latency = _duration_ms(start, first["start"])
        evidence = [f"first action {first['span_id']} "
                    f"({first.get('name')}) after {latency}ms"]
        ok = latency is not None and latency <= self.warn_ms
        return Score(self.evaluator_id, self.kind, 1.0 if ok else 0.5, ok,
                     evidence, confidence=0.7)


EVALUATORS: dict[str, Any] = {
    e.evaluator_id: e
    for e in (RetryLoops, RepeatedCalls, ErrorRecovery, ContextGrowth,
              FirstActionLatency)
}
