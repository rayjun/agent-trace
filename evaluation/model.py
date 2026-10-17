"""Evaluation data model + the TraceView evaluators all share.

TraceView centralizes the questions every evaluator asks about a trace
("which spans ran tests?", "what did the agent finally say?") so the
heuristics live once, not once per evaluator. All heuristics are documented
with their limits — a trajectory signal is evidence with a confidence, never
a verdict (proposal §7.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Example:
    example_id: str
    dataset_id: str
    inputs: dict[str, Any]
    expected: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Score:
    """One evaluator's reading of one trace.

    `evidence` points at the concrete spans/messages behind the reading;
    `confidence` is how much to trust it (0 = 'cannot tell from this trace').
    Both are mandatory because a bare 0.7 means nothing.
    """

    evaluator_id: str
    kind: str                    # deterministic | llm_judge | trajectory
    score: float                 # 0..1
    passed: bool
    evidence: list[str] = field(default_factory=list)
    confidence: float = 1.0
    error: str | None = None     # evaluator itself failed (judge down, ...)

    def to_dict(self) -> dict[str, Any]:
        return {"evaluator_id": self.evaluator_id, "kind": self.kind,
                "score": round(self.score, 4), "passed": self.passed,
                "evidence": self.evidence, "confidence": self.confidence,
                "error": self.error}


#: Tools that execute shell commands, across our three adapters' namings.
SHELL_TOOLS = {"bash", "exec", "shell", "run", "terminal", "command",
               "run_command", "run_terminal_cmd"}

_TEST_CMD_RE = re.compile(
    r"\b(pytest|py\.test|unittest|go test|cargo test|npm test|npx vitest|"
    r"vitest|jest|make test|tox|nosetests|rspec|mix test)\b")

#: What "the tests passed" looks like in tool output, and what it doesn't.
_PASS_RE = re.compile(r"(\b\d+ passed\b|\b0 failed\b|\bOK\b|\bPASS\b|"
                      r"All tests passed|tests? passed)", re.IGNORECASE)
_FAIL_RE = re.compile(r"(\b[1-9]\d* failed\b|\bFAILED\b|\bFAIL:|"
                      r"tests? failed|ERRORS?\b)", re.IGNORECASE)

#: "The agent claims success" — kept deliberately conservative; a claim that
#: isn't there is a low-confidence reading, not a pass.
_CLAIM_RE = re.compile(
    r"(all tests pass|tests? (are )?passing|fixed|resolved|works now|"
    r"done[.!]?$|已完成|已修复|测试通过)", re.IGNORECASE)


class TraceView:
    """Read-only conveniences over the get_trace() dict."""

    def __init__(self, trace: dict[str, Any]) -> None:
        self.trace = trace
        self.spans: list[dict[str, Any]] = trace.get("spans", [])
        self.events: list[dict[str, Any]] = trace.get("events", [])

    @property
    def has_activity(self) -> bool:
        """False when the trace holds nothing to judge (no spans, no events).

        Without this, "no failed spans" and "no retry loops" are trivially
        true on an empty trace and two evaluators report a confident pass on
        nothing — absence of evidence read as evidence of correctness.
        """
        return bool(self.spans or self.events)

    @property
    def tool_spans(self) -> list[dict[str, Any]]:
        return [s for s in self.spans if s.get("kind") == "tool"]

    @property
    def llm_spans(self) -> list[dict[str, Any]]:
        return [s for s in self.spans if s.get("kind") == "llm"]

    @property
    def failed_spans(self) -> list[dict[str, Any]]:
        # Synthetic task roots only MIRROR their children's failure —
        # counting them double-counts every real error (same rule as
        # core/query.overview).
        return [s for s in self.spans
                if s.get("status") in ("error", "interrupted", "cancelled")
                and not s.get("synthetic")]

    def tool_args_text(self, span: dict[str, Any]) -> str:
        args = (span.get("input") or {}).get("args")
        if isinstance(args, dict):
            return " ".join(str(v) for v in args.values())
        return str(args or "")

    def tool_result_text(self, span: dict[str, Any]) -> str:
        out = (span.get("output") or {}).get("result")
        return "" if out is None else str(out)

    def is_test_run(self, span: dict[str, Any]) -> bool:
        return (span.get("kind") == "tool"
                and (span.get("name") or "") in SHELL_TOOLS
                and bool(_TEST_CMD_RE.search(self.tool_args_text(span))))

    def test_runs(self) -> list[dict[str, Any]]:
        """Shell spans that ran a test command, in trace order."""
        return [s for s in self.tool_spans if self.is_test_run(s)]

    def test_run_passed(self, span: dict[str, Any]) -> bool | None:
        """Three-valued: True/False when the signal is clear, else None."""
        text = self.tool_result_text(span)
        if span.get("status") == "error" or _FAIL_RE.search(text):
            return False
        if _PASS_RE.search(text) or (
                span.get("status") == "ok" and not text):
            # ok-with-no-output: the command exited 0, which for most test
            # runners IS the pass signal — but with output absent we can't
            # confirm; the caller gets True with reduced confidence upstream.
            return True
        return None

    def final_message(self) -> str:
        """The last thing the agent said: assistant_message event, else the
        last llm span's content."""
        for e in reversed(self.events):
            if e.get("name") == "assistant_message":
                content = (e.get("attributes") or {}).get("content")
                if content:
                    return str(content)
        for s in reversed(self.llm_spans):
            content = (s.get("output") or {}).get("content")
            if content:
                return str(content)
        return ""

    def claims_success(self) -> bool | None:
        msg = self.final_message()
        if not msg:
            return None
        return bool(_CLAIM_RE.search(msg))
