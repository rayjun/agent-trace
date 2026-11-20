"""Repository review and explicitly recorded local verification."""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
from typing import Any, BinaryIO

from core import query
from core.review import _informational, _snapshot, session_history
from core.review_git import GitReviewError, Worktree, file_diff, worktree, worktree_status
from core.schema.model import now_iso
from core.storage.sqlite_store import TraceStore
from core.workspace import _common

CONFIG = ".agenttrace-review.json"
CHECK_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}\Z")
OUTPUT_LIMIT = 8_000
LABEL = {
    "passed": "Required checks passed on this snapshot",
    "failed": "A current check failed",
    "missing": "Required checks are missing",
    "stale": "Check evidence is out of date",
    "unknown": "Check result is unknown",
    "running": "Check completion has not been recorded",
    "unverified": "Choose which checks this change requires",
}


def _check_name(name: str) -> str:
    if not CHECK_NAME.fullmatch(name):
        raise ValueError("Check names must use letters, digits, dots, underscores or hyphens")
    return name


def _configuration(root: Path) -> dict[str, list[str]]:
    path = root / CONFIG
    try:
        with path.open("rb") as stream:
            body = stream.read(32_001)
    except FileNotFoundError:
        return {}
    if len(body) > 32_000:
        raise ValueError(f"{CONFIG} exceeds 32 KB")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ValueError(f"{CONFIG} must contain valid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("checks"), dict):
        raise ValueError(f'{CONFIG} requires a "checks" object')
    checks = {}
    for key, args in data["checks"].items():
        if not _valid_argv(args):
            raise ValueError(f"Check {key!r} requires a non-empty array of command arguments")
        checks[_check_name(key)] = args
    return checks


def _valid_argv(args: Any) -> bool:
    return isinstance(args, list) and bool(args) and all(isinstance(arg, str) and arg and "\0" not in arg for arg in args)


def _argv_digest(argv: list[str]) -> str:
    return sha256(json.dumps(argv, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _related_sessions(store: TraceStore, root: Path, *, agent: str | None,
                      limit: int = 12) -> list[dict[str, Any]]:
    prefix = str(root).rstrip(os.sep) + os.sep
    return [dict(row) for row in store.query(
        "SELECT adapter_id, session_id, cwd, last_ts FROM sessions"
        " WHERE (cwd=? OR substr(cwd,1,?)=?) AND (? IS NULL OR adapter_id=?)"
        " ORDER BY last_ts DESC, adapter_id, session_id LIMIT ?",
        (str(root), len(prefix), prefix, agent, agent, limit),
    )]


def _session(store: TraceStore, root: Path, sid: str, agent: str | None) -> dict[str, Any]:
    adapter = query._session_agent(store, sid, agent)
    row = store.one("SELECT adapter_id, session_id, cwd, last_ts FROM sessions WHERE adapter_id=? AND session_id=?",
                    (adapter, sid))
    if row is None or not row["cwd"] or not Path(row["cwd"]).resolve().is_relative_to(root):
        raise ValueError("Choose an indexed session from this worktree")
    return dict(row)


def _evidence(store: TraceStore, tree: Worktree, expected: dict[str, list[str]],
              require: list[str]) -> tuple[list[dict[str, Any]], str]:
    rows = store.query(
        """SELECT r.* FROM review_check_runs r
           JOIN (SELECT name, MAX(id) AS id FROM review_check_runs WHERE root=? GROUP BY name) latest
             ON r.id=latest.id ORDER BY r.name""",
        (str(tree.root),),
    )
    by_name = {row["name"]: dict(row) for row in rows}
    checks = []
    names = dict.fromkeys([*expected, *(_check_name(name) for name in require)])
    for name in list(names) + [name for name in by_name if name not in names]:
        evidence = by_name.get(name)
        required = name in names
        argv = expected.get(name)
        if evidence is None:
            checks.append({"name": name, "state": "missing", "required": required,
                           "configured": argv is not None, "argv": _public_argv(argv) if argv else None,
                           "id": None})
            continue
        state = evidence["state"]
        if tree.fingerprint is None:
            state = "unknown"
        elif evidence["fingerprint"] != tree.fingerprint or (
            argv is not None and _argv_digest(argv) != evidence["argv_digest"]
        ):
            state = "stale"
        checks.append({
            **evidence, "argv": json.loads(evidence["argv"]), "required": required,
            "configured": argv is not None,
            "recorded_state": evidence["state"], "state": state,
        })
    required_states = {check["state"] for check in checks if check["required"]}
    if "failed" in required_states or any(check["state"] == "failed" for check in checks):
        state = "failed"
    elif tree.fingerprint is None:
        state = "unknown"
    elif not names:
        state = "unverified"
    else:
        state = next((s for s in ("missing", "stale", "running", "unknown") if s in required_states), "passed")
    return checks, state


def _public_argv(argv: list[str]) -> list[str]:
    if not _common.capture_content():
        return [Path(argv[0]).name]
    return [_common.redact_string(arg) for arg in argv]


def repository_review(store: TraceStore, root: Path, *, required: list[str] | None = None,
                      session: str | None = None, agent: str | None = None,
                      from_turn: str | None = None) -> dict[str, Any]:
    policy_error = None
    try:
        expected = _configuration(root)
    except (OSError, ValueError) as exc:
        expected = {}
        policy_error = str(exc)
    tree = worktree(root)
    if policy_error is None:
        try:
            if expected != _configuration(root):
                policy_error = "Check configuration changed during the snapshot; refresh the review"
        except (OSError, ValueError) as exc:
            policy_error = str(exc)
    if from_turn is not None and session is None:
        raise ValueError("Choose a session before selecting its starting turn")
    with _snapshot(store):
        checks, state = _evidence(store, tree, expected, required or [])
        sessions = _related_sessions(store, tree.root, agent=agent if session is None else None)
        selected = _session(store, tree.root, session, agent) if session is not None else (
            sessions[0] if sessions else None)
        history = session_history(store, selected["session_id"], agent=selected["adapter_id"],
                                  from_turn=from_turn) if selected else {"turns": [], "total": 0, "limited": False}
    if policy_error and state != "failed":
        state = "unknown"
    return {"kind": "repository", **asdict(tree), "root": str(tree.root), "state": state,
            "label": LABEL[state], "checks": checks, "sessions": sessions, "selected": selected,
            "history": history, "session_selected": session is not None, "policy_error": policy_error}


def _process(argv: list[str], root: Path, output: bytearray, terminal: BinaryIO | None) -> int:
    with subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=os.name == "posix") as proc:
        assert proc.stdout is not None
        try:
            while chunk := os.read(proc.stdout.fileno(), 4096):
                output.extend(chunk)
                if len(output) > OUTPUT_LIMIT:
                    del output[:-OUTPUT_LIMIT]
                if terminal is not None:
                    terminal.write(chunk)
                    terminal.flush()
            return proc.wait()
        except BaseException:
            if proc.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
            raise


def record_check(store: TraceStore, root: Path, name: str, argv: list[str], *,
                 session: str | None = None, agent: str | None = None,
                 terminal: BinaryIO | None = None) -> dict[str, Any]:
    _check_name(name)
    configured = _configuration(root).get(name)
    argv = argv or configured or []
    if not _valid_argv(argv):
        raise ValueError(f"Supply a command after -- or define {name!r} in {CONFIG}")
    if configured and argv != configured:
        raise ValueError(f"Use the configured command for {name!r} or choose another check name")
    if _informational(argv[1:]):
        raise ValueError("Choose a command that actually runs the check")
    # Explicit checks use argv so the process exit observed here belongs to the
    # requested runner. Shell cookbooks can be a tracked script with its own
    # deliberate exit contract. Reject inline shell programs.
    exe = Path(argv[0]).name
    if exe in {"sh", "bash", "zsh", "fish", "ksh", "dash"} and any(
        arg.startswith("-") and "c" in arg for arg in argv[1:]
    ):
        raise ValueError("Use a script file or a direct command instead of an inline shell program")

    tree = worktree(root)
    if not tree.fingerprint:
        raise GitReviewError(tree.unavailable or "The worktree snapshot is unavailable")
    source = _session(store, root, session, agent) if session is not None else None
    trace_id = None
    if source:
        history = session_history(store, source["session_id"], agent=source["adapter_id"], limit=1)
        if history["turns"]:
            trace_id = history["turns"][0]["trace_id"]
    output = bytearray()
    record_argv = _public_argv(argv)
    conn = store._conn()
    with store.transaction():
        run_id = conn.execute(
            """INSERT INTO review_check_runs
               (root,name,argv,argv_digest,fingerprint,head,adapter_id,session_id,trace_id,started_at,state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (str(root), name, json.dumps(record_argv), _argv_digest(argv), tree.fingerprint,
             tree.head, source["adapter_id"] if source else None, session, trace_id, now_iso(), "running"),
        ).lastrowid
    code = None
    state = "unknown"
    detail = None
    try:
        code = _process(argv, root, output, terminal)
        after = worktree(root)
        if after.fingerprint != tree.fingerprint:
            detail = after.unavailable or "Worktree changed while the check was running"
        else:
            state = "passed" if code == 0 else "failed"
    except OSError as exc:
        detail = str(exc)
    except KeyboardInterrupt:
        detail = "Check was interrupted"
    except GitReviewError as exc:
        detail = str(exc)
    finally:
        text = output.decode(errors="replace") if _common.capture_content() else ""
        excerpt = _common.redact_string((text + ("\n" + detail if detail else "")).strip()) or None
        with store.transaction():
            conn.execute("UPDATE review_check_runs SET state=?,exit_code=?,output=?,finished_at=? WHERE id=?",
                         (state, code, excerpt, now_iso(), run_id))
    return {"id": run_id, "state": state, "exit_code": code, "detail": detail}


def repository_diff(root: Path, path: str, *, allow_clean: bool = False) -> dict[str, str]:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Choose a file inside the current worktree")
    status = worktree_status(root)
    item = next((item for item in status if item["path"] == path), None)
    if item is None:
        if allow_clean:
            return {}
        raise ValueError("This file no longer has changes in the worktree")
    return file_diff(root, path, untracked=item["index"] == item["worktree"] == "?")


def repository_markdown(review: dict[str, Any]) -> str:
    def quote(value: str) -> str:
        return "\n".join("    " + line for line in value.splitlines()) or "    —"

    lines = ["# Repository review", "", review["label"], "",
             quote(f"Repository {review['root']}\nHEAD {review['head'] or 'unborn'}\n"
                   f"Snapshot {review['fingerprint'] or 'unavailable'}"),
             "", "## Changed files", "",
             quote("\n".join(f"{f['index']}{f['worktree']} {f['path']}" for f in review["files"])),
             "", "## Local checks", ""]
    if not review["staged_matches_worktree"]:
        lines.append("The working tree contains changes outside the Git index. Checks cover the working copy.")
    if review.get("policy_error"):
        lines.extend(["Check configuration could not be evaluated.", quote(review["policy_error"])])
    for check in review["checks"]:
        source = [str(check.get("id") or "—"), check.get("adapter_id"), check.get("session_id"), check.get("trace_id")]
        lines.extend([quote(f"{check['name']}  {check['state']}  · run / session / turn "
                            + " / ".join(s for s in source if s)),
                      quote(f"Snapshot {check.get('fingerprint') or '—'} · exit "
                            f"{check.get('exit_code')} · {check.get('finished_at') or 'unavailable'}"),
                      quote(shlex.join(check.get("argv") or [])), quote(check.get("output") or ""), ""])
    history = review["history"]
    if history["turns"]:
        selected = review["selected"]
        lines.extend(["## Session evidence", "",
                      quote(f"{selected['adapter_id']} / {selected['session_id']}"),
                      "Recorded trace checks have no code snapshot binding.", ""])
        for turn in reversed(history["turns"]):
            lines.extend([quote(f"Turn {turn['trace_id']}\n{turn['task'] or 'Task unavailable'}"),
                          quote("Agent reported: " + (turn["conclusion"] or "No conclusion recorded")), ""])
            for check in turn["checks"]:
                lines.append(quote(f"{check['state']} · {check['command']} · span {check['span_id']}"))
        if history["limited"]:
            lines.extend(["", f"Showing {len(history['turns'])} of {history['total']} turns."])
    return "\n".join(lines).strip() + "\n"
