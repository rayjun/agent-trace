"""Evaluator registry. Ids are stable strings recorded on experiments —
changing an evaluator's logic must change its `version`, or old scores stop
being comparable (proposal §7.3: experiments pin evaluator versions).
"""

from __future__ import annotations

from typing import Any, cast

from evaluation.evaluators.base import Evaluator
from evaluation.evaluators import deterministic, llm_judge, trajectory

_REGISTRY: dict[str, Any] = {
    **deterministic.EVALUATORS,
    **trajectory.EVALUATORS,
    **llm_judge.EVALUATORS,
}


def get_evaluator(evaluator_id: str, config: dict | None = None) -> Evaluator:
    if evaluator_id not in _REGISTRY:
        raise KeyError(f"unknown evaluator: {evaluator_id}")
    factory = _REGISTRY[evaluator_id]
    return cast(Evaluator, factory(config or {}) if callable(factory) else factory)


def all_evaluators() -> list[Evaluator]:
    return [get_evaluator(eid) for eid in sorted(_REGISTRY)]
