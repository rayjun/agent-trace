"""The evaluator contract.

One method: read an Example (inputs + expected) and a TraceView, return a
Score with evidence and confidence. Evaluators never mutate the trace and
never invent data — when the trace doesn't contain the signal, the honest
Score is confidence=0, not a guess.
"""

from __future__ import annotations

from typing import Protocol

from evaluation.model import Example, Score, TraceView


class Evaluator(Protocol):
    evaluator_id: str
    kind: str          # deterministic | llm_judge | trajectory
    version: str       # bump on any logic change; recorded on experiments
    description: str

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        ...
