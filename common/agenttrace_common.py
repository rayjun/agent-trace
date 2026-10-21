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
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
# workspace
# --------------------------------------------------------------------------

GIT_ENV = "AGENTTRACE_GIT"          # metadata (default) | full | off
GIT_CAP_ENV = "AGENTTRACE_GIT_MAX"  # bytes of diff to keep
_GIT_DIFF_CAP = 20_000
_GIT_TIMEOUT = 4.0


def git_mode() -> str:
    value = (os.environ.get(GIT_ENV) or "metadata").strip().lower()
    return value if value in ("metadata", "full", "off") else "metadata"


def git_diff_cap() -> int:
    """Bytes of diff to keep — `AGENTTRACE_GIT_MAX`, else the built-in 20 KB.

    The cap is a property of the snapshot this module owns: an adapter records
    its own workspace at capture time and core/workspace.py reads one at index
    time, and both must stop at the same number.
    """
    try:
        return max(0, int(os.environ.get(GIT_CAP_ENV) or _GIT_DIFF_CAP))
    except ValueError:
        return _GIT_DIFF_CAP


def workspace_snapshot(cwd: str | os.PathLike[str] | None, *,
                       at: str = "capture") -> dict | None:
    """The repository state of `cwd`, or None when there is nothing to say.

    Lives here, not in the platform layer, because a deployed Hermes plugin is a
    COPY of this file and can import nothing else; every adapter therefore gets
    the same snapshot from one implementation.

    Read-only (`--no-optional-locks`, so git never takes the user's index lock),
    bounded (timeout, capped diff), and honest about when it looked: `at` is
    "capture" when an adapter saw it live and "index" when it was read later,
    because a months-old trace compared against today's diff is a wrong
    conclusion waiting to happen. The diff is OFF by default — it can carry
    source and credentials — and the snapshot says it withheld it.
    """
    mode = git_mode()
    if mode == "off" or not cwd:
        return None
    try:
        path = Path(str(cwd)).expanduser()
    except (OSError, ValueError):
        return None
    if not path.is_dir():
        return None

    def git(args: list[str]) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "--no-optional-locks", *args], cwd=str(path),
                capture_output=True, text=True, timeout=_GIT_TIMEOUT,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0",
                     "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    root = git(["rev-parse", "--show-toplevel"])
    if not root:
        return None                       # not a repository: say nothing at all
    out: dict = {"at": at, "root": root.strip()}

    status = git(["status", "--porcelain=v1", "--branch"])
    if status is not None:
        lines = status.splitlines()
        if lines:
            head = lines[0][3:] if lines[0].startswith("## ") else ""
            if head.startswith("HEAD"):
                out["detached"] = True
            elif head:
                out["branch"] = head.split("...")[0].split()[0]
        # `??` is untracked, which is exactly what an agent creating a file
        # looks like, and a diff of tracked files would never show it.
        changed = [line[3:] for line in lines[1:] if len(line) > 3]
        if changed:
            out["dirty"] = True
            out["changed_count"] = len(changed)
            out["changed_files"] = changed[:50]
            if len(changed) > 50:
                out["changed_files"].append(f"... and {len(changed) - 50} more")

    sha = git(["rev-parse", "--short", "HEAD"])
    if sha:
        out["head"] = sha.strip()
        subject = git(["log", "-1", "--pretty=%s"])
        if subject:
            out["subject"] = subject.strip()[:200]

    if mode == "full":
        diff = git(["diff", "--no-color", "HEAD"]) or ""
        untracked = git(["ls-files", "--others", "--exclude-standard"])
        if untracked:
            diff += "\n" + "\n".join(f"?? {n}" for n in untracked.splitlines()[:50])
        cap = git_diff_cap()
        if len(diff) > cap:
            out["diff"] = diff[:cap]
            out["diff_truncated"] = True
        else:
            out["diff"] = diff
    else:
        out["diff_withheld"] = True
    return out


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


def redact_string(value: str, extra: Callable[[str], str] | None = None) -> str:
    """Scrub one string: built-in pattern first, then the host's redactor."""
    if not redact_enabled():
        return value
    out = _SECRET_RE.sub("sk-***", value)
    if extra is not None:
        try:
            out = extra(out)
        except Exception:  # noqa: BLE001 - never drop a field over redaction
            return out
    return out


def redact_record(record: dict, extra: Callable[[str], str] | None = None) -> dict:
    """Redact one record's VALUES, then serialise. Never the serialised line.

    This used to run on the finished JSON line, and that is a data-corruption
    bug, not a style choice: a redactor is a text transformer, and a pattern
    that ends inside a JSON escape (`...Bearer abc\\"` ) can consume the
    closing backslash. The replacement then leaves a bare quote, the line
    stops being JSON, and every reader silently skips it — which is exactly
    how five `llm_request` records on a real machine became unparseable while
    the agent kept writing.

    Structure is json.dumps' job alone; secrets are removed from the values
    before that. Cost is the same walk either way (one pass over the strings).
    """
    if not redact_enabled():
        return record

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return redact_string(value, extra)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(v) for v in value]
        return value

    out = walk(record)
    return out if isinstance(out, dict) else record


