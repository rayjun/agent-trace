"""agent-trace — local LLM input/output tracer for Hermes Agent.

Writes one JSONL record per observed event to ~/.hermes/traces/hermes-YYYYMMDD.jsonl
in the shared agent-trace format (schema/trace.schema.json), so `agenttrace` reads
Hermes, Codex and Pi traces with one CLI.

Privacy: content is written in cleartext to a local file. Nothing leaves the
machine. Set AGENTTRACE_CAPTURE=metadata to drop message bodies and keep only
counts and ids.

Hooks used (payload fields verified against agent/turn_api_request.py and
website/docs/user-guide/features/hooks.md):
  pre_api_request  -> request_messages, system_prompt, message/tool counts, request
  post_api_request -> response, assistant_message, usage, api_duration
  api_request_error-> error, status_code, retry counters
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

AGENT = "hermes"
SCHEMA_V = 1

# Optional dep from the Hermes runtime. Imported lazily so the plugin still loads
# in test harnesses that stub the lifecycle module.
try:
    from agent.redact import redact_sensitive_text  # pyright: ignore[reportMissingImports]
except Exception:  # pragma: no cover
    def redact_sensitive_text(text: str, force: bool = False) -> str:
        return text


_write_lock = threading.Lock()


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _hermes_home() -> Path:
    v = os.environ.get("HERMES_HOME")
    if v:
        return Path(v).expanduser()
    return Path.home() / ".hermes"


def _trace_dir() -> Path:
    d = _hermes_home() / "traces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _capture_mode() -> str:
    """full (default) | metadata. metadata keeps structure, drops content."""
    return (os.environ.get("AGENTTRACE_CAPTURE") or "full").strip().lower()


def _max_chars() -> int:
    try:
        return max(0, int(os.environ.get("AGENTTRACE_MAX_CHARS") or 200_000))
    except ValueError:
        return 200_000


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ms(seconds) -> int | None:
    """Hermes hands the hooks `api_duration` in *seconds* as a float.

    agent/turn_response_check.py computes it as `time.time() - api_start_time`,
    and agent/api_request_hooks.py passes `ended_at - api_start_time` to the
    error hook. The shared schema types `duration_ms` as integer milliseconds,
    so convert here — writing the raw float made every real record violate the
    schema and rendered as `2ms` in the panel for what was a 2-second call.
    """
    if seconds is None or isinstance(seconds, bool):
        return None
    if not isinstance(seconds, (int, float)):
        return None
    return max(0, int(round(float(seconds) * 1000)))


def _ms_int(milliseconds) -> int | None:
    """Pass through a duration that is ALREADY in milliseconds.

    The tool hooks are the opposite of the API hooks: model_tools.py computes
    `_elapsed_ms` as `int((time.monotonic() - start) * 1000)`, so
    `post_tool_call.duration_ms` arrives in ms while `api_duration` arrives in
    seconds. Running either through the other's conversion inflates tool
    timings by 1000x or truncates them to 0.
    """
    if milliseconds is None or isinstance(milliseconds, bool):
        return None
    if not isinstance(milliseconds, (int, float)):
        return None
    return max(0, int(milliseconds))


# --------------------------------------------------------------------------
# shaping
# --------------------------------------------------------------------------

def _truncate(obj, limit: int):
    """Bound one string field; returns the value unchanged if it fits."""
    if isinstance(obj, str) and len(obj) > limit:
        return obj[:limit] + f"\n...[truncated {len(obj) - limit} chars]"
    return obj


def _coerce_text(v) -> str:
    """Normalize a message content field (str, or provider part list) to text."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        out = []
        for part in v:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                t = part.get("text") or part.get("content") or part.get("thinking")
                if t:
                    out.append(str(t))
        return "\n".join(out)
    return str(v)


