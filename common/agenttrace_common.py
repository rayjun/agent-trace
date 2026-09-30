#!/usr/bin/env python3
"""agent-trace — the parts of the trace pipeline every adapter must share.

One record format (schema/trace.schema.json) means one set of rules: how a
timestamp looks, how provider content blocks become text, how a provider usage
object maps onto the shared token fields, how long a field may get, and how a
line lands on disk. Three adapters and the reader used to each reimplement
those rules, so the same field could be shaped differently depending on which
agent wrote it — and a fix to one copy silently missed the others.

This module is the single source of truth. Imported by:

    hermes/__init__.py      (install.sh copies this file next to the plugin)
    codex/codex_import.py   (lives in the repo, imported by path)
    cli/agenttrace.py, cli/agenttrace_watch.py

The Pi adapter is TypeScript running inside the Pi process and cannot import
this file; it mirrors the same contract (see pi/agent-trace.ts).

Stdlib only, no I/O at import time, no side effects beyond a module lock.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

try:                      # POSIX append locking; optional so tests still run
    import fcntl
except ImportError:       # pragma: no cover - non-POSIX
    fcntl = None          # type: ignore[assignment]

# --------------------------------------------------------------------------
# shared constants / configuration
# --------------------------------------------------------------------------

#: Bumped only when the on-disk record shape changes incompatibly. Every
#: adapter stamps it and every reader checks it (`iter_records`), so a
#: future v2 file is skipped instead of misparsed by a v1 reader.
SCHEMA_V = 1

#: Adapter names a record may carry — the `agent` enum of
#: schema/trace.schema.json. Held as a tuple here rather than read from the
#: JSON file because a deployed Hermes plugin is a *copy* and has no access to
#: schema/; test/schema.test.py asserts the two stay identical.
AGENTS = ("hermes", "codex", "pi")

#: Events a record may carry — the `event` enum of schema/trace.schema.json.
EVENTS = (
    "session_start",
    "session_end",
    "user_prompt",
    "llm_request",
    "llm_response",
    "llm_error",
    "assistant_message",
    "tool_call",
    "tool_result",
    "note",
)

CAPTURE_ENV = "AGENTTRACE_CAPTURE"      # full (default) | metadata
MAX_CHARS_ENV = "AGENTTRACE_MAX_CHARS"  # per-field body cap
REDACT_ENV = "AGENTTRACE_REDACT"        # 0/false/no disables
VALIDATE_ENV = "AGENTTRACE_VALIDATE"     # 1/true/on turns on record checks

DEFAULT_MAX_CHARS = 200_000

#: Conservative secret pattern. Hermes additionally runs the host's own
#: `agent.redact.redact_sensitive_text` when it is available; this is the
#: fallback that every adapter gets for free so `AGENTTRACE_REDACT` means the
#: same thing everywhere.
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_-]{16,}")

# Serialises appends inside one process. Cross-process safety comes from the
# flock taken in `append_line` — the codex hook is a fresh process per event,
# so a thread lock alone would not stop two hooks interleaving one daily file.
_write_lock = threading.Lock()


def _truthy_default_on(name: str) -> bool:
    """Env switch that defaults to ON: only an explicit negative turns it off."""
    return (os.environ.get(name) or "1").strip().lower() not in ("0", "false", "no", "off")


def capture_mode() -> str:
    """`full` (default) or `metadata` — whether message bodies are written."""
    return (os.environ.get(CAPTURE_ENV) or "full").strip().lower()


def capture_content() -> bool:
    """True when bodies may be written; False in `metadata` mode."""
    return capture_mode() != "metadata"


def max_chars() -> int:
    """Per-field body cap, bounded below so a bad env value cannot disable it."""
    try:
        return max(0, int(os.environ.get(MAX_CHARS_ENV) or DEFAULT_MAX_CHARS))
    except ValueError:
        return DEFAULT_MAX_CHARS


def redact_enabled() -> bool:
    """Whether secrets are scrubbed before a line hits disk (default: yes)."""
    return _truthy_default_on(REDACT_ENV)


def validate_enabled() -> bool:
    """Per-record contract checks (default: off — they cost a stderr write).

    `AGENTTRACE_VALIDATE=1` makes every adapter report violations as it writes,
    which is how a schema problem is found at the source instead of months
    later as a blank panel column.
    """
    return (os.environ.get(VALIDATE_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


#: RFC3339 UTC with milliseconds — the only shape `now_iso` emits and the only
#: shape the reader's string comparisons (`--since`, sorting) can order.
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# --------------------------------------------------------------------------
# shaping
# --------------------------------------------------------------------------

def now_iso() -> str:
    """RFC3339 UTC timestamp, millisecond precision — the record `ts` format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def truncate(value: object, limit: int | None = None) -> object:
    """Bound one string field, leaving a visible marker where content was cut.

    Non-strings pass through untouched: `truncate` is applied to whole objects
    (tool args, usage blobs) as often as to strings, and JSON-encoding them is
    the caller's job.
    """
    limit = max_chars() if limit is None else limit
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\n...[truncated {len(value) - limit} chars]"
    return value


