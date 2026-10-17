"""Experiments: score recorded traces against a dataset, comparably.

An experiment pins (dataset version, agent config, evaluator ids + versions)
so two runs are comparable. Execution is TRACE PLAYBACK (proposal §7.3): a
run links an example to an already-recorded trace and scores it — the agent
is not re-run. Comparing models means: run each model externally (producing
new traces), attach them to runs of parallel experiments, and compare the
summaries. `compare()` is the report that makes that honest.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from core.pricing import cost_usd
from core.query import get_trace
from core.schema.model import now_iso
from core.storage.sqlite_store import TraceStore
from evaluation.evaluators import get_evaluator
from evaluation.model import Example, TraceView


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def create_experiment(store: TraceStore, name: str, dataset_id: str,
                      evaluator_ids: list[str],
                      agent_config: dict[str, Any] | None = None) -> dict[str, Any]:
    ds = store.one("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,))
    if ds is None:
        raise KeyError(f"unknown dataset: {dataset_id}")
    # resolve evaluators NOW so their versions are pinned on the experiment
    versions = {eid: get_evaluator(eid).version for eid in evaluator_ids}
    experiment_id = _uid()
    with store._write_lock:
        store._conn().execute(
            "INSERT INTO experiments"
            " (experiment_id, name, dataset_id, dataset_version, agent_config,"
            "  evaluator_ids, status, created_at)"
            " VALUES (?,?,?,?,?,?, 'created', ?)",
            (experiment_id, name, dataset_id, ds["version"],
             json.dumps(agent_config or {}, ensure_ascii=False),
             json.dumps(versions, ensure_ascii=False), now_iso()))
        store._conn().commit()
    return get_experiment(store, experiment_id)  # type: ignore[return-value]


def get_experiment(store: TraceStore, experiment_id: str) -> dict[str, Any] | None:
    row = store.one("SELECT * FROM experiments WHERE experiment_id = ?",
                    (experiment_id,))
    if row is None:
        return None
    runs = store.query(
        "SELECT * FROM experiment_runs WHERE experiment_id = ?"
        " ORDER BY created_at", (experiment_id,))
    return {**dict(row),
            "agent_config": _j(row["agent_config"]) or {},
            "evaluator_ids": _j(row["evaluator_ids"]) or {},
            "runs": [{**dict(r), "scores": _j(r["scores"]) or {}} for r in runs]}


def list_experiments(store: TraceStore) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT e.*, COUNT(r.run_id) AS runs FROM experiments e"
        " LEFT JOIN experiment_runs r ON r.experiment_id = e.experiment_id"
        " GROUP BY e.experiment_id ORDER BY e.created_at DESC")
    return [{**dict(r), "agent_config": _j(r["agent_config"]) or {},
             "evaluator_ids": _j(r["evaluator_ids"]) or {}} for r in rows]


def attach(store: TraceStore, experiment_id: str, example_id: str,
           trace_id: str) -> str:
    """Link one example to one recorded trace for this experiment."""
    run_id = _uid()
    with store._write_lock:
        store._conn().execute(
            "INSERT INTO experiment_runs"
            " (run_id, experiment_id, example_id, trace_id, status, created_at)"
            " VALUES (?,?,?,?, 'attached', ?)",
            (run_id, experiment_id, example_id, trace_id, now_iso()))
        store._conn().commit()
    return run_id


def run_experiment(store: TraceStore, experiment_id: str) -> dict[str, Any]:
    """Score every attached run (auto-attaching examples that carry a
    source_trace_id and have no attachment yet)."""
    exp = get_experiment(store, experiment_id)
    if exp is None:
        raise KeyError(f"unknown experiment: {experiment_id}")
    examples = store.query(
        "SELECT * FROM dataset_examples WHERE dataset_id = ?",
        (exp["dataset_id"],))
    attached = {r["example_id"] for r in exp["runs"]}
    for ex in examples:
        meta = _j(ex["metadata"]) or {}
        if ex["example_id"] not in attached and meta.get("source_trace_id"):
            attach(store, experiment_id, ex["example_id"],
                   meta["source_trace_id"])

    exp = get_experiment(store, experiment_id)  # refresh after auto-attach
    evaluators = [(eid, get_evaluator(eid))
                  for eid in (exp["evaluator_ids"] or {}).keys()]
    # The trace's own recorded model(s) are the billing truth; the
    # experiment's declared model is only a fallback for adapters that
    # captured none (see _score_run).
    agent_model = (exp["agent_config"] or {}).get("model")

    with store._write_lock:
        store._conn().execute(
            "UPDATE experiments SET status='running' WHERE experiment_id=?",
            (experiment_id,))
        store._conn().commit()

    for run in exp["runs"]:
        _score_run(store, run, evaluators, agent_model)

    with store._write_lock:
        store._conn().execute(
            "UPDATE experiments SET status='done' WHERE experiment_id=?",
            (experiment_id,))
        store._conn().commit()
    return summary(store, experiment_id)


def _score_run(store: TraceStore, run: dict[str, Any],
               evaluators: list[tuple[str, Any]],
               agent_model: str | None) -> None:
    trace = get_trace(store, run["trace_id"]) if run.get("trace_id") else None
    if trace is None:
        with store._write_lock:
            store._conn().execute(
                "UPDATE experiment_runs SET status='no_trace' WHERE run_id=?",
                (run["run_id"],))
            store._conn().commit()
        return

    example = Example(
        example_id=run["example_id"], dataset_id="",
        inputs=_j(_example_row(store, run["example_id"])["inputs"]) or {},
        expected=_j(_example_row(store, run["example_id"])["expected"]))
    view = TraceView(trace)
    scores = {}
    for eid, evaluator in evaluators:
        try:
            scores[eid] = evaluator.evaluate(example, view).to_dict()
        except Exception as exc:  # one bad evaluator must not kill the run
            scores[eid] = {"evaluator_id": eid, "kind": "error",
                           "score": 0.0, "passed": False,
                           "evidence": [f"evaluator raised: {exc}"],
                           "confidence": 0.0, "error": str(exc)}

    # The trace's own model is the observed truth; the experiment's declared
    # model is only a fallback for adapters that captured none. Billing a
    # playback run at the DECLARED model's price would make cost comparison
    # across experiments meaningless.
    models = [s.get("model") for s in trace["spans"] if s.get("model")]
    # Cost PER MODEL and summed. A trace that used two models must not be
    # billed entirely at the first one's price: that produces a confident,
    # wrong dollar figure, which is the one thing a cost column must never do.
    # If any model that spent tokens has no configured price, the run's cost is
    # UNKNOWN (None) — a partial sum presented as a total is worse than blank.
    cost = _trace_cost(trace, models, agent_model)
    # `all()` over an EMPTY selection is True, so filtering out signal-less
    # scores made a run with nothing measured report as a clean pass —
    # pass_rate 1.0 with every evaluator at confidence 0. That is the same
    # fake verdict D12 forbids, just pointing the other way. No counting
    # score means no verdict at all.
    counted = [s for s in scores.values() if _counts_toward_verdict(s)]
    if not scores:
        run_status = "unscored"
    elif not counted:
        run_status = "no_signal"
    else:
        run_status = "ok" if all(s["passed"] for s in counted) else "error"
    with store._write_lock:
        store._conn().execute(
            "UPDATE experiment_runs SET status=?, tokens_in=?, tokens_out=?,"
            " cache_read=?, duration_ms=?, cost_usd=?, scores=?"
            " WHERE run_id=?",
            (run_status,
             trace["tokens"]["input"], trace["tokens"]["output"],
             trace["tokens"]["cache_read"], trace.get("duration_ms"),
             cost,
             json.dumps(scores, ensure_ascii=False), run["run_id"]))
        store._conn().commit()


def _trace_cost(trace: dict[str, Any], models: list[str],
                declared: str | None) -> float | None:
    """Blended cost across the models the trace actually used.

    Three cases, all explicit:
      * spans named one or more models -> bill each model's own tokens at its
        own price and sum; if ANY of them is unpriced, return None (unknown).
      * no span named a model but the experiment declared one -> bill the
        trace total at that price (the adapter captured no model).
      * nothing known -> None.
    """
    token_bearing = [s for s in trace["spans"]
                     if any((s.get("tokens") or {}).get(k)
                            for k in ("input", "output", "cache_read"))]
    unnamed = [s for s in token_bearing if not s.get("model")]
    if unnamed:
        # Tokens were spent by a span whose model we do not know: pricing the
        # rest and calling it a total under-reports the bill, which is exactly
        # the failure mode this function exists to prevent.
        return None
    named = sorted({m for m in models if m})
    if named:
        total = 0.0
        for model in named:
            spans = [s for s in trace["spans"] if s.get("model") == model]
            tokens = [s.get("tokens") or {} for s in spans]
            per = cost_usd(
                model,
                sum(t.get("input") or 0 for t in tokens),
                sum(t.get("output") or 0 for t in tokens),
                sum(t.get("cache_read") or 0 for t in tokens))
            if per is None:
                return None        # one unknown model makes the total unknown
            total += per
        return round(total, 6)
    if declared:
        return cost_usd(declared, trace["tokens"]["input"],
                        trace["tokens"]["output"],
                        trace["tokens"]["cache_read"])
    return None


def _counts_toward_verdict(score: dict[str, Any]) -> bool:
    """A score with no signal must not vote.

    D12 says an unconfigured/unreachable judge reports confidence=0 with
    `error` set rather than a fake verdict. That is only true end-to-end if
    the rollup ALSO ignores it — otherwise "no judge configured" turns every
    run into status=error and pass_rate 0.0, which is exactly the fake verdict
    the rule exists to prevent.
    """
    if score.get("error"):
        return False
    return float(score.get("confidence") or 0.0) > 0.0


def summary(store: TraceStore, experiment_id: str) -> dict[str, Any]:
    exp = get_experiment(store, experiment_id)
    if exp is None:
        raise KeyError(f"unknown experiment: {experiment_id}")
    # pass_rate / cost speak only for runs that produced a verdict; the
    # per-evaluator table covers EVERY run, because "how often could this
    # evaluator even measure anything" (no_signal) is information the user
    # needs and hiding it would make the table look complete when it is not.
    runs = [r for r in exp["runs"] if r["status"] in ("ok", "error")]
    all_runs = [r for r in exp["runs"] if r["status"] != "no_trace"]
    per_evaluator: dict[str, dict[str, Any]] = {}
    for run in all_runs:
        for eid, score in (run["scores"] or {}).items():
            agg = per_evaluator.setdefault(
                eid, {"scores": [], "passed": 0, "n": 0, "no_signal": 0,
                      "kind": score.get("kind")})
            # Same rule as the run verdict: a score with no signal is counted
            # as no_signal, not as a failure — otherwise the per-evaluator
            # table disagrees with pass_rate for the same experiment.
            if not _counts_toward_verdict(score):
                agg["no_signal"] += 1
                continue
            agg["scores"].append(score.get("score") or 0.0)
            agg["n"] += 1
            agg["passed"] += 1 if score.get("passed") else 0
    for agg in per_evaluator.values():
        agg["mean_score"] = round(sum(agg.pop("scores")) / agg["n"], 4) if agg["n"] else None
        agg["pass_rate"] = round(agg["passed"] / agg["n"], 4) if agg["n"] else None
        del agg["passed"]
    # Cost coverage is judged over the SAME set as the reported total, and the
    # total covers every run that reached a trace. Judging `cost_unknown` over
    # the verdict-only subset while summing over all runs produced a summary
    # claiming a known cost for an experiment whose only scored run was
    # unpriced.
    cost = [r["cost_usd"] for r in all_runs if r.get("cost_usd") is not None]
    return {
        "experiment_id": experiment_id,
        "name": exp["name"],
        "dataset_id": exp["dataset_id"],
        "dataset_version": exp["dataset_version"],
        "status": exp["status"],
        "evaluator_ids": exp["evaluator_ids"],
        "runs": len(exp["runs"]),
        "scored": len(runs),
        "no_trace": sum(1 for r in exp["runs"] if r["status"] == "no_trace"),
        "pass_rate": (round(sum(1 for r in runs if r["status"] == "ok")
                            / len(runs), 4) if runs else None),
        "no_signal": sum(1 for r in exp["runs"]
                         if r["status"] in ("no_signal", "unscored")),
        "tokens": {"input": sum(r["tokens_in"] or 0 for r in all_runs),
                   "output": sum(r["tokens_out"] or 0 for r in all_runs)},
        "cost_usd": round(sum(cost), 6) if cost else None,
        # Judged over the same set the total covers: every run that reached a
        # trace. A run whose cost is None is unknown, whatever its verdict.
        "cost_unknown": len(cost) < len(all_runs),
        "per_evaluator": per_evaluator,
    }


def compare(store: TraceStore, a_id: str, b_id: str) -> dict[str, Any]:
    """Side-by-side of two experiment summaries — the experiment report's
    reason to exist (proposal §7.3: same dataset, different model/config)."""
    a, b = summary(store, a_id), summary(store, b_id)
    evaluators = sorted(set(a["per_evaluator"]) | set(b["per_evaluator"]))
    deltas = {}
    for eid in evaluators:
        ea, eb = a["per_evaluator"].get(eid), b["per_evaluator"].get(eid)
        deltas[eid] = {
            "a_mean": ea and ea["mean_score"], "b_mean": eb and eb["mean_score"],
            "delta": (round(eb["mean_score"] - ea["mean_score"], 4)
                      if ea and eb and ea["mean_score"] is not None
                      and eb["mean_score"] is not None else None),
        }
    return {"a": a, "b": b, "deltas": deltas,
            "token_delta": {
                "input": b["tokens"]["input"] - a["tokens"]["input"],
                "output": b["tokens"]["output"] - a["tokens"]["output"]},
            "cost_delta": (round(b["cost_usd"] - a["cost_usd"], 6)
                           if a["cost_usd"] is not None
                           and b["cost_usd"] is not None else None)}


def _example_row(store: TraceStore, example_id: str) -> Any:
    return store.one("SELECT * FROM dataset_examples WHERE example_id = ?",
                     (example_id,))


def _j(raw: Any) -> Any:
    if raw is None or isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
