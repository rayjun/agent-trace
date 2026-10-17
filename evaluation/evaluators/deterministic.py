"""Deterministic evaluators — objectively checkable expectations.

These read example.expected and answer yes/no with full confidence when the
signal is in the trace, and say so (confidence=0) when it isn't. The signal
source is the trace itself: test commands that ran, their outputs, the
agent's final message. Nothing here trusts the agent's self-report alone —
`det:claimed_vs_actual` exists precisely because "agent says fixed" and
"tests pass" are different observations that can disagree.
"""

from __future__ import annotations

from typing import Any

from evaluation.model import Example, Score, TraceView

_KIND = "deterministic"


class TestsPass:
    evaluator_id = "det:tests_pass"
    kind = _KIND
    version = "1"
    description = "the LAST test-command run in the trace passed"

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        runs = trace.test_runs()
        if not runs:
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         ["no test command found in trace"], confidence=0.0)
        last = runs[-1]
        verdict = trace.test_run_passed(last)
        sid = last["span_id"]
        if verdict is True:
            # ok-without-output is weaker than an explicit "N passed"
            confident = bool(trace.tool_result_text(last))
            return Score(self.evaluator_id, self.kind, 1.0, True,
                         [f"{sid}: last test run passed"
                          + ("" if confident else " (exit 0, no output captured)")],
                         confidence=1.0 if confident else 0.7)
        if verdict is False:
            err = (last.get("error") or {}).get("message", "")
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         [f"{sid}: last test run failed"
                          + (f" — {err[:120]}" if err else "")])
        return Score(self.evaluator_id, self.kind, 0.0, False,
                     [f"{sid}: test output inconclusive"], confidence=0.3)


class ClaimedVsActual:
    """The agent's final message claims success while the last test run says
    failure (or the inverse mismatch). The coding-agent failure mode this
    platform exists to catch."""

    evaluator_id = "det:claimed_vs_actual"
    kind = _KIND
    version = "1"
    description = "final message's success claim agrees with the last test run"

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        claim = trace.claims_success()
        runs = trace.test_runs()
        if claim is None:
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         ["no final agent message captured"], confidence=0.0)
        if not runs:
            return Score(self.evaluator_id, self.kind, 0.5, True,
                         ["no test runs to check the claim against"],
                         confidence=0.2)
        actual = trace.test_run_passed(runs[-1])
        if actual is None:
            return Score(self.evaluator_id, self.kind, 0.5, True,
                         [f"{runs[-1]['span_id']}: last test run inconclusive"],
                         confidence=0.3)
        if claim == actual:
            return Score(self.evaluator_id, self.kind, 1.0, True,
                         [f"claim={claim} matches last test run "
                          f"({runs[-1]['span_id']})"])
        return Score(self.evaluator_id, self.kind, 0.0, False,
                     [f"final message claims success={claim} but last test "
                      f"run ({runs[-1]['span_id']}) passed={actual}"],
                     confidence=1.0)


class NoErrors:
    evaluator_id = "det:no_errors"
    kind = _KIND
    version = "1"
    description = "no span ended in error/interrupted/cancelled"

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        if not trace.has_activity:
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         ["trace has no spans or events"], confidence=0.0)
        failed = trace.failed_spans
        if not failed:
            return Score(self.evaluator_id, self.kind, 1.0, True,
                         ["no failed spans"])
        return Score(self.evaluator_id, self.kind, 0.0, False,
                     [f"{s['span_id']} ({s['kind']}:{s['name']}): "
                      f"{(s.get('error') or {}).get('message', s['status'])[:100]}"
                      for s in failed[:5]])


class OutputContains:
    """expected.contains / expected.not_contains against the final message."""

    evaluator_id = "det:output_contains"
    kind = _KIND
    version = "1"
    description = "final message contains/omits expected substrings"

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        expected = example.expected or {}
        needles = expected.get("contains") or []
        banned = expected.get("not_contains") or []
        if not needles and not banned:
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         ["example sets no contains/not_contains expectation"],
                         confidence=0.0)
        msg = trace.final_message()
        if not msg:
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         ["no final agent message captured"], confidence=0.0)
        evidence = []
        ok = True
        for n in needles:
            hit = str(n) in msg
            ok = ok and hit
            evidence.append(f"contains {n!r}: {hit}")
        for b in banned:
            hit = str(b) in msg
            ok = ok and not hit
            evidence.append(f"not_contains {b!r}: {'violated' if hit else 'clean'}")
        return Score(self.evaluator_id, self.kind, 1.0 if ok else 0.0, ok,
                     evidence)


class Budget:
    """expected.max_tool_calls / max_tokens / max_duration_ms."""

    evaluator_id = "det:budget"
    kind = _KIND
    version = "1"
    description = "trace stayed within declared budgets"

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        expected = example.expected or {}
        checks: list[tuple[str, int | float, int | float]] = []
        skipped: list[str] = []
        if expected.get("max_tool_calls") is not None:
            checks.append(("tool calls", len(trace.tool_spans),
                           expected["max_tool_calls"]))
        if expected.get("max_tokens") is not None:
            tokens = trace.trace["tokens"]
            if tokens["input"] is None and tokens["output"] is None:
                # "not captured" is not "zero": an adapter that recorded no
                # usage would otherwise pass a token budget with full
                # confidence, which is a fabricated verdict.
                skipped.append("tokens: not captured by this adapter")
            else:
                checks.append(("tokens",
                               (tokens["input"] or 0) + (tokens["output"] or 0),
                               expected["max_tokens"]))
        if expected.get("max_duration_ms") is not None:
            duration = trace.trace.get("duration_ms")
            if duration is None:
                skipped.append("duration ms: no span bounds in this trace")
            else:
                checks.append(("duration ms", duration,
                               expected["max_duration_ms"]))
        if not checks:
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         ["no checkable budget: " + "; ".join(skipped)]
                         if skipped else ["example declares no budget"],
                         confidence=0.0)
        evidence = [f"{name}: {actual} (limit {limit})"
                    for name, actual, limit in checks]
        ok = all(actual <= limit for _, actual, limit in checks)
        return Score(self.evaluator_id, self.kind, 1.0 if ok else 0.0, ok,
                     evidence)


EVALUATORS: dict[str, Any] = {
    e.evaluator_id: (lambda cfg, e=e: e())  # config ignored; uniform factory
    for e in (TestsPass, ClaimedVsActual, NoErrors, OutputContains, Budget)
}
