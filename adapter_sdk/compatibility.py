"""Compatibility matrix — which adapters are verified against which surfaces.

Kept as data so `agenttrace adapter list` and docs/adapters.md render the same
truth. `conformance` names the fixture test that proves the claim; a claim
without a test is `unverified`, not `yes`.

Why a matrix at all: an adapter's contract is only meaningful against a
version of the thing it observes. "hermes supported" means nothing; "hermes,
verified against the 8-hook surface, by test/hermes-deploy.test.py" does.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Compatibility:
    adapter_id: str
    target: str            # the agent/format it observes
    verified_by: str       # test file proving it, or "" when unverified
    notes: str = ""

    @property
    def status(self) -> str:
        return "verified" if self.verified_by else "unverified"


MATRIX: tuple[Compatibility, ...] = (
    Compatibility("hermes", "hermes plugin hooks (pre/post_api_request, "
                  "api_request_error, pre/post_llm_call, post_tool_call, "
                  "session start/end)", "test/hermes-deploy.test.py",
                  "deployed as a byte copy; status compares bytes"),
    Compatibility("codex", "Codex CLI rollout JSONL (responses wire format)",
                  "test/codex-import.test.py",
                  "incremental import; ts comes from the rollout line's own "
                  "timestamp"),
    Compatibility("pi", "Pi >= 0.87.1 / verified live on 1.3.0 "
                  "(turn_start, before_provider_request, "
                  "after_provider_response, message_end, tool_execution_*)",
                  "test/pi-adapter.test.mjs",
                  "request/turn ids are synthesised by the adapter: Pi's own "
                  "events carry only a turn index, and the response id exists "
                  "only after the call"),
    Compatibility("otlp", "OTLP/HTTP+JSON trace payloads (GenAI semantic "
                  "conventions)", "test/otlp.test.py",
                  "round-trips agent-trace's own exports exactly"),
    Compatibility("langgraph", "LangChain callback protocol "
                  "(on_llm_*/on_tool_*/on_chain_*)", "test/langgraph.test.py",
                  "kwargs accepted as dicts; no langchain import"),
)


def all_compatibility() -> list[Compatibility]:
    return list(MATRIX)


def for_adapter(adapter_id: str) -> Compatibility | None:
    return next((c for c in MATRIX if c.adapter_id == adapter_id), None)