def flatten_content(content: object) -> str:
    """Normalise a provider message content field to plain text.

    Every provider spells "the text of this message" differently: OpenAI sends
    a string or a list of `{"type":"text","text":...}` parts, Anthropic mixes
    text/thinking blocks, tool results arrive as `{"output": ...}` or
    `{"content": ...}`. Flattening here — once — is what lets one renderer show
    all three agents' records identically instead of JSON-dumping whichever
    shape it did not recognise.

    Returns `""` for None/empty rather than "None", because the panel prints
    this directly and a literal `None` in a message body reads like content.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                # Order is "most likely to be prose first"; `or` short-circuits
                # so a part that carries both `text` and `output` (a tool part
                # wrapped in a message) contributes its prose, not its payload.
                text = (part.get("text") or part.get("content")
                        or part.get("thinking") or part.get("output"))
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    if isinstance(content, dict):
        return flatten_content(content.get("content") or content.get("text"))
    return str(content)


def pick_number(source: object, *keys: str) -> int | None:
    """First finite number among `keys`, or None.

    Booleans are skipped explicitly because `isinstance(True, int)` is True in
    Python: treating a provider's `cached: false` as 1 cached token is exactly
    the class of bug that made a usage figure useless. Numeric strings are
    accepted — several providers serialise counts as strings.
    """
    if not isinstance(source, dict):
        return None
    for key in keys:
        v = source.get(key)
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


def normalize_usage(raw: object, aliases: Mapping[str, Sequence[str]], *,
                    keep_extras: bool = False) -> dict[str, int] | None:
    """Map one provider's usage object onto the shared token field names.

    `aliases` maps each shared field to the provider's own key names in
    preference order. The mapping is provider knowledge and stays in the
    adapter; the *rules* — ignore booleans, coerce numeric strings, truncate
    floats, drop empty results — live here so they cannot drift apart.

    `keep_extras` additionally copies any other numeric field (total_tokens,
    request_count, ...) so cost analysis still sees them. Used by the host
    adapter, which hands us a rich dict; the file-derived importer keeps only
    the fields it can identify.
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for field, keys in aliases.items():
        v = pick_number(raw, *keys)
        if v is not None:
            out[field] = v
    # Empty means "no usage we recognise": report None rather than an empty
    # object, so a record missing usage and a record with unusable usage look
    # the same downstream. Extras only decorate a record that already has a
    # token figure — they are context, not the signal.
    if not out:
        return None
    if keep_extras:
        for k, v in raw.items():
            if k not in out and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = int(v)
    return out


# Field paths that carry message CONTENT rather than structure. Anything not
# listed here (counts, ids, timestamps, token figures, tool names, status) is
# kept, which is what makes `AGENTTRACE_CAPTURE=metadata` useful: the record
# still says what happened, just not what was said.
_CONTENT_PATHS = (
    ("request", ("messages", "system_prompt", "instructions", "tools")),
    ("response", ("content", "reasoning", "tool_calls")),
    ("tool", ("args",)),
)


