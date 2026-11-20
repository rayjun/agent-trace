"""Evidence-backed work review over the latest recorded turn of each session.

This is a reading aid, never a claim that a change is safe to merge. No model,
network request, command execution, or modification of a captured trace occurs.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import json
from pathlib import Path
import re
import shlex
from typing import Any, Iterator

from core import query
from core.storage.sqlite_store import TraceStore

BODY_LIMIT = 16000
CALL_LIMIT = 400
LABELS = {
    "failed": "Checks failed", "passed": "Recorded checks passed",
    "unknown": "Check result unknown", "unverified": "No checks recorded",
    "open": "Turn still open", "empty": "No recorded turns",
}
_SHELL = {"bash", "exec", "exec_command", "shell", "run", "terminal", "command",
          "run_command", "run_terminal_cmd"}
_JS_TOKEN = re.compile(r'''\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|\d+|.''', re.S)
_EXIT = re.compile(r"^(?:Process exited with code|Process exit code:)\s*(-?\d+)\s*$", re.M)
_SESSION = re.compile(r"^Process running with session ID\s+(\d+)\s*$", re.M)
_FAIL = re.compile(r"(?:^\s*(?:FAILED(?:\s|$)|FAIL(?:\s|:|$)|ERROR collecting)|\b[1-9]\d* failed\b|SOME TESTS FAILED)", re.I | re.M)
_INFORMATIONAL_FLAGS = {"--version", "--help", "-h", "-V", "--collect-only", "--co", "--listTests",
                        "--list-tests", "--dry-run", "--showConfig", "--show-config", "--print-config",
                        "--fixtures", "--fixtures-per-test", "--if-present", "--passWithNoTests",
                        "--pass-with-no-tests", "-list"}


def _informational(args: list[str]) -> bool:
    return any(arg.split("=", 1)[0] in _INFORMATIONAL_FLAGS for arg in args)


def _single_check_process(command: str) -> bool:
    """Only associate a successful tool exit with one unambiguous invocation."""
    if any(ch in command for ch in "\n\r;&|<>()`$"):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    return bool(args) and not _informational(args)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(v) for v in value)
    if isinstance(value, dict):
        for key in ("text", "content", "result", "output", "message"):
            if key in value:
                return _text(value[key])
    return ""


def _json(value: str | None) -> Any:
    try:
        return json.loads(value) if value else None
    except (ValueError, TypeError):
        return None


def _literal_tool_values(code: str, tool: str, field: str) -> list[Any]:
    # Strings/comments are atomic. Quoted fixtures are never invocations. This
    # accepts straight-line literal calls; control flow is deliberately opaque.
    tokens = [m[0] for m in _JS_TOKEN.finditer(code)
              if not m[0].isspace() and not m[0].startswith(("//", "/*"))]
    if any(t in {"if", "for", "while", "function", "eval", "Function", "?"} for t in tokens):
        return []
    if any(tokens[i:i + 2] == ["=", ">"] for i in range(len(tokens) - 1)):
        return []
    values = []
    for index in range(len(tokens) - 3):
        if tokens[index:index + 4] != ["tools", ".", tool, "("]:
            continue
        if tokens[index + 4:index + 5] != ["{"]:
            return []
        depth, cursor, found = 1, index + 5, False
        while cursor < len(tokens) and depth:
            token = tokens[cursor]
            if token == "{":
                depth += 1
            elif token == "}":
                depth -= 1
            elif depth == 1 and token.strip("\"'") == field and tokens[cursor + 1:cursor + 2] == [":"]:
                try:
                    values.append(ast.literal_eval(tokens[cursor + 2]))
                except (ValueError, SyntaxError, IndexError):
                    return []
                found = True
                break
            cursor += 1
        if not found:
            return []
    return values


def command_of(name: str, value: Any) -> str | None:
    """Recognize actual shell tool arguments, never quoted test names in prose."""
    if name.rsplit(".", 1)[-1] not in _SHELL:
        return None
    args = value.get("args") if isinstance(value, dict) else None
    if isinstance(args, str):
        parsed = _json(args)
        args = parsed if isinstance(parsed, dict) else args
    if isinstance(args, dict):
        for key in ("cmd", "command", "command_line"):
            command = args.get(key)
            if isinstance(command, str):
                return command
        # Codex orchestration wraps literal exec_command calls in JavaScript.
        # Parse literals only; dynamic code is deliberately not interpreted.
    code = args.get("code") if isinstance(args, dict) else args
    if isinstance(code, str) and "exec_command" in code:
        commands = _literal_tool_values(code, "exec_command", "cmd")
        return "\n".join(commands) if commands and all(isinstance(c, str) for c in commands) else None
    return args if isinstance(args, str) else None


def is_check(command: str) -> bool:
    # shlex preserves quoted separators; split shell lists without matching a
    # test name merely mentioned in echo, grep, a prompt, or an LLM response.
    # Ignore heredoc bodies: Python fixtures or quoted docs often mention
    # pytest without invoking it. Commands after the terminator still count.
    lines, terminator = [], None
    for line in command.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        lines.append(line)
        match = re.search(r'''(?<!<)<<(?!<)-?\s*(?:'([^']+)'|"([^"]+)"|([A-Za-z_]\w*))''', line)
        if match:
            terminator = next(g for g in match.groups() if g is not None)
    command = "\n".join(lines)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return False
    segments: list[list[str]] = [[]]
    for word in words:
        if word and all(c in ";&|()\n" for c in word):
            segments.append([])
        else:
            segments[-1].append(word)
    for tokens in segments:
        while tokens and (tokens[0] in ("env", "uv", "run", "timeout") or "=" in tokens[0]
                          or tokens[0].isdigit()):
            tokens = tokens[1:]
        if not tokens:
            continue
        exe = Path(tokens[0]).name
        args = tokens[1:]
        if _informational(args):
            continue
        if exe in {"pytest", "py.test", "unittest", "vitest", "jest", "tox", "rspec", "ruff", "mypy"}:
            return True
        if exe in {"python", "python3", "python3.10", "python3.12", "python3.14", "bash", "sh", "node"}:
            if len(args) > 1 and args[0] == "-m" and args[1] in {"pytest", "unittest", "ruff", "mypy"}:
                return True
            if args and args[0] == "--test" and exe == "node":
                return True
            if args and not args[0].startswith("-") and re.search(r"(?:^|/)(?:test[^/]*|[^/]+\.test)\.(?:py|sh|mjs|js)$|(?:^|/)test/[^/]+\.(?:py|sh|mjs|js)$", args[0]):
                return True
        if exe == "go" and args[:1] in (["test"], ["vet"]):
            return True
        if exe == "cargo" and args[:1] in (["test"], ["check"], ["clippy"]):
            return True
        if exe in {"npm", "pnpm", "yarn", "bun", "npx", "make"} and args:
            if args[0] == "run":
                args = args[1:]
            if args and args[0] in {"test", "check", "lint", "typecheck", "build", "vitest", "jest", "tsc"}:
                return True
    return False


def _result(output: Any) -> tuple[list[int], str | None, str]:
    """Exit evidence from known tool envelopes; no exit inferred from tool OK."""
    text = _text(output)
    codes: list[int] = []
    session = None
    bodies: list[tuple[int | None, str]] = []
    value = output.get("result", output) if isinstance(output, dict) else output
    if isinstance(value, str):
        value = _json(value)
    values = value if isinstance(value, list) else [value]
    # functions.exec returns a transcript envelope with one JSON tool result
    # per text() call. Read those top-level envelopes, never nested stdout JSON.
    if text.startswith("Script completed\n") and "\nOutput:\n" in text:
        remaining = text.split("\nOutput:\n", 1)[1].lstrip()
        decoder = json.JSONDecoder()
        while remaining:
            try:
                item, end = decoder.raw_decode(remaining)
            except ValueError:
                break
            values.extend(item if isinstance(item, list) else [item])
            remaining = remaining[end:].lstrip()
    for item in values:
        if isinstance(item, dict):
            code = item.get("exit_code")
            if isinstance(code, int) and not isinstance(code, bool):
                codes.append(code)
            if item.get("session_id") is not None and code is None:
                session = str(item["session_id"])
            if (isinstance(code, int) or item.get("session_id") is not None) and "output" in item:
                bodies.append((code, _text(item["output"])))
    # Older shell tools use a text header followed by the actual stdout.
    # A line printed by the command body cannot declare its own exit code.
    if text.startswith(("Chunk ID:", "Wall time:")):
        header = re.split(r"\n(?:Final output|Output):", text, maxsplit=1)[0]
        if not codes:
            codes = [int(m[1]) for m in _EXIT.finditer(header)]
        if session is None and (match := _SESSION.search(header)):
            session = match[1]
    if text.startswith("Script running with cell ID "):
        session = "cell:" + text.splitlines()[0].removeprefix("Script running with cell ID ").strip()
    if bodies:
        failed = [body for code, body in bodies if code is not None and code != 0]
        text = "\n".join(failed if failed else [body for _, body in bodies])
    elif text.startswith("Script completed\n") and not codes and session is None:
        # The orchestration script finishing is not the subprocess finishing.
        # Unattributed text printed by that script cannot establish a pass.
        text = ""
    return codes, session, text


def _poll_key(name: str, args: Any) -> str | None:
    value = args.get("args") if isinstance(args, dict) else None
    if isinstance(value, str):
        value = _json(value) or value
    if isinstance(value, dict):
        if name.rsplit(".", 1)[-1] == "write_stdin":
            return str(value.get("session_id"))
        if name.rsplit(".", 1)[-1] == "wait" and value.get("cell_id"):
            return "cell:" + str(value["cell_id"])
        value = value.get("code")
    if isinstance(value, str) and "write_stdin" in value:
        values = _literal_tool_values(value, "write_stdin", "session_id")
        if len(values) == 1 and isinstance(values[0], (str, int)):
            return str(values[0])
    return None


@contextmanager
def _snapshot(store: TraceStore) -> Iterator[None]:
    conn = store._conn()
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.rollback()


def session_review(store: TraceStore, session_id: str, *,
                   agent: str | None = None) -> dict[str, Any] | None:
    with _snapshot(store):
        return _session_review(store, session_id, agent=agent)


def _session_review(store: TraceStore, session_id: str, *,
                    agent: str | None = None, trace_id: str | None = None) -> dict[str, Any] | None:
    adapter = query._session_agent(store, session_id, agent)
    if adapter is None:
        return None
    session = store.one("SELECT * FROM sessions WHERE adapter_id=? AND session_id=?", (adapter, session_id))
    trace_filter = "" if trace_id is None else " AND trace_id=?"
    latest = store.one(
        "SELECT trace_id, MIN(COALESCE(start_ts,end_ts)) AS started,"
        " MAX(COALESCE(end_ts,start_ts)) AS updated FROM spans"
        " WHERE adapter_id=? AND session_id=?" + trace_filter
        + " GROUP BY trace_id ORDER BY started DESC, trace_id DESC LIMIT 1",
        (adapter, session_id) if trace_id is None else (adapter, session_id, trace_id))
    result: dict[str, Any] = {
        "adapter_id": adapter, "session_id": session_id,
        "cwd": session["cwd"] if session else None,
        "updated_at": session["last_ts"] if session else None,
        "trace_id": latest["trace_id"] if latest else None,
        "task": None, "conclusion": None, "checks": [], "failures": [], "files": [],
        "state": "empty", "open_spans": 0, "limited": False,
        "scope": "Latest recorded turn only; task/conclusion are excerpts. Recorded checks do not verify the current worktree.",
    }
    if latest is None:
        result.update(label=LABELS["empty"], next_action="Start an agent task; no execution has been captured.")
        return result
    params = (adapter, session_id, latest["trace_id"])
    # Bound bodies at the SQL boundary; never load the archive's LLM contexts.
    rows = store.query(
        "SELECT span_id, kind, name, status, ended, start_ts, end_ts,"
        " substr(input_json,1,?) AS input, substr(output_json,1,?) AS output,"
        " substr(error_json,1,2000) AS error, substr(attributes_json,1,?) AS attributes,"
        " length(input_json)>? OR length(output_json)>? OR length(attributes_json)>? AS clipped"
        " FROM spans WHERE adapter_id=? AND session_id=? AND trace_id=? AND synthetic=0"
        " AND kind='tool' ORDER BY COALESCE(start_ts,end_ts), rowid LIMIT ?",
        (BODY_LIMIT, BODY_LIMIT, BODY_LIMIT, BODY_LIMIT, BODY_LIMIT, BODY_LIMIT, *params, CALL_LIMIT + 1))
    result["limited"] = len(rows) > CALL_LIMIT or any(r["clipped"] for r in rows)
    rows = rows[:CALL_LIMIT]
    open_row = store.one("SELECT COUNT(*) FROM spans WHERE adapter_id=? AND session_id=? AND trace_id=? AND synthetic=0 AND ended=0", params)
    result["open_spans"] = open_row[0] if open_row else 0
    for name, field, key in (("user_prompt", "text", "task"), ("assistant_message", "content", "conclusion")):
        events = store.query(
            "SELECT substr(json_extract(payload,?),1,2400) AS text FROM events"
            " WHERE adapter_id=? AND session_id=? AND trace_id=? AND type='event'"
            " AND json_extract(payload,'$.event.name')=? ORDER BY ts "
            + ("ASC,rowid ASC LIMIT 50" if key == "task" else "DESC,rowid DESC LIMIT 1"),
            ("$.event.attributes." + field, *params, name))
        for event in events:
            text = _text(event["text"])
            if key == "task" and text.lstrip().startswith(("<environment_context>", "<codex_internal_context")):
                continue
            if text:
                result[key] = text
                break
    if not result["conclusion"]:
        response = store.one(
            "SELECT substr(json_extract(output_json,'$.content'),1,2400) FROM spans"
            " WHERE adapter_id=? AND session_id=? AND trace_id=? AND kind='llm' AND ended=1"
            " ORDER BY end_ts DESC,rowid DESC LIMIT 1", params)
        if response:
            result["conclusion"] = _text(response[0])
    checks: dict[str, dict[str, Any]] = {}
    pending: dict[str, str] = {}
    files: dict[str, str] = {}
    for row in rows:
        args, out, attrs = _json(row["input"]), _json(row["output"]), _json(row["attributes"]) or {}
        command = command_of(row["name"], args)
        raw_args = args.get("args") if isinstance(args, dict) else None
        code = raw_args.get("code") if isinstance(raw_args, dict) else raw_args
        if command is None and isinstance(code, str) and "exec_command" in code:
            result["limited"] = True  # Dynamic execution cannot be attributed safely.
        poll_key = _poll_key(row["name"], args)
        if poll_key:
            command = pending.get(poll_key)
        codes, session_key, output = _result(out)
        if command and is_check(command):
            if session_key:
                pending[session_key] = command
            exit_value = attrs.get("exit_code")
            if isinstance(exit_value, int) and not isinstance(exit_value, bool):
                codes = [exit_value]
            state = "unknown"
            if (codes and any(code != 0 for code in codes)) or row["status"] in ("error", "interrupted", "cancelled"):
                state = "failed"
            elif _FAIL.search(output):
                # A zero exit with a failing summary is contradictory evidence
                # (shell masking, a wrapper, or a transcript of an earlier run).
                state = "unknown" if codes else "failed"
            elif not row["ended"] or session_key:
                state = "open"
            elif not row["clipped"] and len(codes) == 1 and _single_check_process(command):
                state = "passed"
            checks[command] = {
                "command": command, "state": state, "span_id": row["span_id"],
                "trace_id": latest["trace_id"], "ts": row["end_ts"] or row["start_ts"],
                "exit_codes": codes, "output_excerpt": output[-1800:] if output else None,
            }
        if row["status"] in ("error", "interrupted", "cancelled"):
            result["failures"].append({"span_id": row["span_id"], "name": row["name"],
                                       "trace_id": latest["trace_id"],
                                       "message": _text(_json(row["error"]))[:500] or row["status"]})
        if isinstance(attrs.get("file"), str):
            files[attrs["file"]] = row["span_id"]
    result["checks"] = list(checks.values())
    result["files"] = [{"path": p, "span_id": sid} for p, sid in list(files.items())[:20]]
    result["failures"] = result["failures"][-10:]
    states = {c["state"] for c in checks.values()}
    state = ("failed" if "failed" in states else "open" if result["open_spans"]
             else "unknown" if states & {"unknown", "open"} or result["limited"]
             else "passed" if checks else "unverified")
    result["state"], result["label"] = state, LABELS[state]
    result["next_action"] = {
        "failed": "Open the failed check output, fix the cause, then rerun that command.",
        "open": "Inspect the open turn; missing completion does not prove the agent is still running.",
        "unknown": "Inspect the recorded output or rerun the check; the capture cannot establish its result.",
        "passed": "Review the diff and confirm these checks cover the change before accepting it.",
        "unverified": "Review the agent's conclusion; run appropriate checks if this task changes code.",
    }[state]
    if result["failures"]:
        result["next_action"] += " Inspect recorded tool failures in context; they may have been recovered."
    return result


def session_history(store: TraceStore, session_id: str, *, agent: str,
                    from_turn: str | None = None, limit: int = 12) -> dict[str, Any]:
    with _snapshot(store):
        rows = store.query(
            "SELECT trace_id, MIN(COALESCE(start_ts,end_ts)) AS started FROM spans"
            " WHERE adapter_id=? AND session_id=? GROUP BY trace_id ORDER BY started DESC, trace_id DESC",
            (agent, session_id))
        ids = [row["trace_id"] for row in rows]
        if from_turn is not None:
            if from_turn not in ids:
                raise ValueError("Starting turn does not belong to this session")
            ids = ids[:ids.index(from_turn) + 1]
        return {
            "from_turn": from_turn, "total": len(ids), "limited": len(ids) > limit,
            "turns": [review for trace_id in ids[:limit]
                      if (review := _session_review(store, session_id, agent=agent, trace_id=trace_id)) is not None],
        }


def inbox(store: TraceStore, *, agent: str | None = None, search: str = "",
          limit: int = 12, offset: int = 0, model: str | None = None,
          since: str | None = None) -> dict[str, Any]:
    with _snapshot(store):
        page = query.list_sessions(store, agent=agent, search=search, model=model, since=since,
                                   limit=min(30, max(1, limit)), offset=offset)
        return {**page, "reviews": [r for s in page["sessions"]
                                   if (r := _session_review(store, s["session_id"], agent=s["adapter_id"])) is not None]}


def markdown(review: dict[str, Any]) -> str:
    """Portable handoff with explicit source identities and bounded excerpts."""
    def inline(value: str) -> str:
        fence = "`" * (1 + max((len(m[0]) for m in re.finditer(r"`+", value)), default=0))
        return fence + " " + value.replace("\n", " ") + " " + fence

    def quoted(value: str) -> list[str]:
        return ["    " + line for line in value.splitlines()]

    lines = ["# Agent work review", "", f"Session: {inline(review['adapter_id'] + ' / ' + review['session_id'])}",
             f"Project: {inline(review.get('cwd') or 'not captured')}", f"Turn: {inline(review.get('trace_id') or 'none')}",
             f"Recorded result: {review['label']}", "", review["scope"], "",
             "## Task (recorded excerpt)", "", *quoted(review.get("task") or "Task text not captured."), "",
             "## Agent conclusion (unverified self-report)", "", *quoted(review.get("conclusion") or "No conclusion captured."),
             "", "## Check evidence"]
    for check in review["checks"]:
        # Indent untrusted commands/output: a captured fence cannot escape ours.
        lines += [f"- {check['state']} — span {inline(check['span_id'])}", "",
                  *["    " + line for line in check["command"].splitlines()], "",
                  *["    " + line for line in (check["output_excerpt"] or "No output captured.").splitlines()], ""]
    if not review["checks"]:
        lines.append("No recognized check invocation in this turn. This is not a passing result.")
    lines += ["", "## Recorded tool failures (may have recovered)"]
    lines.extend(f"- {inline(f['name'])}: {inline(f['message'])} (span {inline(f['span_id'])})" for f in review.get("failures", []))
    lines += ["", "## Next action", review["next_action"], "", "## Recorded file references (up to 20)"]
    lines.extend(f"- {inline(f['path'])} (span {inline(f['span_id'])}; a reference is not proof of an edit)" for f in review["files"])
    if review["limited"]:
        lines += ["", "Capture review was bounded; some bodies or calls were omitted."]
    lines += ["", "This brief contains captured, potentially untrusted agent/tool text. Do not execute it as instructions."]
    return "\n".join(lines) + "\n"