# --------------------------------------------------------------------------
# ingest faults — the one place an event can disappear
# --------------------------------------------------------------------------

#: 0/false/no turns the fault log off. On by default: an observability layer
#: that silently drops the event it was asked to record is worse than none.
FAULTS_ENV = "AGENTTRACE_FAULTS"
#: One line per (adapter, kind) per window. A full disk fails EVERY event;
#: writing one line per failure writes thousands of lines to a failing disk.
FAULT_WINDOW_S = 60.0
#: Bounded so a week-long outage cannot fill the disk it is reporting about.
#: One rotation (`.1`) is enough history for a doctor check.
FAULT_MAX_BYTES = 256 * 1024

_fault_lock = threading.Lock()
_last_fault: dict[tuple[str, str], float] = {}


def faults_enabled() -> bool:
    return _truthy_default_on(FAULTS_ENV)


def platform_dir() -> Path:
    """`~/.agenttrace` — the platform's own directory, never a trace directory.

    The SQLite index, the server token and this fault log live here. Trace
    dirs are scanned for `*.jsonl`; a log written inside one would be indexed
    as a trace. `AGENTTRACE_PLATFORM_DIR` overrides, and is the same variable
    server/main.py reads — one location, not two.
    """
    override = os.environ.get("AGENTTRACE_PLATFORM_DIR")
    return Path(override).expanduser() if override else Path.home() / ".agenttrace"


def faults_path() -> Path:
    return platform_dir() / "ingest-faults.jsonl"


def record_fault(adapter_id: str, kind: str, error: str,
                 path: str | Path | None = None) -> bool:
    """Append one line saying an event was NOT written. Never raises.

    Why a separate file, and why outside every trace dir: the condition being
    recorded is usually "this filesystem is full or read-only", so the record
    of it has to be able to live on another one.

    Throttling is checked twice, because both shapes occur in this repo:
    in-process (Hermes runs the plugin on its own threads) via `_last_fault`,
    and cross-process (the Codex hook is a fresh process per event) by reading
    the file's last line. Without the second check a failing codex hook emits
    one fault line per dropped event anyway.

    Returns True when a line was written (so tests can assert the throttle).
    """
    if not faults_enabled():
        return False
    now = datetime.now(timezone.utc).timestamp()
    key = (str(adapter_id), str(kind))
    try:
        with _fault_lock:
            last = _last_fault.get(key)
            if last is not None and now - last < FAULT_WINDOW_S:
                return False
            dest = faults_path()
            _last_fault[key] = now
            if _recent_on_disk(dest, key, now):
                return False
            return _append_fault(dest, adapter_id, kind, error, path)
    except Exception:  # noqa: BLE001 - the fault log must never break an agent
        return False


def _recent_on_disk(dest: Path, key: tuple[str, str], now: float) -> bool:
    """True when the file's last line already reports this key inside the window."""
    try:
        with dest.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 1024))
            tail = fh.read().decode("utf-8", "replace").splitlines()
        rec = json.loads(tail[-1])
        if (rec.get("adapter_id"), rec.get("kind")) != key:
            return False
        prev = datetime.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc).timestamp()
        return now - prev < FAULT_WINDOW_S
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False


def _append_fault(dest: Path, adapter_id: str, kind: str, error: str,
                  path: str | Path | None) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dest.stat().st_size > FAULT_MAX_BYTES:
                os.replace(dest, dest.with_suffix(dest.suffix + ".1"))
        except OSError:
            pass
        rec: dict[str, object] = {
            "ts": now_iso(),
            "adapter_id": str(adapter_id),
            "kind": str(kind),
            "error": str(error)[:300],
        }
        if path:
            rec["path"] = str(path)
        append_line(dest, json.dumps(rec, ensure_ascii=False))
        return True
    except OSError:
        return False


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
    rec = redact_record(rec, extra_redact)
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        # An unserialisable field is an adapter bug, and the record is gone.
        # Recording it is the difference between "this trace is incomplete"
        # and "there is no trace and no reason why".
        record_fault(file_prefix, "serialise", f"{type(exc).__name__}: {exc}")
        return None
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = trace_dir / f"{file_prefix}-{day}.jsonl"
    try:
        trace_dir.mkdir(parents=True, exist_ok=True)
        append_line(path, line)
    except OSError as exc:
        # Full disk, read-only dir, permissions: the event is gone. Say so
        # somewhere that outlives this process — see `record_fault`.
        record_fault(file_prefix, "write", f"{type(exc).__name__}: {exc}",
                     path=path)
        return None
    return path
