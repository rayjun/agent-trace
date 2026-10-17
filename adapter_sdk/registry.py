"""The static adapter registry — M1 form of what becomes the
adapter_registry table in M2.

Levels below are SELF-DECLARED from reading each adapter's source, not yet
verified by the conformance kit; anything unverified stays UNKNOWN on
purpose. The Integrations page should render exactly this distinction.

`replay_inputs` means "the trace carries enough to re-run the input side"
(full request bodies), not that replay is implemented — it is not (M5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from adapter_sdk.capabilities import AdapterCapabilities, Capability, SupportLevel as S
from adapter_sdk.interfaces import AdapterKind


@dataclass
class AdapterRegistryEntry:
    adapter_id: str
    kind: AdapterKind
    agent_name: str
    integration: str          # how it hooks in, one line
    agent_versions: str       # verified-against range, free text
    capabilities: AdapterCapabilities = field(default_factory=AdapterCapabilities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "kind": self.kind.value,
            "agent_name": self.agent_name,
            "integration": self.integration,
            "agent_versions": self.agent_versions,
            "capabilities": self.capabilities.to_dict(),
        }


def _hermes() -> AdapterRegistryEntry:
    caps = AdapterCapabilities()
    caps.set(Capability.LLM_CALLS, S.SUPPORTED,
             "pre/post_api_request + api_request_error hooks carry full bodies")
    caps.set(Capability.TOOL_CALLS, S.SUPPORTED,
             "post_tool_call emits both tool_call (args) and tool_result (outcome)")
    caps.set(Capability.SESSION_LIFECYCLE, S.SUPPORTED)
    caps.set(Capability.AGENT_LIFECYCLE, S.PARTIAL,
             "turn boundaries exist (pre/post_llm_call) but no explicit task span")
    caps.set(Capability.STREAMING, S.PARTIAL,
             "the live request hook sees the full payload once, not per-chunk")
    caps.set(Capability.TOKEN_USAGE, S.SUPPORTED)
    caps.set(Capability.ERRORS, S.SUPPORTED)
    caps.set(Capability.REPLAY_INPUTS, S.PARTIAL,
             "full llm request bodies stored; tool side-effects not replayable")
    # SUBAGENTS / STREAMING: unverified -> UNKNOWN (the default)
    return AdapterRegistryEntry(
        adapter_id="hermes",
        kind=AdapterKind.NATIVE,
        agent_name="hermes",
        integration="plugin hooks, byte-copied into ~/.hermes",
        agent_versions="current",
        capabilities=caps,
    )


def _codex() -> AdapterRegistryEntry:
    caps = AdapterCapabilities()
    caps.set(Capability.LLM_CALLS, S.PARTIAL,
             "rollout files have no raw API request bodies; reconstructed from "
             "response_items, with the rollout line's own timestamp as ts")
    caps.set(Capability.TOOL_CALLS, S.PARTIAL,
             "function_call/output pairs seen after the fact; no tool start clock")
    caps.set(Capability.SESSION_LIFECYCLE, S.PARTIAL,
             "session_meta gives the id, but no session_start/session_end "
             "records are ever emitted, so `sessions.ended` stays false")
    caps.set(Capability.TOKEN_USAGE, S.PARTIAL,
             "token_count readings accumulate onto that turn's llm response "
             "(last reading wins, since Codex reports per call and we emit per "
             "turn); no per-call breakdown")
    caps.set(Capability.ERRORS, S.PARTIAL,
             "tool failures surface as function_call_output with an error "
             "status; provider API errors never reach the rollout file")
    caps.set(Capability.STREAMING, S.UNSUPPORTED,
             "rollout file is written after completion")
    caps.set(Capability.REPLAY_INPUTS, S.UNSUPPORTED,
             "raw request payloads never touch disk")
    return AdapterRegistryEntry(
        adapter_id="codex",
        kind=AdapterKind.PROCESS_HOOK,
        agent_name="codex",
        integration="lifecycle hook -> incremental rollout importer",
        agent_versions="responses wire format",
        capabilities=caps,
    )


def _pi() -> AdapterRegistryEntry:
    caps = AdapterCapabilities()
    caps.set(Capability.LLM_CALLS, S.SUPPORTED)
    caps.set(Capability.TOOL_CALLS, S.SUPPORTED,
             "tool_execution_start/end pair choice → execution → result; the "
             "result body and a shell tool's exit_code are captured")
    caps.set(Capability.SESSION_LIFECYCLE, S.SUPPORTED, "agent_start/agent_end")
    caps.set(Capability.TOKEN_USAGE, S.SUPPORTED)
    caps.set(Capability.ERRORS, S.PARTIAL,
             "tool failures carry tool.status=error and HTTP >=400 responses "
             "are structured llm_error records; transport-level failures "
             "(DNS, TLS, timeout) are not observable through the extension API")
    caps.set(Capability.REPLAY_INPUTS, S.PARTIAL)
    # STREAMING: pi-ai is streaming-native but chunk capture is unverified.
    # SUBAGENTS: unverified.
    return AdapterRegistryEntry(
        adapter_id="pi",
        kind=AdapterKind.NATIVE,
        agent_name="pi",
        integration="TypeScript extension inside the Pi process",
        agent_versions="pi >= 0.87.1",
        capabilities=caps,
    )


def _otlp() -> AdapterRegistryEntry:
    caps = AdapterCapabilities()
    caps.set(Capability.LLM_CALLS, S.PARTIAL,
             "depends on the producer's instrumentation; GenAI conventions read")
    caps.set(Capability.TOOL_CALLS, S.PARTIAL,
             "gen_ai.operation.name=execute_tool or agent_trace.span_kind")
    caps.set(Capability.TOKEN_USAGE, S.PARTIAL,
             "gen_ai.usage.* read; absent when the producer omits usage")
    caps.set(Capability.SESSION_LIFECYCLE, S.PARTIAL,
             "no session concept in OTLP; from agent_trace.session_id only")
    caps.set(Capability.STREAMING, S.UNSUPPORTED,
             "OTLP batches spans; per-chunk emission is not representable")
    caps.set(Capability.ERRORS, S.PARTIAL, "OTLP status code -> error")
    caps.set(Capability.REPLAY_INPUTS, S.PARTIAL,
             "only when the producer puts bodies in agent_trace.input")
    return AdapterRegistryEntry(
        adapter_id="otlp",
        kind=AdapterKind.PROTOCOL,
        agent_name="any OTel-instrumented app",
        integration="OTLP/HTTP+JSON import + export",
        agent_versions="OTLP JSON, GenAI semantic conventions",
        capabilities=caps,
    )


def _langgraph() -> AdapterRegistryEntry:
    caps = AdapterCapabilities()
    caps.set(Capability.LLM_CALLS, S.SUPPORTED,
             "on_llm_start/end/error carry prompts, usage and model")
    caps.set(Capability.TOOL_CALLS, S.SUPPORTED, "on_tool_start/end/error by run_id")
    caps.set(Capability.AGENT_LIFECYCLE, S.SUPPORTED,
             "outermost on_chain_start is the task root")
    caps.set(Capability.SUBAGENTS, S.PARTIAL,
             "nested chains and LangGraph subgraphs are captured, but as "
             "kind=chain — they are not distinguished as kind=agent")
    caps.set(Capability.STREAMING, S.PARTIAL,
             "on_llm_new_token exists but is off by default (volume)")
    caps.set(Capability.TOKEN_USAGE, S.PARTIAL,
             "from llm_output.token_usage; absent for providers that omit it")
    caps.set(Capability.ERRORS, S.SUPPORTED)
    caps.set(Capability.SESSION_LIFECYCLE, S.PARTIAL,
             "no session concept in LangChain; caller supplies session_id")
    caps.set(Capability.REPLAY_INPUTS, S.PARTIAL)
    return AdapterRegistryEntry(
        adapter_id="langgraph",
        kind=AdapterKind.NATIVE,
        agent_name="langchain / langgraph",
        integration="callback handler (kwargs as dicts; no langchain import)",
        agent_versions="LangChain callback protocol",
        capabilities=caps,
    )


_REGISTRY: dict[str, AdapterRegistryEntry] = {
    entry.adapter_id: entry
    for entry in (_hermes(), _codex(), _pi(), _otlp(), _langgraph())
}


def get(adapter_id: str) -> AdapterRegistryEntry | None:
    return _REGISTRY.get(adapter_id)


def all_entries() -> list[AdapterRegistryEntry]:
    return list(_REGISTRY.values())
