"""Adapter conformance kit — `agenttrace adapter test` (M1 form: python -m).

The kit validates an adapter's EMITTED STREAM, not its code: a JSONL file of
canonical events is something every adapter can produce regardless of
implementation language (the Pi adapter is TypeScript). Python interface
compliance (the AgentAdapter protocol) is a separate, optional layer.

Checks (mapping to the proposal's §4.4 list):

  1. schema        every line is a valid canonical event
  2. lifecycle     span_end has a span_start; terminal states legal
  3. parent-child  parent_span_id resolves within the stream (or is the root)
  4. dedup         no event_id carries two different payloads
  5. ordering      end-before-start is REPORTED (legal: crash, hook flush
                   order) but must be reconcilable — both halves must exist
  6. status        error payload <=> failure status, both directions
  7. privacy       capture=metadata streams carry no input/output bodies
  8. capability    observed span kinds vs the adapter's registry declaration
                   (only with --adapter; UNKNOWN declarations are skipped)

Persistence (6 in the proposal's list) and failure-isolation (10) are runtime
properties of a sink/agent pair and cannot be checked from a stream file;
they belong to the integration fixtures under test/.

Exit code: 0 = conformant, 1 = violations found.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from core.schema.model import TERMINAL_STATUSES, validate_event

_FAILURE_STATES = ("error", "interrupted", "cancelled")


@dataclass
class ConformanceReport:
    path: str
    events: int = 0
    corrupt_lines: int = 0
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.corrupt_lines == 0 and not self.violations

    def render(self) -> str:
        lines = [f"conformance: {self.path}",
                 f"  events: {self.events}  corrupt lines: {self.corrupt_lines}"]
        for key, value in sorted(self.stats.items()):
            lines.append(f"  {key}: {value}")
        for v in self.violations:
            lines.append(f"  VIOLATION  {v}")
        for w in self.warnings:
            lines.append(f"  warning    {w}")
        lines.append("  RESULT: PASS" if self.ok else "  RESULT: FAIL")
        return "\n".join(lines)


def _span_key(rec: dict[str, Any]) -> tuple[Any, Any]:
    """A span's identity is (trace_id, span_id) — never span_id alone.

    Span ids are only unique within a trace: the v1 converter mints
    `llm:<request_id>` and `tool:<call_id>`, and two different traces can both
    contain `r1`. Keying on span_id alone let a start in one trace pair with
    an end in another, producing a PASS for a tree that does not exist.
    """
    return (rec.get("trace_id"), rec.get("span_id"))


def _iter_lines(path: Path) -> Iterator[tuple[int, dict[str, Any] | None]]:
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                yield lineno, None
                continue
            yield lineno, rec if isinstance(rec, dict) else None


def check_stream(path: str | Path, adapter_id: str | None = None) -> ConformanceReport:
    report = ConformanceReport(path=str(path))
    seen_ids: dict[str, str] = {}          # event_id -> canonical payload string
    starts: dict[tuple[Any, Any], dict[str, Any]] = {}
    ends: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    declared_parents: set[tuple[Any, Any]] = set()
    span_kinds: dict[str, int] = {}
    event_names: dict[str, int] = {}
    adapters_seen: set[str] = set()

    for lineno, rec in _iter_lines(Path(path)):
        if rec is None:
            report.corrupt_lines += 1
            report.violations.append(f"line {lineno}: not valid JSON")
            continue
        report.events += 1

        # 1. schema
        for problem in validate_event(rec):
            report.violations.append(f"line {lineno}: {problem}")

        adapter = rec.get("adapter_id")
        if adapter:
            adapters_seen.add(adapter)

        # 4. dedup — same id, same payload is a legal re-emit; same id,
        # different payload means the id is not actually an identity.
        eid = rec.get("event_id")
        if isinstance(eid, str):
            canonical = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            if eid in seen_ids:
                if seen_ids[eid] != canonical:
                    report.violations.append(
                        f"line {lineno}: event_id {eid} carries a different "
                        "payload than its first occurrence")
            else:
                seen_ids[eid] = canonical

        etype = rec.get("type")
        span = rec.get("span") if isinstance(rec.get("span"), dict) else None

        if etype == "span_start" and span:
            key = _span_key(rec)
            if key in starts:
                report.violations.append(
                    f"line {lineno}: duplicate span_start for span_id "
                    f"{rec.get('span_id')} in trace {rec.get('trace_id')}")
            else:
                starts[key] = rec
            parent = rec.get("parent_span_id")
            if parent:
                # A parent lives in the SAME trace by definition, so the
                # parent identity is (trace_id, span_id) too.
                declared_parents.add((rec.get("trace_id"), parent))
            span_kinds[span.get("kind", "?")] = span_kinds.get(span.get("kind", "?"), 0) + 1

        elif etype == "span_end" and span:
            ends.setdefault(_span_key(rec), []).append(rec)
            # 6. status <-> error coherence, both directions
            status = span.get("status")
            has_error = bool(span.get("error"))
            if has_error and status not in _FAILURE_STATES:
                report.violations.append(
                    f"line {lineno}: error payload with status {status!r}")
            if status == "error" and not has_error:
                report.warnings.append(
                    f"line {lineno}: status=error without an error payload")

        elif etype == "event":
            name = (rec.get("event") or {}).get("name", "?")
            event_names[name] = event_names.get(name, 0) + 1

    # 2/5/6b. lifecycle, ordering and half-identity — all keyed by
    # (trace_id, span_id). Span ids are only unique WITHIN a trace (the v1
    # converter mints `llm:<request_id>`), so keying by span_id alone let a
    # start in one trace pair with an end in another and report PASS.
    for key, span_ends in ends.items():
        sid = key[1]
        if key not in starts:
            report.violations.append(
                f"span {sid} (trace {key[0]}): span_end without any "
                f"span_start ({len(span_ends)} end(s))")
            continue
        if len(span_ends) > 1:
            report.violations.append(
                f"span {sid}: {len(span_ends)} span_ends for one span_start")
            continue
        end_span = span_ends[0].get("span") or {}
        if end_span.get("status") not in TERMINAL_STATUSES:
            report.violations.append(f"span {sid}: non-terminal span_end")
        if span_ends[0]["ts"] < starts[key]["ts"]:
            # end-before-start is only a WARNING: crash mid-stream or a hook
            # flushing late are real; the pair is complete, order is noise.
            report.warnings.append(
                f"span {sid}: span_end ts precedes span_start ts")
        start_span = starts[key].get("span") or {}
        if start_span.get("kind") != end_span.get("kind"):
            report.violations.append(
                f"span {sid} starts as kind {start_span.get('kind')!r} "
                f"and ends as {end_span.get('kind')!r}")
        if start_span.get("name") != end_span.get("name"):
            report.warnings.append(
                f"span {sid} name changed "
                f"({start_span.get('name')!r} -> {end_span.get('name')!r})")

    unclosed = [key[1] for key in starts if key not in ends]
    if unclosed:
        # Not a violation — an interrupted agent produces exactly this — but
        # the materializer must mark them incomplete, so the stream must
        # admit it has them.
        report.warnings.append(
            f"{len(unclosed)} span(s) never closed (materializer marks "
            f"'incomplete'): {', '.join(sorted(unclosed)[:5])}"
            + (" ..." if len(unclosed) > 5 else ""))

    # 3. parent-child — a parent must be in this stream (else the tree renders
    # a dangling branch) AND in the same trace: a parent that exists but
    # belongs to another trace produces a branch the materializer cannot join,
    # and reporting PASS on it made `verified_by` a false promise.
    for parent_key in sorted(declared_parents, key=lambda k: (str(k[0]), str(k[1]))):
        if parent_key not in starts:
            report.violations.append(
                f"parent_span_id {parent_key[1]}: no span_start in this stream")

    # 8. capability cross-check against the registry
    if adapter_id:
        report = _check_capabilities(report, adapter_id, span_kinds)

    if len(adapters_seen) > 1:
        report.warnings.append(
            f"mixed adapters in one stream: {sorted(adapters_seen)}")

    report.stats = {
        **{f"span.{k}": v for k, v in sorted(span_kinds.items())},
        **{f"event.{k}": v for k, v in sorted(event_names.items())},
        "spans.unclosed": len(unclosed),
    }
    return report


def _check_capabilities(report: ConformanceReport, adapter_id: str,
                        span_kinds: dict[str, int]) -> ConformanceReport:
    from adapter_sdk.registry import get  # local import: registry is optional

    entry = get(adapter_id)
    if entry is None:
        report.warnings.append(f"adapter {adapter_id!r} not in the registry")
        return report
    from adapter_sdk.capabilities import Capability, SupportLevel

    def level(cap: Capability) -> SupportLevel:
        return entry.capabilities.level(cap)

    if span_kinds.get("llm") and level(Capability.LLM_CALLS) == SupportLevel.UNSUPPORTED:
        report.violations.append("stream has llm spans but llm_calls=unsupported")
    if span_kinds.get("tool") and level(Capability.TOOL_CALLS) == SupportLevel.UNSUPPORTED:
        report.violations.append("stream has tool spans but tool_calls=unsupported")
    if not span_kinds.get("llm") and level(Capability.LLM_CALLS) == SupportLevel.SUPPORTED:
        report.warnings.append(
            "llm_calls declared supported but no llm span in this stream "
            "(small sample or stale declaration)")
    return report


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    adapter = next((a.split("=", 1)[1] for a in argv if a.startswith("--adapter=")), None)
    if not args:
        print("usage: python3 -m adapter_sdk.testing <events.jsonl> [--adapter=ID]",
              file=sys.stderr)
        return 2
    report = check_stream(args[0], adapter_id=adapter)
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
