"""The efficiency report — what the trajectory evaluators say across many traces.

Proposal §8.2: surface the raw signals and their trends, and do NOT collapse
them into one "agent efficiency score". A retry loop can be the correct recovery;
extra tokens can be the price of a harder task. So this aggregates each signal
separately, with its sample size, and never adds them up.

It also answers a question no single trace can: is this getting better or worse?
A metric table without a trend is a snapshot, and a snapshot of an agent's
behaviour is what the trace list already gives you.

Every number comes from the SAME evaluators the dashboard scores traces with, so
the report cannot drift from the per-trace verdicts a user already trusts, and
the evidence behind each count is reachable (`agenttrace report --traces`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The trajectory signals, in the order a reader should look at them: what the
#: agent did (motion), then what it cost, then how it ended.
SIGNALS: tuple[str, ...] = (
    "traj:first_action_latency",
    "traj:error_recovery",
    "traj:retry_loops",
    "traj:repeated_calls",
    "traj:context_growth",
)

#: Outcomes worth counting separately from the trajectory signals.
OUTCOMES: tuple[str, ...] = ("det:tests_pass", "det:claimed_vs_actual")


@dataclass
class Signal:
    evaluator_id: str
    kind: str
    n: int = 0
    passed: int = 0
    no_signal: int = 0
    mean_score: float | None = None
    confidence_sum: float = 0.0
    #: Up to a few concrete examples, so a count can be checked instead of
    #: believed.
    examples: list[tuple[str, str]] = field(default_factory=list)
    _scores: list[float] = field(default_factory=list, repr=False)

    def add(self, trace_id: str, score: dict[str, Any]) -> None:
        if score.get("error") or not score.get("confidence"):
            self.no_signal += 1
            return
        self.n += 1
        self.passed += 1 if score.get("passed") else 0
        self.confidence_sum += float(score.get("confidence") or 0)
        self._scores.append(float(score.get("score") or 0.0))
        if not score.get("passed") and len(self.examples) < 5:
            evidence = score.get("evidence") or []
            self.examples.append((trace_id, str(evidence[0])[:160] if evidence
                                  else ""))

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluator_id": self.evaluator_id,
            "kind": self.kind,
            "traces_scored": self.n,
            "traces_flagged": self.n - self.passed,
            "pass_rate": round(self.passed / self.n, 4) if self.n else None,
            "mean_score": (round(sum(self._scores) / len(self._scores), 4)
                           if self._scores else None),
            "mean_confidence": (round(self.confidence_sum / self.n, 3)
                                if self.n else None),
            "no_signal": self.no_signal,
            "examples": [{"trace_id": t, "evidence": e} for t, e in self.examples],
        }


def report(store, *, limit: int = 200, agent: str | None = None,
           since: str | None = None) -> dict[str, Any]:
    """Aggregate the trajectory signals over the most recent traces."""
    from core.pricing import cost_usd
    from core.query import get_trace, list_traces
    from evaluation.evaluators import get_evaluator
    from evaluation.model import Example, TraceView

    traces = list_traces(store, limit=limit, agent=agent, since=since)["traces"]
    signals = {eid: Signal(eid, get_evaluator(eid).kind)
               for eid in (*SIGNALS, *OUTCOMES)}
    per_agent: dict[str, dict[str, int]] = {}
    tokens = {"input": 0, "output": 0, "cache_read": 0}
    cost_total = 0.0
    costs_known = 0
    durations: list[int] = []
    failed_traces = 0
    test_runs = 0
    test_failures = 0
    files_touched: dict[str, int] = {}

    for summary in traces:
        trace = get_trace(store, summary["trace_id"])
        if trace is None:
            continue
        view = TraceView(trace)
        example = Example(example_id=summary["trace_id"], dataset_id="",
                          inputs={}, expected={})
        for eid, signal in signals.items():
            try:
                signal.add(summary["trace_id"], get_evaluator(eid).evaluate(
                    example, view).to_dict())
            except Exception as exc:              # noqa: BLE001
                signal.add(summary["trace_id"],
                           {"error": str(exc), "confidence": 0.0})

        bucket = per_agent.setdefault(summary["adapter_id"],
                                      {"traces": 0, "errors": 0})
        bucket["traces"] += 1
        if summary["status"] == "error":
            bucket["errors"] += 1
            failed_traces += 1
        for key in tokens:
            tokens[key] += summary["tokens"][key] or 0
        if summary.get("duration_ms"):
            durations.append(summary["duration_ms"])
        for span in trace["spans"]:
            touched = (span.get("attributes") or {}).get("file")
            if touched:
                files_touched[touched] = files_touched.get(touched, 0) + 1
        runs = view.test_runs()
        test_runs += len(runs)
        test_failures += sum(1 for r in runs if view.test_run_passed(r) is False)
        models = {s.get("model") for s in trace["spans"] if s.get("model")}
        trace_cost = None
        if models and all(cost_usd(m, 0, 0, 0) is not None for m in models):
            trace_cost = sum(
                cost_usd(m,
                         sum(((s.get("tokens") or {}).get("input") or 0)
                             for s in trace["spans"] if s.get("model") == m),
                         sum(((s.get("tokens") or {}).get("output") or 0)
                             for s in trace["spans"] if s.get("model") == m),
                         sum(((s.get("tokens") or {}).get("cache_read") or 0)
                             for s in trace["spans"] if s.get("model") == m))
                or 0.0 for m in models)
        if trace_cost is not None:
            costs_known += 1
            cost_total += trace_cost

    durations.sort()

    def pct(q: float) -> int | None:
        if not durations:
            return None
        return durations[min(len(durations) - 1, int(q * len(durations)))]

    return {
        "traces": len(traces),
        "failed_traces": failed_traces,
        "tests": {"runs": test_runs, "failed_runs": test_failures},
        "tokens": tokens,
        "cache_hit_rate": (round(tokens["cache_read"] / tokens["input"], 4)
                           if tokens["input"] else None),
        "duration_ms": {"p50": pct(0.50), "p95": pct(0.95)},
        "cost_usd": round(cost_total, 6) if costs_known else None,
        "cost_unknown_traces": len(traces) - costs_known,
        "per_agent": per_agent,
        "files_touched": dict(sorted(files_touched.items(),
                                     key=lambda kv: -kv[1])[:15]),
        "signals": {eid: s.to_dict() for eid, s in signals.items()},
        "note": ("signals are reported separately and never summed into one "
                 "score: a retry loop can be the correct recovery, and extra "
                 "tokens can be the price of a harder task. `no_signal` counts "
                 "the traces where the evaluator could not tell — those are not "
                 "failures."),
    }


def render(rep: dict[str, Any], *, verbose: bool = False) -> str:
    """Human output. Deliberately not a single score."""
    lines: list[str] = []
    tokens = rep["tokens"]
    lines.append(f"traces analysed      {rep['traces']}"
                 f"  ({rep['failed_traces']} failed)")
    lines.append(f"test commands run    {rep['tests']['runs']}"
                 f"  ({rep['tests']['failed_runs']} failed)")
    lines.append(f"tokens               in {tokens['input']:,}  "
                 f"out {tokens['output']:,}  cache_read {tokens['cache_read']:,}")
    if rep["cache_hit_rate"] is not None:
        lines.append(f"cache hit rate       {rep['cache_hit_rate'] * 100:.1f}%")
    if rep["duration_ms"]["p50"] is not None:
        lines.append(f"trace duration       p50 {rep['duration_ms']['p50']}ms  "
                     f"p95 {rep['duration_ms']['p95']}ms")
    if rep["cost_usd"] is not None:
        lines.append(f"cost                 ${rep['cost_usd']:.4f}"
                     f"  ({rep['cost_unknown_traces']} trace(s) unpriced)")
    else:
        lines.append(f"cost                 unknown for every trace "
                     f"({rep['cost_unknown_traces']} unpriced)")

    lines.append("")
    lines.append("per agent")
    for agent_id, counts in sorted(rep["per_agent"].items()):
        lines.append(f"  {agent_id:<10} {counts['traces']:>4} traces  "
                     f"{counts['errors']:>3} failed")

    lines.append("")
    lines.append("trajectory signals (never summed)")
    for eid, sig in rep["signals"].items():
        if not sig["traces_scored"] and not sig["no_signal"]:
            continue
        rate = sig["pass_rate"]
        shown = "—" if rate is None else f"{rate * 100:5.1f}%"
        lines.append(f"  {eid:<26} {shown}  scored={sig['traces_scored']:<4} "
                     f"flagged={sig['traces_flagged']:<4} "
                     f"no_signal={sig['no_signal']:<4} "
                     f"conf={sig['mean_confidence']}")
        if verbose:
            for ex in sig["examples"]:
                lines.append(f"      {ex['trace_id'][:12]}  {ex['evidence']}")

    if rep["files_touched"]:
        lines.append("")
        lines.append("files most touched")
        for path, count in list(rep["files_touched"].items())[:8]:
            lines.append(f"  {count:>4}x  {path}")
    lines.append("")
    lines.append(rep["note"])
    return "\n".join(lines)
