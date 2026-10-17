"""LangChain / LangGraph adapter — the native path via callback handlers.

LangChain's callback interface is the stable seam: `on_llm_start/end/error`,
`on_tool_start/end/error`, `on_chain_start/end`, with a `run_id` that pairs
each start with its end and a `parent_run_id` that gives the tree. LangGraph
runs on top of that interface, so one handler covers both.

Design notes:

* **No `langchain` import.** The handler accepts the callback kwargs as plain
  dicts, exactly as LangChain passes them. That keeps the SDK stdlib-only
  (decision D6) and makes the adapter testable without installing a
  framework — and it means a version bump in LangChain cannot breakour
  importer at import time.
* **`run_id` -> `span_id`** directly; `parent_run_id` -> `parent_span_id`.
  LangChain already solved tree identity, so we reuse it instead of inventing
  a parallel scheme.
* **Chain runs become the trace root.** In LangGraph the outermost
  `on_chain_start` is the graph invocation — that is the task.
* **Attribution**: a nested llm/tool run's parent is the chain that spawned
  it, so the waterfall shows the graph structure, not a flat list.

The handler is deliberately small because the alternative integration —
point LangGraph's OTel exporter at `agenttrace` and let `adapters.otlp` read
it — already exists and is the better choice when a user has OTel set up.
`CAPABILITIES` says so.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from adapter_sdk.capabilities import AdapterCapabilities, Capability, SupportLevel
from adapter_sdk.event_sink import JsonlEventSink
from adapter_sdk.interfaces import AdapterHealth, AdapterKind, BaseAdapter, EventSink
from core.schema.model import TERMINAL_STATUSES, derive_event_id, now_iso

__all__ = ["LangGraphAdapter", "CallbackTraceHandler"]


class CallbackTraceHandler:
    """Feed LangChain/LangGraph callback events in; canonical events come out.

    Usage inside a LangGraph app:

        handler = CallbackTraceHandler(JsonlEventSink(path), session_id=sid)
        graph.invoke(state, config={"callbacks": [handler]})
    """

    def __init__(self, sink: EventSink, *,
                 session_id: str | None = None,
                 adapter_id: str = "langgraph",
                 agent_name: str | None = None,
                 root_run_id: str | None = None) -> None:
        self.sink = sink
        self.session_id = session_id
        self.adapter_id = adapter_id
        self.agent_name = agent_name or adapter_id
        #: which run_id is the trace root; the first chain start wins unless
        #: the caller pinned one (e.g. a graph invoked as a subgraph).
        self.trace_id = root_run_id
        #: run_id -> (kind, name) as declared by the start callback. LangChain
        #: hands the end callback neither, and the conformance kit requires the
        #: two halves to describe the SAME span: inventing "tool"/"chain" on
        #: the end half made every langgraph stream fail its own contract (and
        #: made the waterfall read 'tool' instead of 'bash').
        self._identity: dict[str, tuple[str, str]] = {}

    # -- envelope ---------------------------------------------------------

    def _emit(self, event: dict[str, Any]) -> None:
        self.sink.emit(event)

    def _base(self, etype: str, run_id: str, parent: str | None,
              ts: str | None = None) -> dict[str, Any]:
        return {
            "v": 2,
            "event_id": derive_event_id("langgraph", self.session_id, run_id,
                                        etype, ts or ""),
            "type": etype,
            "ts": ts or now_iso(),
            "observed_at": now_iso(),
            "adapter_id": self.adapter_id,
            "agent_name": self.agent_name,
            "session_id": self.session_id,
            "trace_id": self.trace_id or run_id,
            "span_id": run_id,
            "parent_span_id": parent or None,
        }

    def _start(self, run_id: str, kind: str, name: str,
               parent: str | None, ts: str | None = None,
               **body: Any) -> None:
        self._identity[run_id] = (kind, name)
        self._emit({**self._base("span_start", run_id, parent, ts),
                    "span": {"kind": kind, "name": name,
                             "status": "running", **body}})

    def _end(self, run_id: str, parent: str | None, status: str = "ok",
             ts: str | None = None, **body: Any) -> None:
        kind, name = self._identity.pop(run_id, ("chain", "chain"))
        self._emit({**self._base("span_end", run_id, parent, ts),
                    "span": {"kind": kind, "name": name,
                             "status": status, **body}})

    def _resolve_trace(self, run_id: str, parent_run_id: str | None) -> None:
        if self.trace_id:
            return
        if parent_run_id is None:
            self.trace_id = run_id          # outermost run is the task
        else:
            self.trace_id = parent_run_id

    # -- chains (the graph itself) ---------------------------------------

    def on_chain_start(self, run_id: str, name: str = "chain",
                       parent_run_id: str | None = None, inputs: Any = None,
                       **_: Any) -> None:
        self._resolve_trace(run_id, parent_run_id)
        self._start(run_id, "task" if parent_run_id is None else "chain", name,
                    parent_run_id, input=_safe(inputs))

    def on_chain_end(self, run_id: str, outputs: Any = None,
                     parent_run_id: str | None = None, **_: Any) -> None:
        self._end(run_id, parent_run_id, output=_safe(outputs))

    def on_chain_error(self, run_id: str, error: BaseException | str,
                       parent_run_id: str | None = None, **_: Any) -> None:
        self._end(run_id, parent_run_id, status="error", error=_err(error))

    # -- llm ---------------------------------------------------------------

    def on_llm_start(self, run_id: str, serialized: Any = None,
                     prompts: Any = None, parent_run_id: str | None = None,
                     **_: Any) -> None:
        model = _serialized_model(serialized)
        self._start(run_id, "llm", model or "llm", parent_run_id,
                    input=_safe(prompts),
                    attributes={
                        **({"model": model} if model else {}),
                        "provider": "langchain"})

    def on_chat_model_start(self, run_id: str, serialized: Any = None,
                            messages: Any = None,
                            parent_run_id: str | None = None, **_: Any) -> None:
        model = _serialized_model(serialized)
        self._start(run_id, "llm", model or "chat", parent_run_id,
                    input=_safe(messages),
                    attributes={
                        **({"model": model} if model else {}),
                        "provider": "langchain"})

    def on_llm_end(self, run_id: str, response: Any = None,
                   parent_run_id: str | None = None, **_: Any) -> None:
        usage, content, model = _llm_response(response)
        attributes: dict[str, Any] = {}
        if usage:
            attributes["usage"] = usage
        if model:
            attributes["model"] = model
        self._end(run_id, parent_run_id,
                  output={**({"content": content} if content else {})},
                  attributes=attributes or None)

    def on_llm_error(self, run_id: str, error: BaseException | str,
                     parent_run_id: str | None = None, **_: Any) -> None:
        self._end(run_id, parent_run_id, status="error", error=_err(error))

    # -- tools -------------------------------------------------------------

    def on_tool_start(self, run_id: str, name: str = "tool",
                      inputs: Any = None, parent_run_id: str | None = None,
                      **_: Any) -> None:
        # `name` is only available HERE; the end callback omits it.
        self._start(run_id, "tool", name, parent_run_id,
                    input={"args": _safe(inputs)})

    def on_tool_end(self, run_id: str, output: Any = None,
                    parent_run_id: str | None = None, **_: Any) -> None:
        self._end(run_id, parent_run_id, output={"result": _safe(output)})

    def on_tool_error(self, run_id: str, error: BaseException | str,
                      parent_run_id: str | None = None, **_: Any) -> None:
        self._end(run_id, parent_run_id, status="error", error=_err(error))

    # -- LLM-level events ---------------------------------------------------

    def on_llm_new_token(self, token: str, run_id: str | None = None,
                         **_: Any) -> None:
        """Streaming chunk -> a discrete event, not a span.

        Chunks are high-volume and individually meaningless; recording them as
        events (off by default via `stream_tokens`) keeps the trace
        reconstructable without turning a 4k-token answer into 4k spans.
        """
        if not self.stream_tokens:
            return
        self._emit({**self._base("event", run_id or "stream", None),
                    "event": {"name": "llm_token",
                              "attributes": {"token": token}}})

    stream_tokens = False

    def on_retriever_start(self, run_id: str, query: Any = None,
                           parent_run_id: str | None = None, **_: Any) -> None:
        self._start(run_id, "tool", "retriever", parent_run_id,
                    input={"args": _safe(query)})

    def on_retriever_end(self, run_id: str, documents: Any = None,
                         parent_run_id: str | None = None, **_: Any) -> None:
        self._end(run_id, parent_run_id, output={"result": _safe(documents)})

    def on_retriever_error(self, run_id: str, error: BaseException | str,
                           parent_run_id: str | None = None, **_: Any) -> None:
        self._end(run_id, parent_run_id, status="error", error=_err(error))


#: MAX_CHARS-style cap for bodies: LangChain will happily hand us a 2 MB
#: document list, and the trace file must stay readable.
_BODY_CAP = 20_000


def _serialized_model(serialized: Any) -> str | None:
    """Model name from LangChain's `serialized` dict, tolerating its shapes."""
    if not isinstance(serialized, dict):
        return None
    kwargs = serialized.get("kwargs")
    if isinstance(kwargs, dict):
        for key in ("model_name", "model", "model_id"):
            if kwargs.get(key):
                return str(kwargs[key])
    return str(serialized["name"]) if serialized.get("name") else None


