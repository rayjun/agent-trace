#!/usr/bin/env python3
"""agenttrace — query the trace jsonl files written by the hermes/codex/pi adapters.

Reads the shared record format (schema/trace.schema.json) from one or more
trace directories, filters, and prints. No dependencies beyond stdlib.

    agenttrace ls                          # recent records, one line each
    agenttrace ls --agent codex --limit 20
    agenttrace show <request_id>           # full request+response for one LLM call
    agenttrace search "some text"          # grep across request+response content
    agenttrace sessions                    # group by session
    agenttrace stats                       # calls, tokens, errors per agent/model
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# The CLI is installed as a symlink into ~/.local/bin, so resolve() lands back
# in the repo and the shared helpers are reachable from there.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
import agenttrace_common as common  # noqa: E402

# Repo root, for core.* (discovery, storage) — the platform layers the `serve`
# and `index` subcommands drive. Imported lazily by those commands; only the
# discovery helper is needed at module level.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Sibling modules (trace_index, panel/) live beside this file — the CLI is
# invoked through a symlink, so resolve() first lands us back in the repo.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import trace_index  # noqa: E402

SCHEMA_V = common.SCHEMA_V


# --------------------------------------------------------------------------
# trace discovery
# --------------------------------------------------------------------------

# Discovery rules (profile-aware hermes homes, the two pi locations, ...) now
# live in core/storage/discovery.py — the indexer is their primary consumer and
# there must be exactly one copy. Re-exported here so existing imports keep
# working.
from core.storage.discovery import default_trace_dirs  # noqa: E402,F401


def iter_records(dirs: list[Path]):
    """Yield (path, lineno, record) for every well-formed record line."""
    for d in dirs:
        for path in sorted(d.rglob("*.jsonl")):
            try:
                fh = path.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for n, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict) and rec.get("v") == SCHEMA_V and "event" in rec:
                        yield path, n, rec


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------

def matches(rec: dict, args) -> bool:
    if args.agent and rec.get("agent") != args.agent:
        return False
    if args.model and args.model not in (rec.get("model") or ""):
        return False
    if args.event and rec.get("event") != args.event:
        return False
    if args.session and rec.get("session_id") != args.session:
        return False
    if args.since:
        ts = rec.get("ts") or ""
        if ts < args.since:
            return False
    if args.contains:
        needle = args.contains.lower()
        blob = json.dumps(rec, ensure_ascii=False).lower()
        if needle not in blob:
            return False
    return True


def _tail(rows: list, limit: int) -> list:
    """The `limit` LAST (newest) elements, order preserved.

    `seq[-limit:]` looks equivalent and is not: at `limit == 0` it returns the
    WHOLE sequence instead of nothing, so a caller asking for zero rows gets
    every row.
    """
    if limit <= 0:
        return []
    return rows[len(rows) - limit:]


def _matching(args, need: int = 0):
    """Yield `(source, record)` for every record passing `args`.

    A GENERATOR, on purpose. `stats` and `sessions` walk the whole history and
    only ever need one record at a time; collecting first turned a 25 MB
    streaming pass over 839 MB of traces into a 1.37 GB list, purely so it
    could then be sorted by a key those two commands do not care about. Callers
    that need ordering sort what they keep (`ls`, `search`, `show`).

    `source` is the JSONL file the record lives in, or `""` when the body is
    already in hand — the fallback parses every line anyway, so re-reading it
    would buy nothing. `_resolve()` turns either shape into a full record.

    Served from the on-disk index when the query can be, because scanning
    bodies costs ~80 s over the 839 MB of traces on this machine — almost all
    of it in `json.loads`. The index cannot answer `--contains` (it keeps no
    content), and it can be off (AGENTTRACE_INDEX=0) or unavailable (unwritable
    cache dir); in every one of those cases this falls back to reading bodies,
    which yields the same rows, only slower.

    Index rows are projections, not full records: they carry the top-level
    fields `matches` reads plus a nested `response.usage` shaped exactly like
    the real thing, so one `matches()` and one aggregation loop serve both
    shapes without branching.

    `need` is passed through to the index: when the caller only wants the
    newest N rows (`ls`), whole days can be skipped. It must stay 0 for every
    aggregating command — `stats` needs all of history, not today's slice.
    """
    if not args.contains and trace_index.index_enabled():
        yield from trace_index.entries(
            args.dirs, accept=lambda row: matches(row, args), need=need)
        return
    for _, _, rec in iter_records(args.dirs):
        if matches(rec, args):
            yield ("", rec)


def _sorted(args, need: int = 0) -> list[tuple[str, dict]]:
    """`_matching` as a list, oldest first — for callers that slice by time."""
    rows = list(_matching(args, need=need))
    rows.sort(key=lambda pair: pair[1].get("ts") or "")
    return rows


def _resolve(pairs: list[tuple[str, dict]]) -> list[dict]:
    """Full records: seek by byte range when indexed, use as-is otherwise."""
    out: list[dict] = []
    for src, row in pairs:
        rec = trace_index.load(src, row) if src else row
        if rec is not None:
            out.append(rec)
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def preview(rec: dict) -> str:
    """One-line human summary of a record."""
    ev = rec.get("event", "?")
    agent = rec.get("agent", "?")
    ts = (rec.get("ts") or "")[11:19]
    model = rec.get("model") or rec.get("provider") or "-"
    rid = (rec.get("request_id") or "")[:8]
    bits = []

    if ev in ("llm_request", "llm_response", "llm_error"):
        req = rec.get("request") or {}
        resp = rec.get("response") or {}
        if ev == "llm_request":
            n = req.get("message_count")
            t = req.get("tool_count")
            bits.append(f"msgs={n if n is not None else '?'} tools={t if t is not None else '?'}")
        elif ev == "llm_response":
            u = resp.get("usage") or {}
            content = resp.get("content") or ""
            txt = " ".join(content.split())[:70]
            bits.append(f"in={u.get('input_tokens')} out={u.get('output_tokens')}")
            if txt:
                bits.append(repr(txt))
        else:
            e = rec.get("error") or {}
            bits.append(f"{e.get('type', 'error')}: {(e.get('message') or '')[:60]}")
    elif ev == "user_prompt":
        req = rec.get("request") or {}
        msgs = req.get("messages") or []
        last = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                last = m.get("content") or ""
                if isinstance(last, list):
                    last = " ".join(
                        p.get("text", "") for p in last if isinstance(p, dict)
                    )
                break
        bits.append(repr(" ".join(str(last).split())[:70]))
    elif ev in ("tool_call", "tool_result"):
        t = rec.get("tool") or {}
        bits.append(f"{t.get('name', '?')} {t.get('status', '')}")
    elif ev == "assistant_message":
        resp = rec.get("response") or {}
        bits.append(repr(" ".join((resp.get("content") or "").split())[:70]))
    else:
        bits.append(rec.get("note", "")[:70])

    return f"{ts} {agent:6s} {ev:16s} {model:28s} {rid:8s} " + " | ".join(b for b in bits if b)


#: What each role means, printed beside it by `show`. The point is to say WHO
#: wrote the text without the reader having to know the wire protocol's role
#: vocabulary — the same annotation a hand-written request example carries.
ROLE_NOTES = {
    "system": "written by developer",
    "developer": "written by developer",
    "user": "user input",
    "assistant": "model output",
    "tool": "tool result",
}

#: Column the role annotations line up on, so a block of messages reads as one
#: column of comments instead of a ragged edge.
ROLE_NOTE_COL = 46


def canonical_request(rec: dict) -> dict:
    """The request as the classic `{model, messages: [{role, content}]}` shape.

    The three adapters reach the same prompt three different ways:

      hermes  system prompt in BOTH `messages[0]` and `system_prompt`
      pi      system prompt in `messages[0]` only
      codex   `messages: []`, system prompt in `system_prompt` + `instructions`

    Printing `request` verbatim therefore showed the system prompt TWICE for
    nearly every hermes record — two identical multi-KB blocks, one headed
    `messages[0]` and one headed `system_prompt`, which reads like two
    different prompts — while a codex record appeared to have no prompt at
    all. Worse, `instructions: null` and friends were printed as noise.

    The canonical view has exactly one system message, first, and no separate
    `system_prompt`/`instructions` to disagree with it. Nothing is lost:
    measured over the traces on this machine, 1812/1812 hermes records have
    `system_prompt == messages[system].content` byte for byte, and codex sets
    `instructions == system_prompt`.
    """
    raw = rec.get("request")
    if isinstance(raw, list):
        req: dict = {"messages": raw}
    elif isinstance(raw, dict):
        req = raw
    else:
        req = {}

    messages = [dict(m) for m in (req.get("messages") or []) if isinstance(m, dict)]
    separate = next((req[key] for key in ("system_prompt", "instructions")
                     if isinstance(req.get(key), str) and req[key]), None)
    has_system = any(str(m.get("role", "")).lower() in ("system", "developer")
                     for m in messages)
    if separate and not has_system:
        # Codex-style record: the prompt only exists as a field, so promote it
        # to messages[0] rather than rendering an empty conversation.
        messages.insert(0, {"role": "system", "content": separate})

    out: dict = {}
    # `model` leads, exactly as it does in a real request payload; transport
    # details (provider / api_mode / base_url) stay in `meta`, where they
    # describe the record rather than the payload.
    if rec.get("model") is not None:
        out["model"] = rec["model"]
    if messages:
        out["messages"] = messages
    if req.get("tools"):
        out["tools"] = req["tools"]
    for key in ("message_count", "tool_count", "char_count",
                "approx_input_tokens", "max_tokens"):
        if req.get(key) is not None:
            out[key] = req[key]
    return out


def _render_messages(messages: list[dict]) -> str:
    """`messages` as indented JSON, each `role` line annotated with its author.

    The annotation goes on the line that actually carries the role, not on the
    opening brace — `json.dumps` puts `{` first, and a comment there reads as
    labelling the object rather than saying who wrote it.
    """
    chunks = []
    for message in messages:
        note = ROLE_NOTES.get(str(message.get("role", "")).lower())
        lines = json.dumps(message, ensure_ascii=False, indent=2,
                           default=str).splitlines()
        role_line = next((i for i, ln in enumerate(lines) if '"role"' in ln), 0)
        for i, line in enumerate(lines):
            line = "    " + line
            if i == role_line and note:
                line += " " * max(2, ROLE_NOTE_COL - len(line))
                line += f"// ← {note}"
            lines[i] = line
        chunks.append("\n".join(lines))
    return ",\n".join(chunks)


def _render_request(rec: dict) -> str:
    """`canonical_request` as annotated JSON.

    The `//` comments make the block unparseable by `json.loads`, on purpose:
    `show` output already carries `=== block ===` headers so it was never a
    bare JSON document, and the annotation is the entire point — it says who
    wrote each message at a glance.
    """
    view = canonical_request(rec)
    if not view:
        return "{}"
    blocks = []
    for key, value in view.items():
        if key == "messages":
            body = ['  "messages": [', _render_messages(value), "  ]"]
        elif isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, indent=2,
                                  default=str).splitlines()
            body = [f'  "{key}": {rendered[0]}'] + ["  " + ln
                                                     for ln in rendered[1:]]
        else:
            body = [f'  "{key}": {json.dumps(value, ensure_ascii=False)}']
        blocks.append("\n".join(body))
    return "{\n" + ",\n".join(blocks) + "\n}"


def print_full(rec: dict) -> None:
    def block(title, obj):
        if obj:
            print(f"\n=== {title} ===")
            print(json.dumps(obj, ensure_ascii=False, indent=2))

    hdr = {k: rec.get(k) for k in
           ("ts", "agent", "event", "session_id", "turn_id", "request_id",
            "provider", "model", "api_mode", "base_url", "cwd", "duration_ms")
           if rec.get(k) is not None}
    block("meta", hdr)
    # Gated on the field, not on the rendered view: a response-only record has
    # no `request` at all, and must not grow an empty request block just
    # because `model` is on the record.
    if "request" in rec:
        print("\n=== request ===")
        print(_render_request(rec))
    block("response", rec.get("response"))
    block("tool", rec.get("tool"))
    block("error", rec.get("error"))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_ls(args) -> int:
    # Select the `limit` NEWEST matches, then print them in the requested
    # order. The old version sorted ascending and took [:limit], which selects
    # the OLDEST records on disk — on any trace longer than --limit, the recent
    # rows the README calls "recent records" were exactly the ones never
    # printed (60 records, default limit 40, printed 10:00..10:39 and dropped
    # 10:40..10:59 entirely).
    # `need` limits the index to the days that can still hold the newest rows,
    # so a cold `ls` does not parse the whole history first.
    recs = _resolve(_tail(_sorted(args, need=args.limit), args.limit))
    if args.reverse:
        recs.reverse()
    for r in recs:
        print(preview(r))
    if not recs:
        print(f"(no records under: {', '.join(str(d) for d in args.dirs) or '(no trace dirs found)'})",
              file=sys.stderr)
        return 1
    return 0


def cmd_show(args) -> int:
    """Print the full request and response for one request_id (or session+ordinal)."""
    # Bodies are fetched here, by seeking to their byte range: the index only
    # answers *which* record. Narrowing by request_id first keeps that to a
    # handful of seeks instead of loading the whole trace.
    rows = [pair for pair in _matching(args)
            if not args.request_id or pair[1].get("request_id") == args.request_id]
    picked = _resolve(rows)
    if not picked:
        print(f"no record matched request_id={args.request_id}", file=sys.stderr)
        return 1
    # A request_id pair = request + response (+ error). Show them in order.
    picked.sort(key=lambda r: ((r.get("event") != "llm_request"), r.get("ts") or ""))
    for i, rec in enumerate(picked):
        if i:
            print("\n" + "=" * 70)
        print_full(rec)
    return 0


def cmd_search(args) -> int:
    # `--contains` greps record bodies, which the index deliberately does not
    # keep, so this always takes the full-scan path. It still gets `_tail`'s
    # newest-N selection (the old `recs[-args.limit:]` returned EVERYTHING at
    # `--limit 0`, because `-0` is a no-op slice).
    args.contains = args.query
    pairs = _sorted(args)
    for r in _resolve(_tail(pairs, args.limit)):
        print(preview(r))
    print(f"\n{len(pairs)} match(es) for {args.query!r}", file=sys.stderr)
    return 0 if pairs else 1


def cmd_sessions(args) -> int:
    by_sess: dict[str, dict] = defaultdict(
        lambda: {"agent": "", "ts": "", "n": 0, "calls": 0, "err": 0, "model": set(), "cwd": ""}
    )
    # Filters now apply here too. `sessions` used to read every record in every
    # trace dir regardless of --agent/--model/--event/--session/--since, while
    # the README promised "every command" accepted them; `stats` and `ls`
    # honoured them and this one silently did not.
    for _, rec in _matching(args):
        sid = rec.get("session_id") or "(none)"
        e = by_sess[sid]
        e["agent"] = rec.get("agent", "")
        e["ts"] = min(e["ts"] or rec.get("ts", ""), rec.get("ts", ""))
        e["n"] += 1
        e["cwd"] = e["cwd"] or rec.get("cwd", "")
        if rec.get("model"):
            e["model"].add(rec["model"])
        if rec.get("event") == "llm_request":
            e["calls"] += 1
        if rec.get("event") == "llm_error":
            e["err"] += 1
    # Oldest-first display, but of the NEWEST `limit` sessions — same rule as
    # `ls`, so a long history shows the conversations still in front of you.
    rows = sorted(by_sess.items(), key=lambda kv: kv[1]["ts"])
    for sid, e in _tail(rows, args.limit):
        models = ",".join(sorted(e["model"])) or "-"
        print(f"{sid[:20]:20s} {e['agent']:6s} {e['ts'][:19]} calls={e['calls']:<4d} "
              f"recs={e['n']:<4d} err={e['err']:<3d} {models[:30]:30s} {e['cwd'][:40]}")
    return 0


def cmd_stats(args) -> int:
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"calls": 0, "err": 0, "in": 0, "out": 0, "cache_r": 0, "dur": 0.0}
    )

    def as_int(v) -> int:
        """usage fields arrive as int, float, or numeric string depending on provider."""
        if isinstance(v, bool):
            return 0
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            try:
                return int(float(v))
            except ValueError:
                return 0
        return 0

    for _, rec in _matching(args):
        key = (rec.get("agent", "?"), rec.get("model") or "-")
        a = agg[key]
        if rec.get("event") == "llm_response":
            a["calls"] += 1
            u = (rec.get("response") or {}).get("usage") or {}
            # Repaired total, not the raw field: see total_input_tokens —
            # records from the Pi adapter used to exclude the cache here, so
            # `stats` under-reported pi's input by ~27x against its own cache
            # column.
            a["in"] += common.total_input_tokens(u)
            a["out"] += as_int(u.get("output_tokens"))
            a["cache_r"] += as_int(u.get("cache_read_tokens"))
            a["dur"] += as_int(rec.get("duration_ms"))
        elif rec.get("event") == "llm_error":
            a["err"] += 1
    print(f"{'agent':8s} {'model':30s} {'calls':>6s} {'err':>4s} {'in_tok':>10s} "
          f"{'out_tok':>9s} {'cache_rd':>10s} {'avg_ms':>7s}")
    for (agent, model), a in sorted(agg.items()):
        avg = a["dur"] // a["calls"] if a["calls"] else 0
        print(f"{agent:8s} {model[:30]:30s} {a['calls']:6d} {a['err']:4d} {int(a['in']):10d} "
              f"{int(a['out']):9d} {int(a['cache_r']):10d} {int(avg):7d}")
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # Filters live on the top-level parser AND on every subparser, so both
    # `agenttrace --limit 5 ls` and `agenttrace ls --limit 5` work. Subparser copies
    # default to SUPPRESS so an unset subparser flag never clobbers a value the
    # user already gave before the subcommand.
    # Named `filters`, not `common`: the module-level `common` is
    # agenttrace_common, and a local of the same name here would silently
    # shadow it for anyone reading `common.AGENTS` below.
    filters = argparse.ArgumentParser(add_help=False)
    filters.add_argument("--agent", choices=common.AGENTS, default=argparse.SUPPRESS)
    filters.add_argument("--model", help="substring match on model", default=argparse.SUPPRESS)
    filters.add_argument("--event", default=argparse.SUPPRESS)
    filters.add_argument("--session", default=argparse.SUPPRESS)
    filters.add_argument("--since", help="ISO timestamp lower bound, e.g. 2026-09-25",
                         default=argparse.SUPPRESS)
    filters.add_argument("--contains", help="substring match anywhere in the record (JSON)",
                         default=argparse.SUPPRESS)
    filters.add_argument("--limit", type=int, default=argparse.SUPPRESS)
    filters.add_argument("--reverse", action="store_true", default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(
        prog="agenttrace", description=__doc__, parents=[filters],
        formatter_class=argparse.RawDescriptionHelpFormatter)

    sub = ap.add_subparsers(dest="cmd", required=True)
    # `dirs` is attached per-subcommand, not at top level: a greedy top-level
    # nargs="*" would swallow the subcommand name itself
    # (`agenttrace watch /path` parsed /path as the command).
    def _add(name, **kw):
        return sub.add_parser(name, parents=[filters], **kw)

    def _dirs(p):
        # Added LAST, and it has to be. argparse fills a leading nargs="*"
        # greedily, so with `dirs` declared first the binding of
        # `agenttrace show <id> <dir>` came out as dirs=[<id>], request_id=<dir>
        # — the command then answered "no record matched request_id=/path".
        # Declared last, the first token belongs to the command's own argument
        # and everything after it is a directory, which is also what the README
        # documents (`agenttrace show <request_id>`, dirs optional and trailing).
        p.add_argument("dirs", nargs="*", type=Path, default=[],
                       help="trace dirs (default: per-agent trace locations)")
        return p

    _dirs(_add("ls", help="recent records, one line each"))

    p_show = _add("show", help="full request+response for one LLM call")
    p_show.add_argument("request_id")
    _dirs(p_show)

    p_search = _add("search", help="grep across all record content")
    p_search.add_argument("query")
    _dirs(p_search)

    _dirs(_add("sessions", help="group records by session id"))
    _dirs(_add("stats", help="calls/tokens/errors per agent and model"))

    p_session = _add(
        "session",
        help="one conversation: every turn with its own duration (a trace is "
             "ONE turn, so this is where per-turn time lives)")
    p_session.add_argument("session_id",
                           help="session id, or the trailing part of it")
    p_session.add_argument("--failing-only", action="store_true",
                           help="list only the turns that have a failed span")
    p_session.add_argument("--db", type=Path, default=None)
    _dirs(p_session)

    p_watch = _add("watch", help="live full-screen panel of LLM traffic")
    p_watch.add_argument("--no-follow", action="store_true",
                         help="print current contents and exit (no TUI)")
    p_watch.add_argument("--history", type=int, default=0,
                         help="records of the current session to preload "
                              "(default 0: live tail only)")
    p_watch.add_argument("--all-sessions", action="store_true",
                         help="show every session instead of only the current one")
    _dirs(p_watch)

    # Platform commands (M2): no shared record filters — they operate on the
    # store, not on raw v1 records.
    p_serve = sub.add_parser("serve", help="local dashboard: indexer + HTTP API + web UI")
    p_serve.add_argument("--host", default=os.environ.get("AGENTTRACE_HOST", "127.0.0.1"))
    p_serve.add_argument("--port", type=int,
                         default=int(os.environ.get("AGENTTRACE_PORT", "8741")))
    p_serve.add_argument("--db", type=Path, default=None,
                         help="SQLite path (default: ~/.agenttrace/agenttrace.db)")
    p_serve.add_argument("--interval", type=float, default=1.0,
                         help="trace-dir scan interval seconds")
    p_serve.add_argument("--verbose", action="store_true")
    p_serve.add_argument("--daemon", action="store_true",
                         help="detach and keep running (survives this shell); "
                              "writes ~/.agenttrace/server.pid + server.log")
    p_serve.add_argument("--stop", action="store_true",
                         help="stop a daemon started with --daemon")
    p_serve.add_argument("--status", action="store_true",
                         help="report whether a daemon is running")
    _dirs(p_serve)

    p_index = sub.add_parser("index", help="one-shot (re)index of trace files into SQLite")
    p_index.add_argument("--db", type=Path, default=None)
    p_index.add_argument("--full", action="store_true",
                         help="rebuild projections from the raw archive")
    _dirs(p_index)

    p_doctor = sub.add_parser(
        "doctor",
        help="diagnose install, capture, index and dropped events "
             "(exit 1 on a problem)")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.add_argument("--fast", action="store_true",
                          help="skip the adapter-coverage scan")
    p_doctor.add_argument("--db", type=Path, default=None)
    _dirs(p_doctor)

    # Adapter ecosystem (M4): discovery + conformance, no shared filters.
    p_adapter = sub.add_parser("adapter", help="list / inspect / test adapters")
    adapter_sub = p_adapter.add_subparsers(dest="adapter_cmd", required=True)
    p_al = adapter_sub.add_parser("list", help="every known adapter, with source")
    p_al.add_argument("--json", action="store_true")
    p_ac = adapter_sub.add_parser(
        "coverage",
        help="does an installed adapter actually have traces for the sessions "
             "its agent ran? (exit 1 when a recent session is missing)")
    p_ac.add_argument("--json", action="store_true")
    p_ac.add_argument("--fresh", action="store_true", help="bypass the cache")
    p_at = adapter_sub.add_parser(
        "test",
        help="run the conformance kit over an event stream, or an adapter's "
             "shipped fixture")
    p_at.add_argument("target", help="events.jsonl path, or an adapter id")
    p_at.add_argument("--adapter", default=None,
                      help="adapter id for the capability cross-check")

    p_report = sub.add_parser(
        "report",
        help="efficiency report across recent traces (trajectory signals, "
             "never one score)")
    p_report.add_argument("--limit", type=int, default=200)
    p_report.add_argument("--verbose", action="store_true",
                          help="show the evidence behind each flagged trace")
    p_report.add_argument("--json", action="store_true")
    p_report.add_argument("--db", type=Path, default=None)
    _dirs(p_report)

    p_otlp = sub.add_parser("otlp", help="import/export OTLP JSON (M4)")
    otlp_sub = p_otlp.add_subparsers(dest="otlp_cmd", required=True)
    p_oi = otlp_sub.add_parser("import", help="OTLP JSON file -> trace dirs")
    p_oi.add_argument("file", type=Path)
    p_oi.add_argument("--adapter", default=None,
                      help="adapter_id when the payload names none")
    p_oi.add_argument("--out", type=Path, default=None,
                      help="output dir (default: first trace dir)")
    _dirs(p_oi)
    p_oe = otlp_sub.add_parser("export", help="recorded traces -> OTLP JSON")
    p_oe.add_argument("--trace", action="append", default=[],
                      help="trace_id (repeatable); default: recent traces")
    p_oe.add_argument("--limit", type=int, default=20)
    p_oe.add_argument("--db", type=Path, default=None)
    _dirs(p_oe)
    return ap


DEFAULTS = {
    "agent": None, "model": None, "event": None, "session": None,
    "since": None, "contains": None, "limit": 40, "reverse": False,
}


def cmd_serve(args) -> int:
    """Indexer + local HTTP API + dashboard. Imports are lazy: `agenttrace ls`
    must keep starting fast and dependency-free for users who never serve."""
    from server.main import platform_dir, serve

    pid_file = platform_dir() / "server.pid"
    if args.stop or args.status:
        pid = None
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if args.status:
            print(f"pid {pid or '?'} : {'running' if alive else 'not running'}")
            print(f"pid file : {pid_file}")
            print(f"log      : {platform_dir() / 'server.log'}")
            return 0 if alive else 1
        if not alive or pid is None:
            print("no running dashboard", file=sys.stderr)
            return 1
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except OSError:
                break
        try:
            pid_file.unlink()
        except OSError:
            pass
        print(f"stopped dashboard (pid {pid})")
        return 0

    if args.daemon:
        # Re-exec through the launcher so the server lands in its own session.
        launcher = Path(__file__).resolve().parent.parent / "scripts" / "serve-daemon.py"
        cmd = [sys.executable, str(launcher), "--host", args.host,
               "--port", str(args.port), "--interval", str(args.interval)]
        if args.db:
            cmd += ["--db", str(args.db)]
        cmd += [str(d) for d in args.dirs]
        return subprocess.call(cmd)

    db = args.db or Path(os.environ.get("AGENTTRACE_DB")
                         or platform_dir() / "agenttrace.db")
    serve(host=args.host, port=args.port, db_path=db, dirs=args.dirs,
          scan_interval=args.interval, verbose=args.verbose)
    return 0


def cmd_doctor(args) -> int:
    """One command that answers "why is nothing showing up?".

    Deliberately not a pile of independent scripts: the answer depends on
    cross-checking them (a writable dir with no records is a different problem
    from records the index has not seen). Exit 1 when a check is a warn/fail,
    so it can gate a first-run script.
    """
    from core.doctor import render, run
    from server.main import platform_dir

    db = args.db or Path(os.environ.get("AGENTTRACE_DB")
                         or platform_dir() / "agenttrace.db")
    report = run(args.dirs, db_path=db, fast=args.fast)
    print(render(report, as_json=args.json))
    return 0 if report.ok else 1


def cmd_index(args) -> int:
    """One-shot indexing: scan once (or full rebuild) and print the report."""
    from core.storage.indexer import Indexer
    from core.storage.sqlite_store import TraceStore
    from server.main import platform_dir
    db = args.db or Path(os.environ.get("AGENTTRACE_DB")
                         or platform_dir() / "agenttrace.db")
    store = TraceStore(db)
    report = (Indexer(store, args.dirs).full_rebuild() if args.full
              else Indexer(store, args.dirs).scan_once())
    print(json.dumps(report.__dict__, indent=2))
    store.close()
    return 0


def _coverage_rows(fresh: bool = False):
    """Coverage, from the index when there is one and from the files otherwise.

    The index is one indexed SELECT; the file fallback re-reads every trace
    line and only exists so the check still works before an index is built (or
    when it is deliberately absent).
    """
    from core import coverage as cov_mod
    from server.main import platform_dir

    db = Path(os.environ.get("AGENTTRACE_DB") or platform_dir() / "agenttrace.db")
    if db.exists():
        provider = cov_mod.store_session_provider(db)
        return cov_mod.check(traced_provider=provider) if fresh else \
            cov_mod.cached(traced_provider=provider)
    return cov_mod.check() if fresh else cov_mod.cached()


def _print_coverage_summary(json_out: bool = False) -> bool:
    """Print what the adapters are ACTUALLY capturing; True when all is well.

    Two separate facts, and only one of them is in the registry: an adapter can
    be installed, verified and enabled while capturing nothing at all. A live
    hook is loaded at PROCESS START, so an agent that was already running when
    the adapter was installed writes nothing until it restarts — which is how
    53 of 63 Pi sessions went missing with nothing anywhere saying so.
    """
    covs = _coverage_rows()
    if json_out:
        print(json.dumps([c.to_dict() for c in covs], indent=2))
        return all(c.ok for c in covs)

    gaps = [c for c in covs if c.sessions_known or c.untraced_recent]
    if not gaps:
        return True
    print("\ncoverage (sessions the agent ran vs. traces we hold):")
    for c in gaps:
        mark = "ok " if c.ok else "!! "
        print(f"  {mark}{c.adapter_id:<8} {c.sessions_covered}/{c.sessions_known} traced"
              + (f", {len(c.untraced_recent)} recent gap(s)" if c.untraced_recent else "")
              + (f", {c.untraced_historical} historical" if c.untraced_historical else ""))
    for c in gaps:
        if c.note:
            print(f"     note: {c.note}")
    for w in [c.warning() for c in covs if c.warning()]:
        print(f"\n  WARNING: {w}")
    return all(c.ok for c in covs)


def cmd_adapter(args) -> int:
    from adapter_sdk import compatibility, discovery

    if args.adapter_cmd == "list":
        entries = discovery.discover()
        if args.json:
            print(json.dumps({
                "adapters": [e.to_dict() for e in entries],
                "compatibility": [
                    {"adapter_id": c.adapter_id, "target": c.target,
                     "status": c.status, "verified_by": c.verified_by,
                     "notes": c.notes}
                    for c in compatibility.all_compatibility()],
            }, indent=2))
            return 0
        for e in entries:
            compat = compatibility.for_adapter(e.adapter_id)
            caps = e.capabilities.to_dict()
            supported = [k for k, v in caps.items() if v["level"] == "supported"]
            partial = [k for k, v in caps.items() if v["level"] == "partial"]
            print(f"{e.adapter_id:<12} {e.kind:<13} {e.source:<12} "
                  f"{compat.status if compat else 'unverified'}")
            if e.integration:
                print(f"{'':14} {e.integration}")
            if e.module:
                print(f"{'':14} module: {e.module}")
            if supported:
                print(f"{'':14} supported: {', '.join(sorted(supported))}")
            if partial:
                print(f"{'':14} partial:   {', '.join(sorted(partial))}")
        print(f"\n{len(entries)} adapter(s). "
              f"Manifests: {discovery.manifest_dir()}")
        _print_coverage_summary()
        print("Discovery reads metadata only; nothing is imported "
              "until `agenttrace adapter test <id>`.")
        return 0

    if args.adapter_cmd == "coverage":
        covs = _coverage_rows(fresh=args.fresh)
        if args.json:
            print(json.dumps([c.to_dict() for c in covs], indent=2))
            return 1 if any(not c.ok for c in covs) else 0
        print(f"{'adapter':<10}{'sessions':>9}{'traced':>8}{'recent gaps':>13}"
              f"{'historical':>11}  source")
        for c in covs:
            print(f"{c.adapter_id:<10}{c.sessions_known:>9}{c.sessions_covered:>8}"
                  f"{len(c.untraced_recent):>13}{c.untraced_historical:>11}"
                  f"  {c.source}")
            if c.untraced_unknown_age:
                print(f"{'':<10}{c.untraced_unknown_age} untraced session(s) of "
                      f"unknown age (no install marker to compare against)")
            if c.error:
                print(f"{'':<10}coverage unknown: {c.error}")
            if c.note:
                print(f"{'':<10}note: {c.note}")
            if c.undecided:
                print(f"{'':<10}verdict withheld (no index yet)")
            for name, started in c.untraced_recent[:5]:
                print(f"{'':<10}  recent gap: {started}  {name[:60]}")
        for w in [c.warning() for c in covs if c.warning()]:
            print()
            print(f"WARNING: {w}")
        return 1 if any(not c.ok for c in covs) else 0

    # adapter test <events.jsonl | adapter_id>
    from adapter_sdk.testing import check_stream

    target = Path(args.target).expanduser()
    adapter_id = args.adapter
    if not target.is_file():
        adapter_id = adapter_id or args.target
        fixture = _fixture_stream_for(adapter_id)
        if fixture is None:
            print(f"no such stream file, and no fixture stream for "
                  f"adapter {adapter_id!r}.\nProduce one: point the adapter at "
                  "a temp file and run a small scenario, then pass the path.",
                  file=sys.stderr)
            return 2
        target = fixture
        print(f"using fixture stream: {target}")
    report = check_stream(target, adapter_id=adapter_id)
    print(report.render())
    return 0 if report.ok else 1


def _fixture_stream_for(adapter_id: str) -> Path | None:
    """Adapter-shipped event streams under test/fixtures/adapters/."""
    root = Path(__file__).resolve().parent.parent
    candidate = root / "test" / "fixtures" / "adapters" / f"{adapter_id}.jsonl"
    return candidate if candidate.is_file() else None


def _fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    if ms < 3_600_000:
        return f"{ms / 60_000:.1f}m"
    return f"{ms / 3_600_000:.1f}h"


def cmd_session(args) -> int:
    """Per-turn timing for one conversation, from the index."""
    from core.query import session_detail
    from core.storage.indexer import Indexer
    from core.storage.sqlite_store import TraceStore
    from server.main import platform_dir

    db = args.db or Path(os.environ.get("AGENTTRACE_DB")
                         or platform_dir() / "agenttrace.db")
    store = TraceStore(db)
    span_count = store.one("SELECT COUNT(*) AS n FROM spans")
    if not (span_count["n"] if span_count else 0):
        Indexer(store, args.dirs).scan_once()

    # Match the whole id or its tail: these ids are long paths for pi and
    # opaque handles for hermes, and nobody wants to paste a full path.
    exact = store.one("SELECT DISTINCT session_id FROM spans WHERE session_id = ?",
                      (args.session_id,))
    sid = exact["session_id"] if exact else None
    if sid is None:
        like = store.query(
            "SELECT session_id, MAX(COALESCE(end_ts, start_ts)) AS last"
            " FROM spans WHERE session_id LIKE ?"
            " GROUP BY session_id ORDER BY last DESC LIMIT 2",
            (f"%{args.session_id}%",))
        if not like:
            print(f"no session matching {args.session_id!r}", file=sys.stderr)
            store.close()
            return 1
        sid = like[0]["session_id"]

    detail = session_detail(store, sid)
    store.close()
    if detail is None:
        print("session has no turns", file=sys.stderr)
        return 1

    print(f"session   {detail['session_id']}")
    print(f"adapter   {detail['adapter_id']}")
    print(f"window    {detail['start']} -> {detail['end']}")
    print(f"turns     {detail['turn_count']}"
          f"   p50 {_fmt_ms(detail['turn_p50_ms'])}"
          f"   p95 {_fmt_ms(detail['turn_p95_ms'])}")
    # The two numbers mean different things and are both useful: the agent's own
    # time, and how long the conversation was open.
    # Two different numbers, deliberately apart: what the agent spent, and how
    # long the conversation was open. Collapsing them makes an idle session look
    # like a busy one.
    print(f"agent work {_fmt_ms(detail['work_ms'])}"
          f"   wall {_fmt_ms(detail['wall_ms'])}"
          f"   outside turns {_fmt_ms(detail['outside_ms'])}"
          + (f"   (excluded {detail['work_excluded_turns']} interrupted turn(s))"
             if detail.get("work_excluded_turns") else ""))
    print(f"tokens    in {detail['tokens']['input']:,}"
          f"  out {detail['tokens']['output']:,}"
          f"  cache_read {detail['tokens']['cache_read']:,}")
    if detail["models"]:
        print("models    " + ", ".join(
            f"{m}×{v['turns']}" for m, v in detail["models"].items()))

    print()
    print(f"{'#':>3}  {'start':<20} {'duration':>9} {'status':<11} "
          f"{'spans':>5} {'tokens':>9}  kinds")
    for i, turn in enumerate(detail["turns"], 1):
        if args.failing_only and not turn["errors"]:
            continue
        mark = " (interrupted)" if turn.get("interrupted") else ""
        print(f"{i:>3}  {turn['start']:<20} {_fmt_ms(turn['duration_ms']):>9} "
              f"{turn['status']:<11} {turn['spans']:>5} "
              f"{turn['tokens']['input']:>9,}  {','.join(turn['kinds'])}{mark}")
    return 0


def cmd_report(args) -> int:
    """The efficiency report, over the index (or a one-shot scan if there is none).

    Reads the STORE, not the raw files: the report needs every trace's spans,
    and re-parsing the JSONL for that is the thing the index exists to avoid.
    """
    from core.storage.indexer import Indexer
    from core.storage.sqlite_store import TraceStore
    from evaluation.report import render, report
    from server.main import platform_dir

    db = args.db or Path(os.environ.get("AGENTTRACE_DB")
                         or platform_dir() / "agenttrace.db")
    store = TraceStore(db)
    span_count = store.one("SELECT COUNT(*) AS n FROM spans")
    if not (span_count["n"] if span_count else 0):
        # No index yet: build one from the dirs the user pointed at, so the
        # command works on a fresh machine without a separate `index` run.
        Indexer(store, args.dirs).scan_once()
    rep = report(store, limit=args.limit, agent=args.agent, since=args.since)
    print(json.dumps(rep, indent=2) if args.json
          else render(rep, verbose=args.verbose))
    store.close()
    return 0


def cmd_otlp(args) -> int:
    from adapters.otlp import export_traces, import_file
    from core.query import get_trace, list_traces
    from core.storage.sqlite_store import TraceStore
    from server.main import platform_dir

    if args.otlp_cmd == "import":
        imported = list(import_file(args.file, args.adapter))
        if not imported:
            print("no spans found in the payload", file=sys.stderr)
            return 1
        out = args.out or (args.dirs[0] if args.dirs else None)
        if out is None:
            print("no output dir: pass --out or a trace dir", file=sys.stderr)
            return 2
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        dest = out / f"otlp-import-{time.strftime('%Y%m%d')}.jsonl"
        # Through the SDK sink, not a raw json.dumps: a foreign payload is
        # untrusted input and must get the same validate + redact treatment as
        # an adapter-native event (decision D7). Writing it directly would put
        # any credential the producer happened to include on disk verbatim.
        from adapter_sdk.event_sink import JsonlEventSink
        sink = JsonlEventSink(dest)
        written = sink.emit_many(imported)
        print(f"imported {written} event(s) -> {dest}")
        if sink.write_errors:
            print(f"{sink.write_errors} event(s) refused: "
                  f"{'; '.join(sink.violations[:3])}", file=sys.stderr)
        return 0 if written else 1

    # export — from the RAW events, not the projection: the projection merges
    # a span's two halves into one row, which is right for a UI and lossy for
    # an interchange (per-half attributes, per-half timestamps).
    db = args.db or Path(os.environ.get("AGENTTRACE_DB")
                         or platform_dir() / "agenttrace.db")
    store = TraceStore(db)
    traces: list[dict] = []
    if args.trace:
        for tid in args.trace:
            found = get_trace(store, tid)
            if found is not None:
                traces.append(found)
    else:
        for summary in list_traces(store, limit=args.limit)["traces"]:
            found = get_trace(store, summary["trace_id"])
            if found is not None:
                traces.append(found)
    events_by_trace = {t["trace_id"]: list(store.iter_events(t["trace_id"]))
                       for t in traces}
    store.close()
    if not traces:
        print("no traces to export (run `agenttrace index` first)", file=sys.stderr)
        return 1
    print(json.dumps(export_traces(traces, events_by_trace), ensure_ascii=False))
    return 0


def cmd_watch(args) -> int:
    """Live full-screen panel. Lives in agenttrace_watch to keep the TUI isolated."""
    try:
        from agenttrace_watch import watch
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from agenttrace_watch import watch
    # The subparser exposes --no-follow; the TUI module reads a positive flag.
    args.follow = not args.no_follow
    return watch(args)


def main(argv=None) -> int:
    # Python sets SIGPIPE to SIG_IGN at interpreter start, so any agenttrace
    # command piped into a short-circuiting reader (`agenttrace ls | head -5`,
    # `agenttrace search x | grep -q`) dies with a BrokenPipeError traceback
    # instead of exiting quietly. Restore the default for every subcommand.
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, AttributeError, ValueError, OSError):
        pass

    args = build_parser().parse_args(argv)
    for k, v in DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    # `adapter list` / `adapter test` do not take trace dirs; only the
    # commands that scan traces declare them.
    if not getattr(args, "dirs", None):
        args.dirs = default_trace_dirs()
    else:
        args.dirs = [d.expanduser().resolve() for d in args.dirs]

    return {
        "ls": cmd_ls, "show": cmd_show, "search": cmd_search,
        "sessions": cmd_sessions, "stats": cmd_stats, "watch": cmd_watch,
        "serve": cmd_serve, "index": cmd_index, "doctor": cmd_doctor,
        "adapter": cmd_adapter, "otlp": cmd_otlp, "report": cmd_report,
        "session": cmd_session,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
