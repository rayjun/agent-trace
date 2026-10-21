"""LLM-as-Judge — semantic quality via a configurable OpenAI-compatible endpoint.

Local-first (proposal §8.3): the default endpoint is a LOCAL model server
(AGENTTRACE_JUDGE_BASE_URL, e.g. Ollama at http://127.0.0.1:11434/v1). If no
judge is configured or reachable, evaluate() returns confidence=0 with
error set — a judge that isn't there must not produce a fake score.

Stdlib urllib; the judge call is NOT traced (recording it would create a
recursive loop of judge traces — noted in docs/decisions.md).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, cast

from evaluation.model import Example, Score, TraceView

_KIND = "llm_judge"

_PROMPT = """You are evaluating an AI coding agent's execution.

TASK GIVEN TO THE AGENT:
{task}

EXPECTED OUTCOME:
{expected}

AGENT'S FINAL MESSAGE:
{final}

EXECUTION SUMMARY:
- spans: {span_count}, tool calls: {tool_calls}, failed spans: {failed}
- last test run: {last_test}
- tokens (in/out): {tokens_in}/{tokens_out}

Judge whether the task was completed. Answer with ONLY a JSON object:
{{"completed": true/false, "confidence": 0.0-1.0, "reasoning": "one paragraph"}}"""


class TaskCompletionJudge:
    evaluator_id = "judge:task_completion"
    kind = _KIND
    version = "1"
    description = "an LLM judges task completion from the trace summary"

    def __init__(self, config: dict | None = None) -> None:
        cfg = config or {}
        self.base_url = (cfg.get("base_url")
                         or os.environ.get("AGENTTRACE_JUDGE_BASE_URL") or "")
        self.model = (cfg.get("model")
                      or os.environ.get("AGENTTRACE_JUDGE_MODEL") or "")
        self.api_key = (cfg.get("api_key")
                        or os.environ.get("AGENTTRACE_JUDGE_API_KEY") or "")
        self.timeout = float(cfg.get("timeout", 60))

    def _available(self) -> str | None:
        if not self.base_url or not self.model:
            return ("judge not configured: set AGENTTRACE_JUDGE_BASE_URL and "
                    "AGENTTRACE_JUDGE_MODEL (a local server is fine)")
        return None

    def evaluate(self, example: Example, trace: TraceView) -> Score:
        unavailable = self._available()
        if unavailable:
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         [unavailable], confidence=0.0, error=unavailable)

        runs = trace.test_runs()
        last_test = "none"
        if runs:
            test_verdict = trace.test_run_passed(runs[-1])
            last_test = {True: "passed", False: "failed",
                         None: "inconclusive"}[test_verdict]
        prompt = _PROMPT.format(
            task=json.dumps(example.inputs.get("task", ""), ensure_ascii=False),
            expected=json.dumps(example.expected, ensure_ascii=False),
            final=trace.final_message()[:4000],
            span_count=len(trace.spans),
            tool_calls=len(trace.tool_spans),
            failed=len(trace.failed_spans),
            last_test=last_test,
            tokens_in=trace.trace["tokens"]["input"],
            tokens_out=trace.trace["tokens"]["output"])

        try:
            verdict = self._call(prompt)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            msg = f"judge unreachable at {self.base_url}: {exc}"
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         [msg], confidence=0.0, error=msg)

        if verdict is None:
            msg = "judge returned unparseable output"
            return Score(self.evaluator_id, self.kind, 0.0, False,
                         [msg], confidence=0.0, error=msg)
        completed = bool(verdict.get("completed"))
        confidence = float(verdict.get("confidence") or 0.5)
        return Score(self.evaluator_id, self.kind,
                     1.0 if completed else 0.0, completed,
                     [str(verdict.get("reasoning", ""))[:500]],
                     confidence=max(0.0, min(1.0, confidence)))

    def _call(self, prompt: str) -> dict[str, Any] | None:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"}
                        if self.api_key else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        text = (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        # lenient parse: first {...} block in the reply
        match = re.search(r"\{[^{}]*\"completed\"[^{}]*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            return cast(dict[str, Any], json.loads(match.group(0)))
        except json.JSONDecodeError:
            return None


EVALUATORS: dict[str, Any] = {TaskCompletionJudge.evaluator_id: TaskCompletionJudge}