def _safe(value: Any) -> Any:
    """JSON-safe, size-capped copy of a LangChain object.

    LangChain passes dataclass-ish objects (BaseMessage, Document); we take
    the `content`/`page_content`/`dict()` view when present and otherwise
    stringify. Never raise: a repr that throws must not kill the run.
    """
    try:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:_BODY_CAP]
        if isinstance(value, dict):
            return {str(k): _safe(v) for k, v in list(value.items())[:50]}
        if isinstance(value, (list, tuple)):
            return [_safe(v) for v in value[:50]]
        content = getattr(value, "content", None)
        if content is None:
            content = getattr(value, "page_content", None)
        if content is not None:
            return {"content": _safe(content)}
        if hasattr(value, "dict") and callable(value.dict):
            return _safe(value.dict())
        return str(value)[:_BODY_CAP]
    except Exception as exc:  # noqa: BLE001 - a repr must never break tracing
        return f"<unserializable {type(value).__name__}: {exc}>"


def _err(error: BaseException | str) -> dict[str, Any]:
    if isinstance(error, BaseException):
        return {"type": type(error).__name__, "message": str(error)[:4000]}
    return {"type": "error", "message": str(error)[:4000]}


def _llm_response(response: Any) -> tuple[dict[str, Any], str, str | None]:
    """(usage, content, model) from a LangChain LLMResult, defensively."""
    usage: dict[str, Any] = {}
    content = ""
    model = None
    try:
        generations = getattr(response, "generations", None) or []
        if generations and generations[0]:
            gen = generations[0][0]
            message = getattr(gen, "message", None)
            if message is not None:
                raw = getattr(message, "content", "")
                content = raw if isinstance(raw, str) else json.dumps(
                    _safe(raw), ensure_ascii=False)
                model = (getattr(message, "response_metadata", {}) or {}).get("model_name")
            elif getattr(gen, "text", None):
                content = str(gen.text)
        llm_output = getattr(response, "llm_output", None) or {}
        model = model or llm_output.get("model_name")
        raw_usage = llm_output.get("token_usage") or {}
        if raw_usage:
            usage = {
                "input_tokens": raw_usage.get("prompt_tokens"),
                "output_tokens": raw_usage.get("completion_tokens"),
            }
            details = raw_usage.get("prompt_tokens_details") or {}
            if details.get("cached_tokens") is not None:
                usage["cache_read_tokens"] = details["cached_tokens"]
    except Exception:  # noqa: BLE001
        pass
    return {k: v for k, v in usage.items() if v is not None}, content[:_BODY_CAP], model


