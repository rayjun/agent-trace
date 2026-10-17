"""agent-trace adapter SDK — the contract layer for integrations.

What this package is:

* capabilities.py — what an adapter can observe, as queryable data.
* interfaces.py   — EventSink + AgentAdapter protocols for PYTHON-native
                    adapters. The cross-language contract is the wire format
                    (schema/events.schema.json), not these protocols: the Pi
                    adapter is TypeScript and conforms without importing any
                    of this.
* event_sink.py   — the reference JSONL sink (locked append, validate-on-emit).
* registry.py     — the static registry of known adapters (first version of
                    what becomes the adapter_registry table in M2).
* testing.py      — the conformance kit: language-agnostic checks over an
                    adapter's emitted event stream.

Stdlib only — an SDK that adapters import inside host processes cannot carry
dependencies (see core/__init__.py).
"""

from adapter_sdk.capabilities import AdapterCapabilities, Capability, SupportLevel
from adapter_sdk.interfaces import (
    AdapterHealth,
    AdapterKind,
    AgentAdapter,
    BaseAdapter,
    EventSink,
)

__all__ = [
    "AdapterCapabilities",
    "AdapterHealth",
    "AdapterKind",
    "AdapterCapabilities",
    "AgentAdapter",
    "BaseAdapter",
    "Capability",
    "EventSink",
    "SupportLevel",
]
