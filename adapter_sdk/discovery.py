"""Adapter discovery — metadata first, execution only on explicit load.

The security model (proposal §4.5): a plugin that can be *discovered* must
not therefore be *executed*. Discovery here reads manifests and entry-point
metadata only; nothing is imported and no install script runs. `load()` is a
separate, explicit call, and the CLI's `adapter test` prints what it is about
to import before importing it.

Three sources, in precedence order:

1. **builtin** — the in-tree registry (hermes, codex, pi, otlp, langgraph).
2. **entry points** — `importlib.metadata` group `agent_trace.adapters`, for
   installed distributions. Only `name`/`version`/`module` metadata is read.
3. **local manifests** — `~/.agenttrace/adapters/*.json` (or
   `$AGENTTRACE_ADAPTER_DIR`), each declaring `adapter_id`, `module`, and
   `capabilities`. Written by the user, so they are a deliberate act; still
   not imported until requested.

Trust levels are explicit and surfaced in `adapter list`, because "where did
this come from" is the first question a reviewer should be able to answer.
"""

from __future__ import annotations

import importlib
import json
import os
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adapter_sdk.capabilities import AdapterCapabilities
from adapter_sdk.interfaces import AdapterKind

#: A manifest larger than this is not a manifest; refusing to read it keeps a
#: hostile or corrupt file from being slurped into memory on every CLI run.
_MAX_MANIFEST = 1 << 20  # 1 MiB


@dataclass
class DiscoveryEntry:
    adapter_id: str
    source: str                      # builtin | entry_point | manifest
    kind: str = "native"
    module: str | None = None
    version: str | None = None
    location: str | None = None
    integration: str = ""
    agent_versions: str = ""
    capabilities: AdapterCapabilities = field(default_factory=AdapterCapabilities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "source": self.source,
            "kind": self.kind,
            "module": self.module,
            "version": self.version,
            "location": self.location,
            "integration": self.integration,
            "agent_versions": self.agent_versions,
            "capabilities": self.capabilities.to_dict(),
        }


def manifest_dir() -> Path:
    override = os.environ.get("AGENTTRACE_ADAPTER_DIR")
    return (Path(override).expanduser() if override
            else Path.home() / ".agenttrace" / "adapters")


def _builtin() -> list[DiscoveryEntry]:
    from adapter_sdk.registry import all_entries

    return [DiscoveryEntry(
        adapter_id=e.adapter_id, source="builtin", kind=e.kind.value,
        module=f"adapters.{e.adapter_id}" if e.adapter_id in ("otlp", "langgraph")
        else None,
        integration=e.integration, agent_versions=e.agent_versions,
        capabilities=e.capabilities) for e in all_entries()]


def _entry_points() -> list[DiscoveryEntry]:
    """Installed distributions declaring `agent_trace.adapters` entry points.

    Metadata only — the module is NOT imported here.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - py3.9
        return []
    out: list[DiscoveryEntry] = []
    try:
        eps = entry_points()
        selected = (eps.select(group="agent_trace.adapters")
                    if hasattr(eps, "select") else eps.get("agent_trace.adapters", []))
    except Exception:  # noqa: BLE001 - a broken dist must not break discovery
        return []
    for ep in selected:
        out.append(DiscoveryEntry(
            adapter_id=ep.name, source="entry_point",
            module=getattr(ep, "value", None),
            location=str(getattr(ep, "dist", "") or ""),
            integration="third-party distribution entry point"))
    return out


def _manifests() -> list[DiscoveryEntry]:
    out: list[DiscoveryEntry] = []
    directory = manifest_dir()
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        # Only regular files, and only small ones. A manifest can be a FIFO, a
        # symlink to /dev/zero or a device node, and `read_text()` on any of
        # those blocks forever — discovery runs on every CLI invocation, so a
        # single such path would hang the whole tool (observed).
        try:
            stat = path.stat()
        except OSError:
            continue
        if not stat_module.S_ISREG(stat.st_mode) or stat.st_size > _MAX_MANIFEST:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or not data.get("adapter_id"):
            continue
        out.append(DiscoveryEntry(
            adapter_id=str(data["adapter_id"]), source="manifest",
            kind=str(data.get("kind") or "native"),
            module=data.get("module"), version=data.get("version"),
            location=str(path), integration=str(data.get("integration") or ""),
            agent_versions=str(data.get("agent_versions") or ""),
            capabilities=AdapterCapabilities.from_dict(
                data.get("capabilities") or {})))
    return out


def discover() -> list[DiscoveryEntry]:
    """All known adapters, metadata only, builtin first, dedup by id."""
    seen: dict[str, DiscoveryEntry] = {}
    for entry in _builtin() + _entry_points() + _manifests():
        seen.setdefault(entry.adapter_id, entry)
    return list(seen.values())


def load(adapter_id: str) -> Any:
    """Explicitly import an adapter and return its `AgentAdapter`.

    Raises KeyError with an actionable message when the adapter declares no
    module (a built-in whose code lives in a fixed place, like the hermes
    plugin, has nothing importable here) or ImportError when the import
    fails. Never gives up silently: a caller that asked to load an adapter
    must learn why it did not happen.
    """
    entry = next((e for e in discover() if e.adapter_id == adapter_id), None)
    if entry is None:
        raise KeyError(f"unknown adapter: {adapter_id}")
    if not entry.module:
        raise KeyError(
            f"adapter {adapter_id!r} has no importable module (source="
            f"{entry.source}); it is installed by its host agent, not loaded "
            "as a Python adapter")
    module = importlib.import_module(entry.module)
    # conventional attribute; every adapter in this repo exposes one
    for attr in ("ADAPTER", "adapter", "OtplAdapter", "LangGraphAdapter"):
        if hasattr(module, attr):
            candidate = getattr(module, attr)
            return candidate() if isinstance(candidate, type) else candidate
    classes = [getattr(module, n) for n in dir(module)
               if isinstance(getattr(module, n), type)
               and getattr(getattr(module, n), "adapter_id", None) == adapter_id]
    if classes:
        return classes[0]()
    raise ImportError(
        f"module {entry.module} for adapter {adapter_id!r} exposes no "
        "ADAPTER instance or AgentAdapter subclass")
