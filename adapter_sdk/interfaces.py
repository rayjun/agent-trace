"""The adapter contract, as Python protocols.

Scope — read this before implementing:

These protocols bind PYTHON-native adapters only. The canonical contract
every adapter conforms to is the wire format (schema/events.schema.json) plus
the stream-level invariants checked by adapter_sdk.testing — because the Pi
adapter is TypeScript and the Codex hook is a short-lived process, neither of
which can or should implement a Python interface.

Lifecycle is deliberately minimal and mostly optional:

* install/start/stop exist for adapters that RUN somewhere (a LangGraph
  callback registered for a process lifetime, an OTLP receiver).
* A process/hook adapter (codex) has no meaningful start(): it is exec'd by
  the host per event, converts, appends via an EventSink, exits. Forcing a
  daemon lifecycle onto that shape is how you get a 50 ms hook spawning a
  supervisor tree. Such an adapter implements identity + capabilities and
  lets the remaining methods be the defaults below.

Non-invasive rule (project principle 5): nothing here may raise into the
host. EventSink.emit catches its own I/O errors and reports them via the
return value / health; an adapter whose collector is down must degrade, not
block the agent it observes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from adapter_sdk.capabilities import AdapterCapabilities


class AdapterKind(str, Enum):
    NATIVE = "native"              # in-process plugin/extension/callback
    PROCESS_HOOK = "process_hook"  # short-lived hook/importer per event
    PROTOCOL = "protocol"          # OTLP receiver / SDK wire protocol


@dataclass
class AdapterHealth:
    """What health_check returns; also what the Integrations page renders."""

    ok: bool
    adapter_id: str
    detail: str = ""
    events_written: int = 0
    write_errors: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class EventSink(Protocol):
    """Where an adapter puts events. Implementations: JsonlEventSink (M1),
    later an HTTP sink to the local collector (M2). One method contract:
    emit never raises — failure is reported, not thrown into the host agent.
    """

    def emit(self, event: dict[str, Any]) -> bool:
        """Append one canonical event. False = not written (see health)."""
        ...

    def emit_many(self, events: list[dict[str, Any]]) -> int:
        """Append a batch; returns how many landed."""
        ...


@runtime_checkable
class AgentAdapter(Protocol):
    """Identity + capability + optional lifecycle for Python-native adapters."""

    @property
    def adapter_id(self) -> str:
        """Stable, lowercase, matches the `adapter_id` field on emitted events."""
        ...

    @property
    def kind(self) -> AdapterKind:
        ...

    @property
    def capabilities(self) -> AdapterCapabilities:
        ...

    def install(self) -> None:
        """One-time setup (copy plugin, write hooks.json). May be a no-op."""
        ...

    def start(self) -> None:
        """Begin observing. No-op for process/hook adapters."""
        ...

    def stop(self) -> None:
        """Stop observing, flush pending events. No-op is legal."""
        ...

    def health_check(self) -> AdapterHealth:
        ...


class BaseAdapter:
    """Convenience base with the no-op lifecycle filled in.

    Not part of the contract — the Protocol above is. Exists so a hook-style
    adapter declares identity + capabilities + a sink and is done.
    """

    adapter_id: str = "unnamed"
    kind: AdapterKind = AdapterKind.PROCESS_HOOK

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities()

    def install(self) -> None:
        return None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def health_check(self) -> AdapterHealth:
        return AdapterHealth(ok=True, adapter_id=self.adapter_id)
