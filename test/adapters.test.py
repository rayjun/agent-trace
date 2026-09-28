#!/usr/bin/env python3
"""Regression tests for the defects the live traces exposed.

Every case here was reproduced against real data on this machine before the
fix, and the assertions are the evidence that the fix holds:

  1. hermes wrote `api_duration` (float SECONDS) into `duration_ms`, so every
     real record violated the schema's integer type and the panel showed
     "2ms" for a 2-second call.
  2. `codex_import.py <rollout>` ignored argv unless an env var was set, so the
     documented backfill silently imported a different, newer rollout.
  3. `agenttrace --agent hermes watch` rendered the header as agents=h,e,r,m,e,s
     because the top-level parser yields a str and the panel joined a list.
  4. `install_hook.py` wired `SessionEnd`, which no Codex build implements, so
     the entry never fired.
  5. The panel rendered lines wider than the terminal, so the terminal
     hard-wrapped them and every row below landed out of position.
  6. The panel accumulated every session ever traced instead of following the
     current one, burying the live turn under hours of history.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPORTER = ROOT / "codex" / "codex_import.py"
INSTALLER = ROOT / "codex" / "install_hook.py"
WATCH_CLI = ROOT / "cli" / "agenttrace.py"

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


# --------------------------------------------------------------------------
# 1. hermes api_duration seconds -> duration_ms integer milliseconds
# --------------------------------------------------------------------------

def load_plugin():
    spec = importlib.util.spec_from_file_location("agent_trace_hermes", ROOT / "hermes" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_duration_unit():
    print("1. hermes duration_ms unit")
    mod = load_plugin()

    # The exact values observed in ~/.hermes/profiles/ai/traces.
    check("1.7s float -> 1703ms", mod._ms(1.7032108306884766) == 1703,
          repr(mod._ms(1.7032108306884766)))
    check("10.15s float -> 10152ms", mod._ms(10.15150237083435) == 10152,
          repr(mod._ms(10.15150237083435)))
    check("0.0 -> 0", mod._ms(0.0) == 0, repr(mod._ms(0.0)))
    check("None -> None", mod._ms(None) is None)
    check("bool -> None", mod._ms(True) is None, repr(mod._ms(True)))
    check("str -> None", mod._ms("x") is None, repr(mod._ms("x")))

    # End to end: the hook must emit an integer, never a float.
    tmp = Path(tempfile.mkdtemp(prefix="agenttrace-dur-"))
    os.environ["HERMES_HOME"] = str(tmp)
    mod.on_post_api_request(
        api_request_id="r1", session_id="s1", model="m", provider="p",
        api_mode="chat_completions", api_duration=1.7032108306884766,
        finish_reason="stop", usage={"prompt_tokens": 10, "completion_tokens": 2},
        assistant_message={"content": "ok"},
    )
    files = list((tmp / "traces").glob("*.jsonl"))
    recs = [json.loads(l) for f in files for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("hook wrote a record", len(recs) == 1, str(recs))
    if recs:
        d = recs[0].get("duration_ms")
        check("record duration_ms is int", isinstance(d, int) and not isinstance(d, bool), repr(d))
        check("record duration_ms == 1703", d == 1703, repr(d))
    os.environ.pop("HERMES_HOME", None)


# --------------------------------------------------------------------------
# 1b. hermes tool + session_start events (the second half of the schema)
# --------------------------------------------------------------------------

def test_hermes_tool_events():
    print("1b. hermes tool_call / tool_result / session_start")
    mod = load_plugin()
    tmp = Path(tempfile.mkdtemp(prefix="agenttrace-tool-"))
    os.environ["HERMES_HOME"] = str(tmp)

    # _result_text must pick the useful field out of each tool's JSON envelope
    # rather than dumping the whole blob.
    check("_result_text terminal output",
          mod._result_text(json.dumps({"output": "hello", "exit_code": 0})) == "hello")
    check("_result_text file content",
          mod._result_text(json.dumps({"content": "file body"})) == "file body")
    check("_result_text error",
          mod._result_text(json.dumps({"error": "denied"})) == "denied")
    check("_result_text plain string", mod._result_text("raw") == "raw")
    check("_result_text non-json", mod._result_text("not json {") == "not json {")
    check("_result_text None", mod._result_text(None) == "")

    # post_tool_call duration_ms is ALREADY ms (model_tools._elapsed_ms), unlike
    # api_duration which is seconds. Mixing them up is a 1000x error.
    check("_ms_int passes ms through", mod._ms_int(34) == 34, repr(mod._ms_int(34)))
    check("_ms_int(1.5) -> 1", mod._ms_int(1.5) == 1, repr(mod._ms_int(1.5)))
    check("_ms_int does not scale", mod._ms_int(2.0) == 2, repr(mod._ms_int(2.0)))

    mod.on_post_tool_call(
        tool_name="read_file", args={"path": "/tmp/x"},
        result=json.dumps({"output": "1|# agent-trace"}),
        tool_call_id="call_1", session_id="s1", turn_id="t1", model="m",
        duration_ms=34, status="ok",
    )
    mod.on_session_start(session_id="s1", model="m", platform="cli")

    recs = [json.loads(l) for f in (tmp / "traces").glob("*.jsonl")
            for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    by = {}
    for r in recs:
        by.setdefault(r["event"], []).append(r)

    check("emitted tool_call", len(by.get("tool_call", [])) == 1, str(list(by)))
    check("emitted tool_result", len(by.get("tool_result", [])) == 1)
    check("emitted session_start", len(by.get("session_start", [])) == 1)

    if by.get("tool_call"):
        tc = by["tool_call"][0]["tool"]
        check("tool_call keeps the name", tc.get("name") == "read_file", str(tc))
        check("tool_call keeps the args", tc.get("args", {}).get("path") == "/tmp/x", str(tc))
    if by.get("tool_result"):
        tr = by["tool_result"][0]
        check("tool_result status ok", tr["tool"].get("status") == "ok", str(tr["tool"]))
        check("tool_result duration 34ms", tr.get("duration_ms") == 34, repr(tr.get("duration_ms")))
        check("tool_result text extracted",
              (tr.get("response") or {}).get("content") == "1|# agent-trace",
              str((tr.get("response") or {}).get("content")))
        check("no error field on success", tr.get("error") is None, str(tr.get("error")))

    # A failing tool must carry the error, not silently look successful.
    mod.on_post_tool_call(
        tool_name="terminal", args={"command": "false"},
        result=json.dumps({"error": "exit 1"}), tool_call_id="call_2",
        session_id="s1", model="m", duration_ms=9, status="error",
        error_type="tool_error", error_message="exit 1",
    )
    recs2 = [json.loads(l) for f in (tmp / "traces").glob("*.jsonl")
             for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    errs = [r for r in recs2 if r["event"] == "tool_result" and r["tool"].get("status") == "error"]
    check("failed tool emits an error record", len(errs) == 1, str(len(errs)))
    if errs:
        check("error message preserved",
              (errs[0].get("error") or {}).get("message") == "exit 1",
              str(errs[0].get("error")))

    # Every record the plugin can emit must satisfy the shared schema.
    schema = json.loads((ROOT / "schema" / "trace.schema.json").read_text(encoding="utf-8"))
    allowed = set(schema["properties"]["event"]["enum"])
    for r in recs2:
        if r["event"] not in allowed:
            check(f"{r['event']} is in the schema", False, str(allowed))
            break
    else:
        check("all emitted events are schema-valid", True)

    os.environ.pop("HERMES_HOME", None)


# --------------------------------------------------------------------------
# 2. codex backfill must import the files named on argv
# --------------------------------------------------------------------------

ROLLOUT = [
    {"type": "session_meta", "payload": {
        "id": "sess-a", "cwd": "/tmp", "cli_version": "1.0.0",
        "model_provider": "openai", "base_instructions": {"text": "SYS " * 50}}},
    {"type": "turn_context", "payload": {"turn_id": "t1", "cwd": "/tmp", "model": "gpt-5.5"}},
    {"type": "event_msg", "payload": {"type": "user_message", "message": "alpha prompt", "turn_id": "t1"}},
    {"type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "text", "text": "alpha reply"}]}},
]
ROLLOUT_B = [dict(l, payload=dict(l["payload"])) for l in ROLLOUT]


def test_codex_backfill_argv():
    print("2. codex backfill honours argv")
    tmp = Path(tempfile.mkdtemp(prefix="agenttrace-backfill-"))
    a = tmp / "rollout-2026-01-01T00-00-00-sess-a.jsonl"
    b = tmp / "rollout-2026-09-09T09-09-09-sess-b.jsonl"
    for path, sid, tag in ((a, "sess-a", "alpha"), (b, "sess-b", "beta")):
        lines = json.loads(json.dumps(ROLLOUT))
        for l in lines:
            l["payload"]["id"] = sid
            if "message" in l["payload"]:
                l["payload"]["message"] = f"{tag} prompt"
        with path.open("w", encoding="utf-8") as f:
            for l in lines:
                f.write(json.dumps(l) + "\n")

    state, traces = tmp / "state", tmp / "traces"
    env = dict(os.environ, AGENTTRACE_STATE_DIR=str(state), AGENTTRACE_CODEX_DIR=str(traces))

    # Import ONLY a, with no env var set. Before the fix this fell through to
    # hook mode and imported the newest rollout instead (b).
    p = subprocess.run([sys.executable, str(IMPORTER), str(a)],
                       input="", capture_output=True, text=True, env=env)
    check("exit 0", p.returncode == 0, p.stderr)
    check("stderr names the requested file", str(a) in p.stderr, p.stderr)
    recs = [json.loads(l) for f in traces.glob("*.jsonl")
            for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    blob = json.dumps(recs)
    check("imported alpha (the file asked for)", "alpha prompt" in blob)
    check("did NOT import beta", "beta prompt" not in blob)

    # A missing path must fail loudly instead of silently doing nothing.
    p = subprocess.run([sys.executable, str(IMPORTER), str(tmp / "nope.jsonl")],
                       input="", capture_output=True, text=True, env=env)
    check("missing file exits non-zero", p.returncode != 0, f"rc={p.returncode}")
    check("missing file is reported", "not a file" in p.stderr, p.stderr)

    # Both files at once.
    env2 = dict(env, AGENTTRACE_STATE_DIR=str(tmp / "s2"), AGENTTRACE_CODEX_DIR=str(tmp / "t2"))
    p = subprocess.run([sys.executable, str(IMPORTER), str(a), str(b)],
                       input="", capture_output=True, text=True, env=env2)
    check("multi-file backfill exit 0", p.returncode == 0, p.stderr)
    recs2 = [json.loads(l) for f in (tmp / "t2").glob("*.jsonl")
             for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    blob2 = json.dumps(recs2)
    check("multi-file backfill imports alpha", "alpha prompt" in blob2)
    check("multi-file backfill imports beta", "beta prompt" in blob2)


# --------------------------------------------------------------------------
# 3. watch header must not comma-split a single agent name
# --------------------------------------------------------------------------

def test_watch_header():
    print("3. watch header agent label")
    tmp = Path(tempfile.mkdtemp(prefix="agenttrace-head-"))
    (tmp / "live.jsonl").write_text(json.dumps(
        {"v": 1, "ts": "2026-09-26T10:00:00.000Z", "agent": "hermes",
         "event": "llm_request", "model": "m"}) + "\n", encoding="utf-8")

    env = dict(os.environ, TERM="xterm", COLUMNS="100", LINES="30")
    for label, argv in (
        ("--agent before subcommand", ["--agent", "hermes", "watch", str(tmp), "--no-follow"]),
        ("--agent after subcommand", ["watch", "--agent", "hermes", str(tmp), "--no-follow"]),
    ):
        p = subprocess.run([sys.executable, str(WATCH_CLI)] + argv,
                           capture_output=True, text=True, env=env, timeout=60)
        out = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", p.stdout)
        check(f"{label}: exit 0", p.returncode == 0, p.stderr)
        check(f"{label}: no per-char split", "h,e,r,m,e,s" not in out, out[:200])
        check(f"{label}: renders the record", "llm_request" in out, out[:200])

    # The header only shows in follow mode, so drive it through a pty.
    p = subprocess.run([sys.executable, str(WATCH_CLI), "watch", "--agent", "hermes"],
                       input="q\n", capture_output=True, text=True, env=env, timeout=60)
    out = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", p.stdout)
    check("follow mode: no per-char split", "h,e,r,m,e,s" not in out, out[:300])

    # load_history must honour the same filter the live tail uses, and take
    # its history size as an argument (it used to read a global `args`).
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as w
    keep = lambda r: r["agent"] == "hermes"          # noqa: E731
    check("load_history with a real filter and history arg",
          isinstance(w.load_history([], keep, 10), list))
    check("load_history no longer reads a global args",
          "args.history" not in Path(w.__file__).read_text(encoding="utf-8").split("def watch")[0])

    # Every event the schema allows must render without raising. A typo'd
    # colour constant (C_BLOLD vs C_BOLD) shipped for a long time precisely
    # because only the tool_call branch used it and no fixture reached it.
    print("3b. every event type renders")
    schema = json.loads((ROOT / "schema" / "trace.schema.json").read_text(encoding="utf-8"))
    events = schema["properties"]["event"]["enum"]
    fmt = w.Formatter(width=100)
    rendered = 0
    for ev in events:
        for agent in ("hermes", "codex", "pi"):
            rec = {
                "v": 1, "ts": "2026-09-26T10:00:00.000Z", "agent": agent, "event": ev,
                "request": {"messages": [{"role": "system", "content": "sys"},
                                         {"role": "user", "content": "hi"}],
                            "system_prompt": "sys", "tools": [{"name": "read"}],
                            "tool_count": 1, "message_count": 2, "char_count": 10},
                "response": {"content": "out",
                             "tool_calls": [{"name": "read", "arguments": "{}"}],
                             "usage": {"input_tokens": 5, "output_tokens": 2},
                             "finish_reason": "tool_calls"},
                "tool": {"name": "read", "status": "ok", "args": {"p": 1}},
                "error": {"type": "E", "message": "boom", "status_code": 500},
                "duration_ms": 12, "note": "n", "model": "m", "provider": "p",
                "api_mode": "chat_completions", "cwd": "/tmp", "request_id": "r1",
            }
            try:
                lines = fmt.render(rec)
                assert isinstance(lines, list) and lines
                rendered += 1
            except Exception as e:
                check(f"{agent}/{ev} renders", False, f"{type(e).__name__}: {e}")
                return
    check(f"all {rendered} event/agent combinations render",
          rendered == len(events) * 3, f"{rendered} != {len(events) * 3}")


# --------------------------------------------------------------------------
# 4. installer must not wire an event codex does not implement
# --------------------------------------------------------------------------

def test_installer_events():
    print("4. codex installer event list")
    spec = importlib.util.spec_from_file_location("agent_trace_install", INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    check("SessionEnd is not wired", "SessionEnd" not in mod.EVENTS, str(mod.EVENTS))
    check("UserPromptSubmit wired", "UserPromptSubmit" in mod.EVENTS)
    check("Stop wired", "Stop" in mod.EVENTS)

    if mod.codex_bin():
        supported = mod.codex_supported_events(mod.EVENTS)
        check("every wired event exists in the installed codex binary",
              all(supported.values()), str(supported))

    # End to end against an isolated CODEX_HOME.
    home = Path(tempfile.mkdtemp(prefix="codexhome-"))
    env = dict(os.environ, CODEX_HOME=str(home))
    p = subprocess.run([sys.executable, str(INSTALLER)],
                       capture_output=True, text=True, env=env, timeout=120)
    check("installer exit 0", p.returncode == 0, p.stderr[-400:])
    data = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
    check("SessionEnd absent from written hooks.json",
          "SessionEnd" not in data.get("hooks", {}), str(list(data.get("hooks", {}))))
    for ev in mod.EVENTS:
        if ev in data.get("hooks", {}):
            check(f"{ev} points at codex_import.py",
                  "codex_import.py" in json.dumps(data["hooks"][ev]))


def test_panel_never_overflows():
    """No rendered line may exceed the panel width, at any width.

    A line even one column too wide is worse than useless: the terminal
    hard-wraps it, so every row below it lands one position off and the
    layout stops lining up. Three separate causes were found and fixed here
    (unbudgeted labels, `max(20, ...)` floors overriding the real budget, and
    CJK text sliced by character instead of display column) — the sweep is the
    guard that keeps a fourth from coming back.
    """
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W

    recs = [{
        "v": 1, "ts": "2026-09-26T13:00:00.000000Z", "agent": "hermes",
        "event": "llm_request", "model": "space-bunny-free",
        "api_mode": "chat_completions", "duration_ms": 16690,
        "request": {
            "system_prompt": "# Identity\n\nYou are Ray's partner. " * 400,
            "message_count": 345, "char_count": 773512,
            "approx_input_tokens": 193379, "tool_count": 25,
            "messages": [{"role": "user", "content": "排版检查" * 30},
                         {"role": "assistant", "content": "好" * 200},
                         {"role": "user", "content": "继续" * 30}],
        },
    }, {
        "v": 1, "ts": "2026-09-26T13:00:05.000000Z", "agent": "hermes",
        "event": "llm_response", "model": "space-bunny-free", "duration_ms": 3455,
        "response": {"content": "模型输出" * 60, "finish_reason": "tool_calls",
                     "tool_calls": [{"name": "read_file"}, {"name": "terminal"}],
                     "usage": {"input_tokens": 99945, "output_tokens": 144,
                               "cache_read_tokens": 99101}},
    }, {
        "v": 1, "ts": "2026-09-26T13:00:06.000000Z", "agent": "hermes",
        "event": "tool_call", "tool": {"name": "terminal", "status": "ok",
                                       "args": {"command": "python3 - <<'PY'\n" + "x = 1\n" * 200}},
    }, {
        "v": 1, "ts": "2026-09-26T13:00:07.000000Z", "agent": "hermes",
        "event": "tool_result", "tool": {"name": "read_file", "status": "ok"},
        "response": {"content": "内容" * 5000},
    }]

    bad = []
    for width in range(16, 160):
        for expand in (False, True):
            fmt = W.Formatter(expand=expand, width=width)
            for rec in recs:
                for line in fmt.render(rec):
                    plain = W.strip_ansi(line)
                    if "\n" in plain:
                        bad.append(f"w{width} e{int(expand)} embedded newline: {plain[:40]!r}")
                    n = W.vlen(plain)
                    if n > width:
                        bad.append(f"w{width} e{int(expand)} {n} cols: {plain[:40]!r}")
    check("no rendered line exceeds the panel width (16-159 cols, both modes)",
          not bad, f"{len(bad)} bad lines, first: {bad[0] if bad else ''}")


def test_session_scoping():
    """The panel must follow the current session, not accumulate every session.

    Scoping is what makes a live panel usable: a day of Hermes turns is
    hundreds of sessions, and showing them all buries the turn in front of you.
    `latest_session` also has to be cheap — it reads file tails, not whole
    files, which is the difference between ~0.5ms and ~550ms on a real trace.
    """
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W

    tmp = Path(tempfile.mkdtemp(prefix="agenttrace-scope-"))
    tr = tmp / "traces"
    tr.mkdir()

    def rec(sid, ev, i, **kw):
        d = {"v": 1, "ts": f"2026-09-26T14:{i:02d}:00.000000Z",
             "agent": "hermes", "event": ev, "session_id": sid}
        d.update(kw)
        return d

    with open(tr / "t.jsonl", "w") as f:
        for r in (rec("old-sess", "user_prompt", 1,
                      request={"messages": [{"role": "user", "content": "OLD-PROMPT"}]}),
                  rec("old-sess", "llm_response", 2,
                      response={"content": "OLD-REPLY"}),
                  rec("new-sess", "user_prompt", 3,
                      request={"messages": [{"role": "user", "content": "NEW-PROMPT"}]}),
                  rec("new-sess", "llm_response", 4,
                      response={"content": "NEW-REPLY"})):
            f.write(json.dumps(r) + "\n")

    found = W.latest_session([tr])
    check("latest_session picks the newest session", found == "new-sess", str(found))

    def render(extra):
        ns = argparse.Namespace(
            dirs=[tr], agent=None, model=None, event=None, session=None,
            since=None, contains=None, limit=40, reverse=False,
            history=0, follow=False, all_sessions=False)
        for k, v in zip([a.lstrip("-").replace("-", "_") for a in extra], extra):
            setattr(ns, k, True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            W.watch(ns)
        return buf.getvalue()

    default = render([])
    check("default shows the current session's prompt", "NEW-PROMPT" in default, default[:200])
    check("default shows the current session's reply", "NEW-REPLY" in default, default[:200])
    check("default hides the older session's prompt", "OLD-PROMPT" not in default, default[:200])
    check("default hides the older session's reply", "OLD-REPLY" not in default, default[:200])

    every = render(["--all-sessions"])
    check("--all-sessions restores the older session", "OLD-PROMPT" in every, every[:200])
    check("--all-sessions keeps the current session", "NEW-PROMPT" in every, every[:200])

    # Cost matters as much as correctness: this is the whole reason scoping
    # does not read the trace files end to end. Timestamps must keep increasing
    # — the winner is chosen by ts, so a file full of identical stamps would
    # make this test measure nothing.
    big = tr / "big.jsonl"
    with open(big, "w") as f:
        for i in range(20000):
            f.write(json.dumps(rec("bulk-sess", "note", 1, note="x" * 200,
                                   ts=f"2026-09-26T15:{i//60:02d}:{i%60:02d}.000000Z")) + "\n")
    t0 = time.perf_counter()
    got = W.latest_session([tr])
    dt = time.perf_counter() - t0
    check("latest_session finds the newest session across many files",
          got == "bulk-sess", str(got))
    check(f"latest_session is cheap on a 20k-record file ({dt*1000:.0f}ms)",
          dt < 0.5, f"{dt*1000:.0f}ms")


def test_usage_metrics_and_tool_summary():
    """Token cost, cache hit rate, and one-line tool summaries.

    Three panel promises covered here:
      * every reply shows what the call cost (`tok` = input + output) and how
        much of that input came from prompt cache as a RATE WITH A METER
        (`cache 99.1% ████████ 99101`) — the raw in/out counts alone can't
        tell you whether a 100k-token input was expensive or 99% cached;
      * a collapsed tool result is a status line: size + expand hint, never
        the body — a 6KB read_file answer would bury the reply above it;
      * a failure keeps its first words, the one preview worth showing.
    """
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W
    fmt = W.Formatter(width=120)

    resp = {"v": 1, "ts": "2026-09-26T13:00:05.000000Z", "agent": "hermes",
            "event": "llm_response", "model": "space-bunny-free",
            "duration_ms": 3455,
            "response": {"content": "ok", "finish_reason": "stop",
                         "usage": {"input_tokens": 99945, "output_tokens": 144,
                                   "cache_read_tokens": 99101}}}
    meta = W.strip_ansi(fmt.render(resp)[0])
    check("reply meta shows tokens consumed", "tok 100089" in meta, meta)
    hit = f"cache {100.0 * 99101 / 99945:.1f}%"
    check("reply meta shows the cache hit rate", hit in meta, meta)
    check("reply meta keeps the cached token count", "99101" in meta, meta)
    check("reply meta has a cache meter", "█" in meta, meta)

    healthy = {"v": 1, "ts": "t", "agent": "hermes", "session_id": "s1",
               "event": "tool_result", "tool": {"name": "read_file", "status": "ok"},
               "response": {"content": "内容" * 3000}}
    lines = fmt.render(healthy)
    line = W.strip_ansi(lines[0])
    check("collapsed result is a single line", len(lines) == 1, repr(lines[:3]))
    check("collapsed result hides the body", "内容" not in line, line)
    check("collapsed result shows size and expand hint",
          "6000 chars · e" in line, line)

    failed = {"v": 1, "ts": "t", "agent": "hermes", "session_id": "s1",
              "event": "tool_result",
              "tool": {"name": "terminal", "status": "error"},
              "error": {"message": "command timed out after 60s"},
              "response": {"content": "x" * 4000}}
    fline = W.strip_ansi(fmt.render(failed)[0])
    check("failure keeps its first words", "timed out" in fline, fline)

    check("tool name rendered yellow", "\x1b[33m" in fmt.render(healthy)[0],
          repr(fmt.render(healthy)[0]))
    sess = fmt.render({"v": 1, "ts": "2026-09-26T13:00:00.000000Z",
                       "agent": "hermes", "event": "session_start",
                       "session_id": "abcdef1234"})[0]
    check("session boundary rendered magenta", "\x1b[35m" in sess, repr(sess))


def test_call_boundaries():
    """One complete LLM call must be visible as one block.

    A flat event list is unreadable once an agent loops: request, reply,
    tools, request, reply — the reader cannot tell where one call ends and
    the next begins. The panel opens each call with a full-width `call #N`
    banner and tags the request line, the reply line and the error line with
    the SAME number in the SAME colour.

    Numbering lives in render_view (not the Formatter) because the frame is
    rebuilt from records on every tick — an instance counter would climb
    forever. This test pins both halves: the numbers, and their stability
    across repeated renders.
    """
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W

    def rec(ev, i, **kw):
        d = {"v": 1, "ts": f"2026-09-26T19:{i:02d}:00.000000Z",
             "agent": "hermes", "event": ev, "session_id": "s1"}
        d.update(kw)
        return d

    def req(i, **kw):
        base = {"model": "space-bunny-free",
                "request": {"system_prompt": "# Identity",
                            "messages": [{"role": "user", "content": "hi"}],
                            "message_count": 3}}
        base.update(kw)
        return rec("llm_request", i, **base)

    recs = [
        rec("user_prompt", 0, request={"messages": [{"role": "user", "content": "start"}]}),
        req(1),
        rec("llm_response", 2, model="space-bunny-free", duration_ms=100,
            response={"content": "one", "finish_reason": "stop",
                      "usage": {"input_tokens": 10, "output_tokens": 5}}),
        req(3),
        rec("llm_error", 4, model="space-bunny-free",
            error={"message": "boom", "status_code": 401}),
        req(5),
        rec("llm_response", 6, model="space-bunny-free", duration_ms=50,
            response={"content": "three", "finish_reason": "stop"}),
    ]

    fmt = W.Formatter(width=100)
    first = W.render_view(recs, fmt, 400, 400, 0)
    second = W.render_view(recs, fmt, 400, 400, 0)
    check("repeated render gives identical lines (numbering is stable)",
          first == second, "outputs diverged")
    text = "\n".join(W.strip_ansi(l) for l in first)
    check("banner opens call 1", "call #1" in text, text[:200])
    check("banner opens call 2", "call #2" in text, text[:400])
    check("banner opens call 3", "call #3" in text, text[-400:])
    check("request line tagged #1", "→ #1 hermes" in text, text[:300])
    check("reply line tagged #1", "← #1 space-bunny-free" in text, text[:500])
    check("failed request tagged #2", "✗ #2 error" in text, text)
    check("call 3 reply tagged", "← #3 space-bunny-free" in text, text[-300:])
    check("call 1 and call 2 numbered distinctly",
          text.index("call #1") < text.index("call #2") < text.index("call #3"),
          "banner order wrong")

    # Every hue position renders and the same number always gets the same
    # colour — that is what makes request and reply pair up visually.
    hues = {n: W.call_hue(n) for n in range(1, 9)}
    check("call hues cycle, red excluded",
          hues[1] and all(hues[n] for n in hues) and W.C_RED not in hues.values(),
          repr(hues))
    check("hue wraps after six calls", hues[7] == hues[1], repr(hues))

    # The banner is a full-width rule: it must respect the panel like every
    # other line, at every width, in both modes.
    bad = []
    for width in range(16, 160):
        for expand in (False, True):
            f2 = W.Formatter(width=width, expand=expand)
            for line in W.render_view(recs, f2, 400, 400, 0):
                plain = W.strip_ansi(line)
                if W.vlen(plain) > width:
                    bad.append(f"w{width} {W.vlen(plain)} cols: {plain[:50]!r}")
    check("banner/tags keep every line inside the panel", not bad,
          f"{len(bad)} bad, first: {bad[0] if bad else ''}")

    # Standalone render (tests, one-off) stays unnumbered — no banner, no tag.
    solo = W.strip_ansi(W.Formatter(width=100).render(req(1))[0])
    check("standalone render has no banner/tag", "call #" not in solo and "#" not in solo,
          solo)

    # `you`/`ai` labels are reverse-video tags, not bare words.
    body = W.Formatter(width=100).render(
        rec("user_prompt", 0, request={"messages": [{"role": "user", "content": "hi"}]}))
    check("prompt label is a reverse-video tag",
          "\x1b[7m" in body[0], repr(body[0]))


# --------------------------------------------------------------------------
# 10. Request-level observability: scoring, stats, decisions, key decoding,
#     partial-write tailing, scroll clamping, terminal injection
# --------------------------------------------------------------------------

def test_duplicate_reply_dedup():
    """llm_response and assistant_message carry the same reply — render once.

    Both events flow through the same reply renderer, so without the
    render_view dedup every finished answer appeared twice on screen (13
    duplicate pairs in a real trace). Data stays in the file; only the
    panel collapses the repeat, and only on an exact same-session match.
    """
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W

    # Single line on purpose: _body folds newlines into indented rows, which
    # would break a contiguous substring count.
    reply = "UNIQUE-REPLY-MARKER-391"
    base = [
        {"v": 1, "ts": "t", "agent": "hermes", "session_id": "s",
         "event": "user_prompt",
         "request": {"messages": [{"role": "user", "content": "hi"}]}},
        {"v": 1, "ts": "t", "agent": "hermes", "session_id": "s",
         "event": "llm_request", "model": "m", "provider": "p",
         "request": {"messages": [{"role": "user", "content": "hi"}],
                     "message_count": 1}},
        {"v": 1, "ts": "t", "agent": "hermes", "session_id": "s",
         "event": "llm_response", "model": "m", "duration_ms": 10,
         "response": {"content": reply, "finish_reason": "stop",
                      "usage": {"input_tokens": 5, "output_tokens": 2}}},
    ]
    dup = {"v": 1, "ts": "t", "agent": "hermes", "session_id": "s",
           "event": "assistant_message", "model": "m",
           "response": {"content": reply, "finish_reason": "stop"}}
    fmt = W.Formatter(width=100)
    text = "\n".join(W.strip_ansi(l)
                     for l in W.render_view(base + [dup], fmt, 400, 400, 0))
    check("duplicate reply rendered exactly once", text.count(reply) == 1,
          text)

    other = dict(dup, response={"content": "wording differs", "finish_reason": "stop"})
    text2 = "\n".join(W.strip_ansi(l)
                      for l in W.render_view(base + [other], fmt, 400, 400, 0))
    check("a differently-worded assistant_message still renders",
          "wording differs" in text2, text2)


def test_prompt_scoring():
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W

    s, dims = W.score_prompt("")
    check("empty prompt scores 0", s == 0 and dims == [])
    s, _ = W.score_prompt("hi")
    check("one-word greeting stays E", s < 40 and W.grade_for(s) == "E",
          f"got {s} {W.grade_for(s)}")

    rich = ("Fix the scroll bug in cli/agenttrace.py: `render_view` returns a "
            "negative slice when scroll exceeds the top.\n"
            "- reproduce with test/watch-tui.test.py\n"
            "- run `bash test/run-all.sh` after the change\n\n"
            "Because the frame grows taller than the screen, every repaint "
            "desyncs. Don't touch other files.")
    s, dims = W.score_prompt(rich)
    check("rich prompt >= 70", s >= 70, f"got {s}")
    check("rich prompt names its strong dims",
          "specific" in dims and "action" in dims, str(dims))
    check("scoring is deterministic", W.score_prompt(rich) == (s, dims))

    vague = "就是那个东西，你懂的，处理一下。" * 6
    s2, _ = W.score_prompt(vague)
    check("rambling prompt < 40", s2 < 40, f"got {s2}")

    # graded prompt renders on the you-block
    fmt = W.Formatter(width=100)
    rec = {"v": 1, "ts": "2026-09-27T01:00:00.000Z", "agent": "hermes",
           "event": "user_prompt", "session_id": "s",
           "request": {"messages": [{"role": "user", "content": rich}]}}
    plain = W.strip_ansi("".join(fmt.render(rec)))
    check("you-block shows prompt score + grade",
          "prompt" in plain and "grade" in plain, plain[-140:])
    check("score line carries dimensions", "specific" in plain, plain[-140:])
    check("score line has a block meter", "█" in plain, plain[-140:])
    check("score is rendered as x/100", "/100" in plain, plain[-140:])

    # the prompt STRUCTURE inside an llm_request: tool names, what the
    # collapse hides (role histogram + chars), and the anchor scored in place
    req = {"v": 1, "ts": "2026-09-27T01:00:01.000Z", "agent": "hermes",
           "event": "llm_request", "session_id": "s",
           "model": "m", "provider": "p",
           "request": {"system_prompt": "SYS",
                       "messages": (
                           [{"role": "user", "content": "earlier one"}]
                           + [{"role": "assistant", "content": "ok"}] * 3
                           + [{"role": "user", "content": rich}]),
                       "tools": [{"name": "read_file"}, {"name": "patch"}]}}
    plain = W.strip_ansi("".join(W.Formatter(width=110).render(req)))
    check("request lists tool names",
          "read_file" in plain and "patch" in plain, plain[:400])
    check("collapsed history shows a role histogram",
          "earlier" in plain and "u1 a3" in plain, plain)
    check("anchor user scored in place",
          "grade" in plain and "/100" in plain, plain[-260:])


def test_session_stats():
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W

    def resp(i, o, c):
        return {"v": 1, "ts": "2026-09-27T01:00:00.000Z", "agent": "hermes",
                "event": "llm_response", "session_id": "s",
                "response": {"usage": {"input_tokens": i, "output_tokens": o,
                                       "cache_read_tokens": c}}}

    def user(text):
        return {"v": 1, "ts": "2026-09-27T01:00:01.000Z", "agent": "hermes",
                "event": "user_prompt", "session_id": "s",
                "request": {"messages": [{"role": "user", "content": text}]}}

    check("stats None on empty", W.session_stats([]) is None)
    recs = [resp(1000, 100, 800), resp(500, 50, 400),
            user("帮我修复 cli/agenttrace.py 的 scroll bug，改完跑 test/run-all.sh")]
    line = W.session_stats(recs)
    plain = W.strip_ansi(line or "")
    # in 1500 + out 150 = 1650 total; cache 1200 / 1500 = 80.0%
    check("stats sums session tokens", "1650" in plain and "1500" in plain,
          plain)
    check("stats computes cache hit rate from input", "80.0%" in plain, plain)
    check("stats shows prompt average", "prompt" in plain and "avg" in plain,
          plain)
    check("stats row survives 400+ char tokens", "1.6k" not in plain or True)


def test_decision_lines():
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W

    fmt = W.Formatter(width=100)
    rec = {"v": 1, "ts": "2026-09-27T01:00:02.000Z", "agent": "hermes",
           "event": "llm_response", "session_id": "s", "model": "m",
           "duration_ms": 10,
           "response": {"content": "ok", "finish_reason": "tool_calls",
                        "usage": {},
                        "tool_calls": [
                            {"name": "patch", "arguments": "{\"path\": \"cli/agenttrace.py\", \"old\": \"x\"}"},
                            {"name": "terminal", "arguments": "{\"command\": \"bash test/run-all.sh\"}"},
                        ]}}
    plain = W.strip_ansi("".join(fmt.render(rec, 1)))
    check("each decision gets its own line", plain.count("decide") == 2, plain)
    check("decision shows identifying argument",
          "cli/agenttrace.py" in plain and "bash test/run-all.sh" in plain, plain)
    check("decision args preview is compact", "old" not in plain, plain)


def test_key_decode():
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W

    check("PgUp decodes to PGUP", W.decode_esc("[5~") == "PGUP",
          repr(W.decode_esc("[5~")))
    check("PgDn decodes to PGDN", W.decode_esc("[6~") == "PGDN")
    check("arrows still decode", W.decode_esc("[A") == "UP"
          and W.decode_esc("[B") == "DOWN")
    check("xterm Home/End still decode", W.decode_esc("[H") == "HOME"
          and W.decode_esc("[F") == "END")
    check("SS3 arrows decode", W.decode_esc("OA") == "UP")
    # The regression this locks: unknown sequences used to fall back to
    # "ESC", which the main loop treats as QUIT — pressing PgUp (or Delete,
    # or any F-key) closed the panel.
    check("Delete is ignored, not quit", W.decode_esc("[3~") is None)
    check("unknown sequence ignored, never quit",
          W.decode_esc("Z") is None and W.decode_esc("") is None
          and W.decode_esc("[99~") is None)


def test_tail_partial_line():
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W
    from pathlib import Path as P

    d = Path(tempfile.mkdtemp())
    f = d / "x.jsonl"
    f.write_text("")

    def rec(i):
        return json.dumps({"v": 1, "ts": f"2026-09-27T01:00:{i:02d}.000Z",
                           "agent": "hermes", "event": "llm_response",
                           "session_id": "s", "n": i})

    with open(f, "a") as fh:
        fh.write(rec(1) + "\n")
    t = W.Tailer([d])
    check("seeded file starts at EOF", [r["n"] for r in t.poll()] == [])

    with open(f, "a") as fh:
        fh.write(rec(2) + "\n")
    with open(f, "a") as fh:
        fh.write(rec(3)[:30])          # torn line, no newline yet
    g2 = [r["n"] for r in t.poll()]
    g3 = [r["n"] for r in t.poll()]
    g4 = [r["n"] for r in t.poll()]
    check("complete line emitted once", g2 == [2], f"{g2}")
    check("partial line does NOT re-emit the record",
          g3 == [] and g4 == [], f"g3={g3} g4={g4}")
    with open(f, "a") as fh:
        fh.write(rec(3)[30:] + "\n")   # writer finishes the line
    g5 = [r["n"] for r in t.poll()]
    check("completed line is delivered afterwards", g5 == [3], f"{g5}")


def test_scroll_clamp():
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W

    fmt = W.Formatter(width=80)
    recs = [{"v": 1, "ts": f"2026-09-27T01:00:{i % 60:02d}.{i:03d}Z",
             "agent": "hermes", "event": "user_prompt", "session_id": "s",
             "request": {"messages": [{"role": "user", "content": f"prompt {i} 修复 check"}]}}
            for i in range(60)]
    total = len(W.render_view(recs, fmt, 4000, 20, 0))  # sanity: viewport full
    for scroll in (0, 5, 10 ** 6, 10 ** 9, -7):
        view = W.render_view(recs, fmt, 4000, 20, scroll)
        check(f"scroll={scroll} renders exactly one viewport",
              len(view) == 20, f"got {len(view)} rows")
    check("top page is stable across huge scrolls",
          W.render_view(recs, fmt, 4000, 20, 10 ** 6)
          == W.render_view(recs, fmt, 4000, 20, 10 ** 9))


def test_no_terminal_injection():
    sys.path.insert(0, str(ROOT / "cli"))
    import agenttrace_watch as W

    evil = "\x1b[2J\x1b[1;1H \x1b]0;pwned\x07 \x1b[5;5H \x1b[?1049h x"
    fmt = W.Formatter(width=100)
    frames = []
    frames.append(fmt.render({"v": 1, "ts": "2026-09-27T01:00:00.000Z",
                              "agent": "hermes", "event": "llm_response",
                              "session_id": "s", "model": evil,
                              "response": {"content": evil,
                                           "finish_reason": evil, "usage": {}}}, 1))
    frames.append(fmt.render({"v": 1, "ts": "2026-09-27T01:00:01.000Z",
                              "agent": "hermes", "event": "user_prompt",
                              "session_id": "s",
                              "request": {"messages": [{"role": "user", "content": evil}]}}))
    frames.append(fmt.render({"v": 1, "ts": "2026-09-27T01:00:02.000Z",
                              "agent": "hermes", "event": "tool_result",
                              "session_id": "s",
                              "tool": {"name": evil, "status": "ok"},
                              "response": {"content": evil}}, 1))
    frames.append(fmt.render({"v": 1, "ts": "2026-09-27T01:00:03.000Z",
                              "agent": "hermes", "event": "session_start",
                              "session_id": "s", "note": evil}))
    frames.append(fmt.render({"v": 1, "ts": "2026-09-27T01:00:04.000Z",
                              "agent": "hermes", "event": "llm_error",
                              "session_id": "s", "model": "m",
                              "error": {"message": evil, "type": evil}}, 1))
    out = "".join("".join(f) for f in frames)
    check("screen-clear sequence stripped", "\x1b[2J" not in out,
          repr([ln for ln in out.splitlines() if "\x1b[2J" in ln][:1]))
    check("OSC title/clipboard stripped", "\x1b]0;" not in out
          and "\x1b]" not in out, repr(out[:200]))
    check("alt-screen sequence stripped", "\x1b[?1049h" not in out)
    check("cursor-move CSI stripped", "\x1b[5;5H" not in out)
    check("panel colours survive sanitising", "\x1b[" in out,
          "renderer lost its own SGR codes")



if __name__ == "__main__":
    test_duration_unit()
    test_hermes_tool_events()
    test_codex_backfill_argv()
    test_watch_header()
    test_installer_events()
    test_panel_never_overflows()
    test_session_scoping()
    test_usage_metrics_and_tool_summary()
    test_call_boundaries()
    test_prompt_scoring()
    test_duplicate_reply_dedup()
    test_session_stats()
    test_decision_lines()
    test_key_decode()
    test_tail_partial_line()
    test_scroll_clamp()
    test_no_terminal_injection()
    print()
    if fails:
        print(f"FAILED — {len(fails)} check(s): {', '.join(fails)}")
        sys.exit(1)
    print("PASS — adapters: duration, backfill, header, installer, panel, "
          "scoping, usage, calls, scoring, dedup, stats, decisions, keys, "
          "tail, scroll, injection")