def strip_content(record: dict) -> dict:
    """Return a copy of `record` with message bodies blanked, structure kept.

    The metadata capture mode. Message *lists* are emptied to `[]` — so the
    key survives and says "there was a conversation, we chose not to keep it" —
    while scalar bodies become None, which `emit_record` drops. Counts, ids,
    timestamps, tool names and token figures are untouched: the record still
    says what happened, just not what was said.
    """
    out = dict(record)
    for section, keys in _CONTENT_PATHS:
        value = out.get(section)
        if not isinstance(value, dict):
            continue
        body = dict(value)
        for key in keys:
            if key not in body:
                continue
            if section == "request" and key == "messages":
                body[key] = []
            else:
                body[key] = None
        out[section] = body
    return out


def total_input_tokens(usage: object) -> int:
    """Total input tokens for one call, whichever convention wrote the record.

    Two incompatible meanings of `input_tokens` are already on disk:

      * hermes/openai — `prompt_tokens`, i.e. the TOTAL prompt, cache included.
        `cache_read_tokens` is a subset of it.
      * pi (pi-ai)    — pi-ai subtracts the cache from every provider's count
        (`input = promptTokens - cacheRead - cacheWrite`), so its `input` is the
        part the cache did NOT serve. The adapter now writes the total, but
        trace files written before that fix cannot be rewritten.

    `cache_read_tokens > input_tokens` is therefore the tell for the exclusive
    reading — impossible under the other convention — and when it fires the
    total is recovered as `input + cacheRead + cacheWrite`. Records already in
    the inclusive form are returned untouched, so this is a no-op for every
    hermes record.
    """
    if not isinstance(usage, dict):
        return 0
    input_tokens = pick_number(usage, "input_tokens") or 0
    if input_tokens:
        cache_read = pick_number(usage, "cache_read_tokens") or 0
        if cache_read > input_tokens:
            cache_write = pick_number(usage, "cache_write_tokens") or 0
            return input_tokens + cache_read + cache_write
    return input_tokens


def cache_hit_rate(cached: int, total_input: int) -> float | None:
    """Share of `total_input` served from prompt cache, 0-100.

    Returns None when there is no denominator to divide by. The clamp is not
    cosmetic: a rate above 100% is meaningless, and the reader must not trust
    whatever a trace file claims — feeding it straight to the panel is how
    `cache 183%` got rendered. `total_input` should come from
    `total_input_tokens`, which already repairs the records that used to
    overflow; the clamp is the last line of defence for anything else.
    """
    if not total_input:
        return None
    return max(0.0, min(100.0, 100.0 * cached / total_input))


