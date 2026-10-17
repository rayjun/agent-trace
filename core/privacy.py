"""Redaction for the v2 pipeline — the capture-time gate (decision D7).

The v1 path scrubs secrets inside `common/agenttrace_common.py` before a line
hits disk. The v2-native adapters (langgraph, otlp-imported payloads, any
third-party sink) did NOT, which meant "Redaction is the adapter's job, before
the event touches disk" was true only of the legacy half of the system.

This module is the shared implementation for everything that writes canonical
events. `JsonlEventSink` applies it by default, so a new adapter gets the
guarantee by using the SDK rather than by remembering to opt in.

Scope, honestly stated: this catches the shapes that are cheap and reliable to
detect (provider key prefixes, bearer tokens, private key blocks, obvious
`password=`/`api_key=` assignments, AWS/GitHub/Google token prefixes). It is a
seatbelt, not a DLP system — adapters handling known-sensitive fields should
redact those themselves and say so in `privacy`.
"""

from __future__ import annotations

import os
import re
from typing import Any

REDACT_ENV = "AGENTTRACE_REDACT"

#: Values are replaced by this marker so a reader can tell "withheld" from
#: "genuinely empty" — the distinction D7 exists to preserve.
MARKER = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|token|"
               r"access[_-]?key|client[_-]?secret)\b\s*[:=]\s*[\"']?([^\s\"',}]{8,})"),
)


def redact_enabled() -> bool:
    """Default ON, matching the v1 switch's semantics."""
    return (os.environ.get(REDACT_ENV) or "1").strip().lower() not in (
        "0", "false", "no", "off")


def redact_text(text: str) -> str:
    out = text
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            out = pattern.sub(lambda m: f"{m.group(1)}={MARKER}", out)
        else:
            out = pattern.sub(MARKER, out)
    return out


def redact(value: Any) -> Any:
    """Recursively scrub one JSON-ish value. Never raises, never mutates.

    This is the PUBLIC entry point and it is key-aware: `{"api_key": "..."}`
    is redacted by NAME as well as by value shape. An earlier split where the
    public `redact()` matched values only and the key-aware walk lived in a
    private helper is exactly how `input.api_key` got scrubbed while
    `attributes.api_key` leaked — two code paths, one guarantee. There is now
    one path.
    """
    return _redact_payload(value)


def redact_text_only(value: Any) -> Any:
    """Value-pattern scrubbing without key awareness (used while recursing)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_text_only(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_text_only(v) for v in value]
    return value


#: Assignment-style secrets whose NAME marks them regardless of the value's
#: shape. Applied to tool args, request/response bodies AND extension
#: attributes, so the same key cannot be scrubbed in one place and leak in
#: another.
_ALWAYS_KEYS = frozenset({
    "api_key", "apikey", "authorization", "password", "passwd",
    "secret", "client_secret", "access_token", "refresh_token",
    "private_key", "session_token", "cookie", "credentials",
})


#: Keys whose values are replaced only when the value looks like a credential.
#: `token` is a credential in a request body and a counter in a usage block;
#: `key` is a Redis/KV operand as often as it is an API key — redacting it
#: unconditionally destroyed the operand a trace exists to show. `auth` is a
#: scheme name (`auth: oauth`) as often as it is a secret, so it is decided by
#: value shape; `authorization` (the header) stays unconditional above.
_AMBIGUOUS_KEYS = frozenset({"token", "key", "auth", "secret_key",
                             "api_key_id", "bearer"})


def redact_event(event: dict[str, Any]) -> dict[str, Any]:
    """Redact one canonical event and mark it as redacted.

    Returns a new dict. `privacy.redacted` is set to True so a reader knows
    the scrub ran. Must never raise: this runs on the path into the host's
    callback, and a malformed `privacy` field must degrade to a dropped flag,
    not an exception inside the agent.
    """
    if not redact_enabled():
        return event
    try:
        out = dict(event)
        for key in ("span", "event"):
            body = out.get(key)
            if isinstance(body, dict):
                out[key] = _redact_body(body)
        raw_privacy = out.get("privacy")
        # `privacy` is documented as an object; a string (or anything else) in
        # that slot previously crashed `dict(str)`. Treat it as absent.
        privacy = dict(raw_privacy) if isinstance(raw_privacy, dict) else {}
        privacy.setdefault("capture", "full")
        privacy["redacted"] = True
        out["privacy"] = privacy
        return out
    except Exception:  # noqa: BLE001 - never break the host over redaction
        return event


def _redact_body(body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key in ("input", "output"):
            # Key-aware: `input.api_key` is a leak by NAME, whether or not the
            # value happens to match a token pattern.
            out[key] = _redact_payload(value)
        elif key == "error" and isinstance(value, dict):
            out[key] = {k: (redact_text(v) if isinstance(v, str) else v)
                        for k, v in value.items()}
        elif key == "attributes" and isinstance(value, dict):
            # Also key-aware. Routing attributes through value-only `redact()`
            # left `attributes.api_key` in the clear while the identical key
            # under `input` was scrubbed — an inconsistency with no defensible
            # rationale.
            out[key] = _redact_payload(value)
        else:
            out[key] = value
    return out


def _is_credentialish(value: Any) -> bool:
    """True for values that look like a secret rather than a name or a counter.

    The distinction that matters in practice is separators: real API tokens are
    one opaque run of characters (`abcdefghijklmnop`, `aB3xY9...`), while the
    identifiers that appear under generic keys in tool arguments are readable
    snake_case or kebab-case names (`customer_profile_42`, `cache-key`).
    Redacting those destroyed the operand a trace exists to show, so a value
    containing `_`, `-` or `.` is never treated as credentialish here — genuine
    tokens of that shape (sk-, ghp_, AKIA…) are matched by the explicit
    patterns in `redact_text`, which run regardless of the key.
    """
    if not isinstance(value, str) or len(value) < 12 or " " in value:
        return False
    if any(sep in value for sep in ("_", "-", ".", "/", ":")):
        return False
    if not value.isalnum():
        return False
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    if not has_alpha:
        return False
    return has_digit or value.islower() or value.isupper()


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            lowered = str(k).lower()
            if lowered in _ALWAYS_KEYS:
                out[k] = MARKER
            elif lowered in _AMBIGUOUS_KEYS and _is_credentialish(v):
                # `token: 1234` in a usage block must survive; a 40-char
                # alphanumeric bearer token must not.
                out[k] = MARKER
            else:
                out[k] = _redact_payload(v)
        return out
    if isinstance(value, list):
        return [_redact_payload(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