def _shape_messages(raw) -> list[dict]:
    """Keep role + content; drop provider-specific noise but keep tool calls."""
    out = []
    for m in raw or []:
        if not isinstance(m, dict):
            continue
        row = {"role": m.get("role")}
        content = m.get("content")
        row["content"] = _coerce_text(content)
        tcs = m.get("tool_calls")
        if tcs:
            row["tool_calls"] = _shape_tool_calls(tcs)
        if m.get("tool_call_id"):
            row["tool_call_id"] = m["tool_call_id"]
        reasoning = m.get("reasoning") or m.get("reasoning_content")
        if reasoning:
            row["reasoning"] = _truncate(_coerce_text(reasoning), _max_chars())
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "thinking":
                    row["reasoning"] = _truncate(str(part.get("thinking", "")), _max_chars())
        for k in ("name",):
            if m.get(k):
                row[k] = m[k]
        out.append(row)
    return out


def _get(obj, *names, default=None):
    """Read a field from a dict or an SDK object (attribute or mapping key)."""
    for n in names:
        if isinstance(obj, dict):
            if n in obj:
                return obj[n]
        else:
            v = getattr(obj, n, None)
            if v is not None:
                return v
    return default


def _result_text(result) -> str:
    """Flatten one tool result to displayable text.

    Tool handlers return a JSON string (tools/registry.py wraps every payload
    with json.dumps), but the field worth showing differs per tool: terminal
    puts the text in `output`, file tools in `content`, and a refusal carries a
    human sentence in `error`. Rendering the whole JSON blob puts that useful
    part behind a wall of boilerplate, so pick the best field and fall back to
    the raw text.
    """
    if result is None:
        return ""
    if isinstance(result, (dict, list)):
        data = result
    else:
        if not isinstance(result, str):
            return str(result)
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return result
        if not isinstance(data, (dict, list)):
            return result

    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return _coerce_text(data)

    for key in ("output", "content", "text", "result", "error", "message"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return json.dumps(data, ensure_ascii=False)


def _usage(raw) -> dict | None:
    if not isinstance(raw, dict):
        raw = _as_dict(raw)
    if not raw:
        return None
    def num(*keys):
        for k in keys:
            v = raw.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                return int(v)
            if isinstance(v, str):
                try:
                    return int(float(v))
                except ValueError:
                    continue
        return None
    u = {
        "input_tokens": num("prompt_tokens", "input_tokens"),
        "output_tokens": num("completion_tokens", "output_tokens"),
        "cache_read_tokens": num("cache_read_input_tokens", "cache_read_tokens"),
        "cache_write_tokens": num("cache_creation_input_tokens", "cache_write_tokens"),
        "reasoning_tokens": num("reasoning_tokens"),
    }
    out = {k: v for k, v in u.items() if v is not None}
    if not out:
        return None
    # keep provider extras (total_tokens, request_count, ...) for cost analysis
    for k, v in raw.items():
        if k not in out and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = int(v)
    return out


def _as_dict(obj) -> dict:
    """Best-effort dict view of a provider SDK message object."""
    if isinstance(obj, dict):
        return obj
    if obj is None:
        return {}
    for meth in ("model_dump", "to_dict", "dict"):
        fn = getattr(obj, meth, None)
        if callable(fn):
            try:
                d = fn()
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return {k: v for k, v in d.items() if not k.startswith("_")}
    return {}


def _shape_tool_calls(raw) -> list[dict]:
    out = []
    for tc in raw or []:
        fn = _get(tc, "function", default={}) or {}
        out.append({
            "id": _get(tc, "id"),
            "name": _get(fn, "name") or _get(tc, "name"),
            "arguments": _truncate(_get(fn, "arguments") or _get(tc, "arguments"), _max_chars()),
        })
    return out


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------

def _write(record: dict) -> None:
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        return
    if os.environ.get("AGENTTRACE_REDACT", "1") not in ("0", "false", "no"):
        try:
            line = redact_sensitive_text(line, force=True)
        except Exception:
            pass
    path = _trace_dir() / f"hermes-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    with _write_lock:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def _emit(event: str, **fields) -> None:
    rec = {"v": SCHEMA_V, "ts": _now(), "agent": AGENT, "event": event}
    rec.update({k: v for k, v in fields.items() if v is not None})
    _write(rec)


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------

def on_pre_api_request(**kw) -> None:
    """Observer hook: fires per provider attempt, immediately before the request."""
    request = kw.get("request") or {}
    api_mode = kw.get("api_mode") or ""
    meta = _capture_mode() == "metadata"

    # The hook wraps provider kwargs one level deep — see
    # _api_request_payload_for_hook: {"method": "POST", "body": {model,
    # messages, tools, instructions, ...}}. Reading tools off the OUTER
    # object silently yielded None (the panel could only show a count),
    # while messages survived only because `request_messages` is passed as
    # a separate kwarg. Unwrap first; fall back to the flat shape.
    body = request.get("body") if isinstance(request, dict) else None
    if not isinstance(body, dict):
        body = request if isinstance(request, dict) else {}

    messages = kw.get("request_messages")
    if not messages:
        messages = body.get("messages") or body.get("input") or []

    system_prompt = kw.get("system_prompt")
    instructions = body.get("instructions")
    # Keep tool NAMES, not full schemas: 25 schemas are tens of KB per
    # request, and the panel's promise is `tools read, write, patch` — the
    # name list is what says what the model can call. schema/trace.schema
    # .json accepts an array here.
    raw_tools = body.get("tools")
    tools = None
    if isinstance(raw_tools, list):
        names = []
        for t in raw_tools:
            if isinstance(t, dict):
                n = t.get("name") or (t.get("function") or {}).get("name")
            else:
                n = t
            if n:
                names.append(str(n))
        tools = names or None

    req: dict = {
        "messages": [] if meta else _shape_messages(messages),
        "system_prompt": None if meta else _truncate(system_prompt, _max_chars()),
        "instructions": None if meta else _truncate(instructions, _max_chars()),
        "tools": None if meta else tools,
        "tool_count": kw.get("tool_count") if kw.get("tool_count") is not None
                      else (len(raw_tools) if isinstance(raw_tools, list) else None),
        "message_count": kw.get("message_count"),
        "char_count": kw.get("request_char_count"),
        "approx_input_tokens": kw.get("approx_input_tokens"),
        "max_tokens": kw.get("max_tokens"),
    }

    _emit(
        "llm_request",
        request_id=kw.get("api_request_id"),
        turn_id=kw.get("turn_id"),
        session_id=kw.get("session_id"),
        provider=kw.get("provider"),
        model=kw.get("model"),
        api_mode=api_mode,
        base_url=kw.get("base_url"),
        request=req,
        note=f"api_call={kw.get('api_call_count')} retry={kw.get('retry_count')}",
    )


def on_post_api_request(**kw) -> None:
    """Observer hook: fires after a normalized provider success."""
    # `assistant_message` is a provider SDK object, not a dict. `response` is the
    # host's sanitized dict view (model/finish_reason/assistant_message/usage).
    assistant = kw.get("assistant_message")
    if assistant is None:
        assistant = (kw.get("response") or {}).get("assistant_message") \
            if isinstance(kw.get("response"), dict) else None
    am = _as_dict(assistant)
    content = _coerce_text(am.get("content"))
    reasoning = _coerce_text(am.get("reasoning") or am.get("reasoning_content"))
    tool_calls = _shape_tool_calls(am.get("tool_calls"))

    meta = _capture_mode() == "metadata"
    resp = {
        "content": None if meta else _truncate(content, _max_chars()),
        "reasoning": None if meta else _truncate(reasoning, _max_chars()),
        "tool_calls": tool_calls,
        "finish_reason": kw.get("finish_reason"),
        "usage": _usage(kw.get("usage")),
    }

    _emit(
        "llm_response",
        request_id=kw.get("api_request_id"),
        turn_id=kw.get("turn_id"),
        session_id=kw.get("session_id"),
        provider=kw.get("provider"),
        model=kw.get("model"),
        api_mode=kw.get("api_mode"),
        response=resp,
        duration_ms=_ms(kw.get("api_duration")),
        note=f"response_model={kw.get('response_model')}",
    )


def on_api_request_error(**kw) -> None:
    """Observer hook: fires on each failed provider attempt."""
    err = kw.get("error") or {}
    message = err.get("message") if isinstance(err, dict) else str(err)
    _emit(
        "llm_error",
        request_id=kw.get("api_request_id"),
        turn_id=kw.get("turn_id"),
        session_id=kw.get("session_id"),
        provider=kw.get("provider"),
        model=kw.get("model"),
        api_mode=kw.get("api_mode"),
        duration_ms=_ms(kw.get("api_duration")),
        error={
            "type": err.get("type") if isinstance(err, dict) else type(err).__name__,
            "message": _truncate(str(message or ""), 4000),
            "status_code": kw.get("status_code"),
            "retryable": kw.get("retryable"),
            "retry_count": kw.get("retry_count"),
        },
        note=f"reason={kw.get('reason')} max_retries={kw.get('max_retries')}",
    )


def on_pre_llm_call(**kw) -> None:
    """Directive hook: once per turn before the loop. Records the user prompt."""
    history = kw.get("conversation_history") or []
    user = kw.get("user_message") or ""
    _emit(
        "user_prompt",
        session_id=kw.get("session_id"),
        turn_id=kw.get("turn_id"),
        model=kw.get("model"),
        request={"messages": [{"role": "user", "content": _truncate(str(user), _max_chars())}],
                 "message_count": len(history) + 1},
        note=f"platform={kw.get('platform')} first_turn={kw.get('is_first_turn')}",
    )


def on_post_llm_call(**kw) -> None:
    """Observer hook: successful turn finalization."""
    _emit(
        "assistant_message",
        session_id=kw.get("session_id"),
        turn_id=kw.get("turn_id"),
        model=kw.get("model"),
        response={"content": _truncate(str(kw.get("assistant_response") or ""), _max_chars())},
    )


def on_session_start(**kw) -> None:
    """Observer hook: fires once per session when a conversation opens.

    agent/conversation_loop.py fires this with session_id/model/platform only,
    so there is no request-side content to record — it exists so a reader can
    see a session open even when no LLM call follows it (empty turn, refusal,
    or a run that dies before the first request).
    """
    _emit(
        "session_start",
        session_id=kw.get("session_id"),
        model=kw.get("model"),
        note=f"platform={kw.get('platform')}",
    )


def on_session_end(**kw) -> None:
    _emit(
        "session_end",
        session_id=kw.get("session_id"),
        model=kw.get("model"),
        note=f"completed={kw.get('completed')} interrupted={kw.get('interrupted')} "
             f"reason={kw.get('turn_exit_reason') or kw.get('reason')}",
    )


def on_post_tool_call(**kw) -> None:
    """Observer hook: every tool execution, with the result.

    model_tools.py::_emit_post_tool_call_hook passes tool_name/args/result plus
    status and duration_ms. One firing is both sides of a tool interaction, so
    it is emitted as a `tool_call` carrying the arguments and a `tool_result`
    carrying the outcome — that is what makes the tool sequence of a turn
    reconstructable from the trace alone. `llm_response.tool_calls` still holds
    the model's intent; these two hold what actually ran.
    """
    name = kw.get("tool_name") or "?"
    call_id = kw.get("tool_call_id") or None
    common = {
        "session_id": kw.get("session_id"),
        "turn_id": kw.get("turn_id"),
        "model": kw.get("model"),
    }

    _emit("tool_call", **common,
          tool={"name": name, "call_id": call_id,
                "args": _truncate(kw.get("args"), _max_chars())})

    status = kw.get("status") or "ok"
    _emit(
        "tool_result",
        **common,
        tool={"name": name, "call_id": call_id, "status": status},
        response={"content": _truncate(_result_text(kw.get("result")), _max_chars())},
        duration_ms=_ms_int(kw.get("duration_ms")),
        error=({
            "type": kw.get("error_type") or "tool_error",
            "message": _truncate(str(kw.get("error_message") or ""), 4000),
        } if status == "error" else None),
    )


def register(ctx) -> None:
    # Register unconditionally, like the bundled observability/langfuse plugin.
    # Do NOT gate on has_hook() here: discovery has not finished at register time,
    # so has_hook() returns False and the plugin would silently register nothing.
    hooks = (
        ("pre_api_request", on_pre_api_request),
        ("post_api_request", on_post_api_request),
        ("api_request_error", on_api_request_error),
        ("pre_llm_call", on_pre_llm_call),
        ("post_llm_call", on_post_llm_call),
        ("post_tool_call", on_post_tool_call),
        ("on_session_start", on_session_start),
        ("on_session_end", on_session_end),
    )
    for name, fn in hooks:
        ctx.register_hook(name, fn)
