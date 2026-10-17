#!/usr/bin/env python3
"""Adapter discovery + compatibility matrix + the SDK's public surface.

The property under test is a security property: discovery must be able to
describe an adapter WITHOUT importing it or running anything it declares. A
manifest that points at a module which raises on import must still be
listable; only an explicit load() touches the module.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


td = Path(tempfile.mkdtemp())
adapter_dir = td / "adapters"
adapter_dir.mkdir()
os.environ["AGENTTRACE_ADAPTER_DIR"] = str(adapter_dir)

# A manifest pointing at a module that EXPLODES on import. Listing must not
# care; loading must fail loudly.
(adapter_dir / "boom.json").write_text(json.dumps({
    "adapter_id": "boom", "module": "boom_module_that_explodes",
    "integration": "hostile manifest",
    "capabilities": {"llm_calls": {"level": "partial", "note": "declared"}},
}), encoding="utf-8")
(adapter_dir / "broken.json").write_text("{not json", encoding="utf-8")
(adapter_dir / "incomplete.json").write_text(json.dumps({"name": "no id"}),
                                             encoding="utf-8")

import adapter_sdk  # noqa: E402
import adapter_sdk.discovery as discovery  # noqa: E402
from adapter_sdk import compatibility  # noqa: E402
from adapter_sdk.capabilities import Capability, SupportLevel  # noqa: E402

entries = {e.adapter_id: e for e in discovery.discover()}
check("builtins discovered", {"hermes", "codex", "pi", "otlp", "langgraph"}
      <= set(entries))
check("hermes is builtin with no importable module",
      entries["hermes"].source == "builtin" and entries["hermes"].module is None)
check("otlp/langgraph declare modules",
      entries["otlp"].module == "adapters.otlp"
      and entries["langgraph"].module == "adapters.langgraph")
check("hostile manifest listed without being imported",
      "boom" in entries and "boom_module_that_explodes" not in sys.modules)
check("manifest capabilities parsed",
      entries["boom"].capabilities.level(Capability.LLM_CALLS)
      == SupportLevel.PARTIAL)
check("malformed manifests skipped, not fatal",
      all(e.adapter_id in entries for e in entries.values()) and len(entries) >= 6)
check("discovery is metadata only (no adapter modules imported)",
      "adapters.langgraph" not in sys.modules)

try:
    discovery.load("boom")
    check("loading a hostile manifest fails loudly", False)
except ImportError:
    check("loading a hostile manifest fails loudly", True)
except KeyError as exc:
    check("loading a hostile manifest fails loudly", False, str(exc))

try:
    discovery.load("hermes")
    check("loading a host-installed adapter explains itself", False)
except KeyError as exc:
    check("loading a host-installed adapter explains itself",
          "installed by its host agent" in str(exc))

lg = discovery.load("langgraph")
check("explicit load works and returns the adapter",
      lg.adapter_id == "langgraph"
      and "adapters.langgraph" in sys.modules)
otlp = discovery.load("otlp")
check("otlp loads", otlp.adapter_id == "otlp")

# --- compatibility matrix ---------------------------------------------------
matrix = {c.adapter_id: c for c in compatibility.all_compatibility()}
check("every registry adapter has a compatibility claim",
      {"hermes", "codex", "pi", "otlp", "langgraph"} <= set(matrix))
check("claims name the test that proves them",
      all(c.verified_by for c in matrix.values()))
check("a claim without a test would read as unverified",
      compatibility.Compatibility("x", "y", "").status == "unverified")

# --- public SDK surface ------------------------------------------------------
check("adapter_sdk exports the contract types",
      all(hasattr(adapter_sdk, n) for n in
          ("AgentAdapter", "EventSink", "AdapterCapabilities", "Capability",
           "SupportLevel", "AdapterHealth", "AdapterKind", "BaseAdapter")))
check("capability levels are four, and unknown is the default",
      {l.value for l in SupportLevel} == {"supported", "partial",
                                          "unsupported", "unknown"}
      and adapter_sdk.AdapterCapabilities().level(Capability.SUBAGENTS)
      == SupportLevel.UNKNOWN)

# --- BaseAdapter satisfies the AgentAdapter protocol --------------------------
from adapter_sdk.interfaces import AgentAdapter  # noqa: E402


class _Mine(adapter_sdk.BaseAdapter):
    adapter_id = "mine"


check("a minimal BaseAdapter satisfies the AgentAdapter protocol",
      isinstance(_Mine(), AgentAdapter))
check("its health check reports ok without a lifecycle",
      _Mine().health_check().ok is True)

# --- convention check: in-tree adapters expose ADAPTER or a class ------------
import adapters.langgraph as lg_mod  # noqa: E402
import adapters.otlp as otlp_mod  # noqa: E402

check("langgraph exposes a loadable adapter class",
      hasattr(lg_mod, "LangGraphAdapter"))
check("otlp exposes a loadable adapter class", hasattr(otlp_mod, "OtplAdapter"))

del os.environ["AGENTTRACE_ADAPTER_DIR"]

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("adapter discovery OK")