class LangGraphAdapter(BaseAdapter):
    """Identity + capability declaration; the handler does the work."""

    adapter_id = "langgraph"
    kind = AdapterKind.NATIVE

    @property
    def capabilities(self) -> AdapterCapabilities:
        caps = AdapterCapabilities()
        caps.set(Capability.LLM_CALLS, SupportLevel.SUPPORTED,
                 "on_llm_start/end/error carry prompts, usage and model")
        caps.set(Capability.TOOL_CALLS, SupportLevel.SUPPORTED,
                 "on_tool_start/end/error pair by run_id")
        caps.set(Capability.AGENT_LIFECYCLE, SupportLevel.SUPPORTED,
                 "outermost on_chain_start is the task root")
        caps.set(Capability.SUBAGENTS, SupportLevel.SUPPORTED,
                 "nested chains and LangGraph subgraphs appear as agent/chain spans")
        caps.set(Capability.STREAMING, SupportLevel.PARTIAL,
                 "on_llm_new_token available but OFF by default (volume); "
                 "enable via stream_tokens")
        caps.set(Capability.TOKEN_USAGE, SupportLevel.PARTIAL,
                 "read from llm_output.token_usage; absent for providers that "
                 "do not report it through LangChain")
        caps.set(Capability.ERRORS, SupportLevel.SUPPORTED)
        caps.set(Capability.SESSION_LIFECYCLE, SupportLevel.PARTIAL,
                 "no session concept in LangChain; caller supplies session_id")
        caps.set(Capability.REPLAY_INPUTS, SupportLevel.PARTIAL,
                 "prompt bodies captured; arbitrary tool side effects are not "
                 "replayable (M5)")
        return caps

    def make_handler(self, sink: EventSink, **kwargs: Any) -> CallbackTraceHandler:
        return CallbackTraceHandler(sink, **kwargs)

    def health_check(self) -> AdapterHealth:
        return AdapterHealth(ok=True, adapter_id=self.adapter_id,
                             detail="callback handler; no background process")


def random_run_id() -> str:
    """Only for tests/harnesses — LangChain supplies real run ids."""
    return uuid.uuid4().hex
