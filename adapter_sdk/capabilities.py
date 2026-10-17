"""Capability declarations: what an adapter can actually observe.

The point is honesty, not marketing. A log-parsing adapter that sees an LLM
response but never the tool's start time must say tool_calls=PARTIAL and let
the UI render "this field was not captured" — instead of shipping an empty
string that looks like an empty argument list.

Levels are deliberately four, not two: `unknown` (not yet verified) is the
DEFAULT and is different from `unsupported` (verified impossible via this
integration path). Conformance runs promote unknown -> supported/partial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Capability(str, Enum):
    LLM_CALLS = "llm_calls"
    TOOL_CALLS = "tool_calls"
    SESSION_LIFECYCLE = "session_lifecycle"
    AGENT_LIFECYCLE = "agent_lifecycle"
    SUBAGENTS = "subagents"
    STREAMING = "streaming"
    TOKEN_USAGE = "token_usage"
    ERRORS = "errors"
    REPLAY_INPUTS = "replay_inputs"


class SupportLevel(str, Enum):
    SUPPORTED = "supported"        # verified: all events of this kind captured
    PARTIAL = "partial"            # some events or some fields only
    UNSUPPORTED = "unsupported"    # verified impossible via this path
    UNKNOWN = "unknown"            # not yet verified — the honest default


@dataclass
class AdapterCapabilities:
    """A queryable capability map; every level defaults to UNKNOWN.

    `notes` carries the *why* for anything not plainly supported — it is what
    the Integrations page shows next to the level, e.g.
    "rollout files carry no per-request clock; ts is parse time".
    """

    levels: dict[Capability, SupportLevel] = field(default_factory=dict)
    notes: dict[Capability, str] = field(default_factory=dict)

    def level(self, cap: Capability) -> SupportLevel:
        return self.levels.get(cap, SupportLevel.UNKNOWN)

    def set(self, cap: Capability, level: SupportLevel, note: str = "") -> None:
        self.levels[cap] = level
        if note:
            self.notes[cap] = note

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {
            cap.value: {
                "level": self.level(cap).value,
                **({"note": self.notes[cap]} if cap in self.notes else {}),
            }
            for cap in Capability
        }

    @classmethod
    def from_dict(cls, data: dict[str, dict[str, str]]) -> "AdapterCapabilities":
        caps = cls()
        for name, entry in (data or {}).items():
            try:
                cap = Capability(name)
                level = SupportLevel(entry.get("level", "unknown"))
            except ValueError:
                continue  # forward-compat: ignore capabilities from a newer SDK
            caps.set(cap, level, entry.get("note", ""))
        return caps