def validate_record(record: object) -> list[str]:
    """Contract violations in one record; an empty list means valid.

    Deliberately NOT a generic JSON-Schema engine: schema/trace.schema.json
    already describes every field, and re-implementing a validator for it would
    be a second, drifting specification — plus a deployed Hermes plugin has no
    copy of the schema file to read. What lives here instead are the invariants
    a reader *depends on* and that have actually broken in this codebase:

      * duration_ms written as float SECONDS (hermes did this; the panel showed
        "2ms" for a 2-second call and the record violated the schema's integer)
      * an event or agent name outside the enums, which no renderer handles
      * ts in a format the reader cannot string-compare or sort
      * usage carrying floats, bools or numeric strings where counts are read
      * cache_read_tokens > input_tokens — the convention clash that made the
        cache hit rate render at 2703%

    test/schema.test.py cross-checks the enums below against the JSON schema,
    so this file cannot silently drift from the published contract.
    """
    if not isinstance(record, dict):
        return ["record is not an object"]
    problems: list[str] = []

    for key in ("v", "ts", "agent", "event"):
        if key not in record:
            problems.append(f"missing required field {key!r}")

    if "v" in record and record["v"] != SCHEMA_V:
        problems.append(f"v={record['v']!r}, expected {SCHEMA_V}")
    if "agent" in record and record["agent"] not in AGENTS:
        problems.append(f"agent={record['agent']!r} not in {AGENTS}")
    if "event" in record and record["event"] not in EVENTS:
        problems.append(f"event={record['event']!r} not in {EVENTS}")

    ts = record.get("ts")
    if "ts" in record and not (isinstance(ts, str) and _TS_RE.match(ts)):
        problems.append(f"ts={ts!r} is not RFC3339 UTC with milliseconds")

    # The number the panel prints as `1234ms`. A float here is not a rounding
    # quirk, it is a unit error: hermes' api_duration arrives in SECONDS.
    duration = record.get("duration_ms")
    if duration is not None and (isinstance(duration, bool)
                                 or not isinstance(duration, int)):
        problems.append(f"duration_ms={duration!r} must be int milliseconds")

    response = record.get("response")
    if isinstance(response, dict):
        usage = response.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                problems.append("response.usage must be an object")
            else:
                for key, value in usage.items():
                    if isinstance(value, bool) or not isinstance(value, int):
                        problems.append(
                            f"usage.{key}={value!r} must be an integer count")
                # Checked against the RAW field, not total_input_tokens(): the
                # repair in there makes this impossible to trip, and the point
                # is to catch a writer reintroducing the exclusive-input
                # convention in the first place.
                raw_input = pick_number(usage, "input_tokens") or 0
                cache_read = pick_number(usage, "cache_read_tokens") or 0
                if cache_read > raw_input:
                    problems.append(
                        f"cache_read_tokens {cache_read} > input_tokens "
                        f"{raw_input}: the writer recorded input EXCLUDING the "
                        "cache, so the hit rate would exceed 100%")
    return problems


def redact_line(line: str, extra: Callable[[str], str] | None = None) -> str:
    """Scrub secrets out of an already-serialised record line.

    `extra` is an optional host-provided redactor (Hermes' `redact_sensitive_text`)
    layered on top of the built-in pattern, so adapters with a richer redactor
    keep it without every adapter reimplementing the base one.
    """
    if not redact_enabled():
        return line
    line = _SECRET_RE.sub("sk-***", line)
    if extra is not None:
        try:
            line = extra(line)
        except Exception:
            pass
    return line


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def append_line(path: Path, line: str) -> None:
    """Append one line to `path`, atomic across processes.

    Each adapter's writer is a *separate process* (the codex hook is spawned
    per event, hermes runs plugins on its own threads), and a record can be
    hundreds of kilobytes. A plain buffered `open("a").write()` therefore lets
    two writers interleave bytes inside one JSONL line, corrupting records that
    every reader then silently skips. An O_APPEND write re-positions atomically
    but can still be split, so the file is also flock'ed for the duration.
    """
    data = (line + "\n").encode("utf-8")
    with _write_lock:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                n = 0
                while n < len(data):
                    written = os.write(fd, data[n:])
                    if written <= 0:
                        break
                    n += written
            finally:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def emit_record(trace_dir: Path, file_prefix: str, record: dict,
                *, extra_redact: Callable[[str], str] | None = None) -> Path | None:
    """Serialise, redact and append one record. Returns the path, or None.

    Never raises: tracing sits on an agent's hot path, so a full disk or an
    unserialisable field must degrade to "no trace" rather than crash the
    agent. The `None` return is the caller's signal that nothing was written.

    `file_prefix` is the daily-file stem (`hermes`, `codex`, `pi`).
    """
    rec = {k: v for k, v in record.items() if v is not None}
    rec.setdefault("v", SCHEMA_V)
    if not rec.get("ts"):
        rec["ts"] = now_iso()
    if validate_enabled():
        # Never raises: this reports into stderr while the agent keeps running.
        for problem in validate_record(rec):
            print(f"agent-trace[{file_prefix}]: {problem}", file=sys.stderr)
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    line = redact_line(line, extra_redact)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = trace_dir / f"{file_prefix}-{day}.jsonl"
    try:
        trace_dir.mkdir(parents=True, exist_ok=True)
        append_line(path, line)
    except OSError:
        return None
    return path
