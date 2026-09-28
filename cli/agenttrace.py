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
import sys
from collections import defaultdict
from pathlib import Path

SCHEMA_V = 1


# --------------------------------------------------------------------------
# trace discovery
# --------------------------------------------------------------------------

def default_trace_dirs() -> list[Path]:
    """Where adapters write. Each is optional; missing dirs are skipped.

    Hermes is profile-aware: the active profile writes under $HERMES_HOME
    (which is ~/.hermes/profiles/<name> when a profile is active), while the
    default profile writes under ~/.hermes. Scan both plus the other agents'
    conventional locations.
    """
    home = Path.home()
    candidates: list[Path] = []

    if os.environ.get("AGENTTRACE_HOME"):
        candidates.append(Path(os.environ["AGENTTRACE_HOME"]))

    hermes_homes = [home / ".hermes"]
    if os.environ.get("HERMES_HOME"):
        hermes_homes.insert(0, Path(os.environ["HERMES_HOME"]))
    for hh in hermes_homes:
        candidates.append(hh / "traces")
        # every named profile too, so traces from all profiles show up
        profiles = hh / "profiles"
        if profiles.is_dir():
            for p in sorted(profiles.iterdir()):
                if p.is_dir():
                    candidates.append(p / "traces")

    candidates += [home / ".codex" / "traces",
                   # Pi's own dir (PI_CODING_AGENT_DIR) wins: the adapter
                   # writes under ~/.pi/agent/traces, and ~/.pi/traces alone
                   # silently hid every pi record from the CLI.
                   home / ".pi" / "agent" / "traces",
                   home / ".pi" / "traces",
                   home / ".agent-trace"]

    out, seen = [], set()
    for c in candidates:
        try:
            r = c.expanduser().resolve()
        except OSError:
            continue
        if r not in seen and r.is_dir():
            seen.add(r)
            out.append(r)
    return out


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
    block("request", rec.get("request"))
    block("response", rec.get("response"))
    block("tool", rec.get("tool"))
    block("error", rec.get("error"))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_ls(args) -> int:
    recs = [r for _, _, r in iter_records(args.dirs) if matches(r, args)]
    recs.sort(key=lambda r: r.get("ts") or "")
    if args.reverse:
        recs.reverse()
    for r in recs[: args.limit]:
        print(preview(r))
    if not recs:
        print(f"(no records under: {', '.join(str(d) for d in args.dirs) or '(no trace dirs found)'})",
              file=sys.stderr)
        return 1
    return 0


def cmd_show(args) -> int:
    """Print the full request and response for one request_id (or session+ordinal)."""
    picked: list[dict] = []
    for _, _, rec in iter_records(args.dirs):
        if not matches(rec, args):
            continue
        if args.request_id and rec.get("request_id") != args.request_id:
            continue
        picked.append(rec)
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
    args.contains = args.query
    recs = [r for _, _, r in iter_records(args.dirs) if matches(r, args)]
    recs.sort(key=lambda r: r.get("ts") or "")
    for r in recs[-args.limit:]:
        print(preview(r))
    print(f"\n{len(recs)} match(es) for {args.query!r}", file=sys.stderr)
    return 0 if recs else 1


def cmd_sessions(args) -> int:
    by_sess: dict[str, dict] = defaultdict(
        lambda: {"agent": "", "ts": "", "n": 0, "calls": 0, "err": 0, "model": set(), "cwd": ""}
    )
    for _, _, rec in iter_records(args.dirs):
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
    for sid, e in sorted(by_sess.items(), key=lambda kv: kv[1]["ts"]):
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

    for _, _, rec in iter_records(args.dirs):
        if not matches(rec, args):
            continue
        key = (rec.get("agent", "?"), rec.get("model") or "-")
        a = agg[key]
        if rec.get("event") == "llm_response":
            a["calls"] += 1
            u = (rec.get("response") or {}).get("usage") or {}
            a["in"] += as_int(u.get("input_tokens"))
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
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--agent", choices=["hermes", "codex", "pi"], default=argparse.SUPPRESS)
    common.add_argument("--model", help="substring match on model", default=argparse.SUPPRESS)
    common.add_argument("--event", default=argparse.SUPPRESS)
    common.add_argument("--session", default=argparse.SUPPRESS)
    common.add_argument("--since", help="ISO timestamp lower bound, e.g. 2026-09-25",
                        default=argparse.SUPPRESS)
    common.add_argument("--contains", help="substring match anywhere in the record (JSON)",
                        default=argparse.SUPPRESS)
    common.add_argument("--limit", type=int, default=argparse.SUPPRESS)
    common.add_argument("--reverse", action="store_true", default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(
        prog="agenttrace", description=__doc__, parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter)

    sub = ap.add_subparsers(dest="cmd", required=True)
    # `dirs` is attached per-subcommand, not at top level: a greedy top-level
    # nargs="*" would swallow the subcommand name itself
    # (`agenttrace watch /path` parsed /path as the command).
    def _add(name, **kw):
        p = sub.add_parser(name, parents=[common], **kw)
        p.add_argument("dirs", nargs="*", type=Path, default=[],
                       help="trace dirs (default: per-agent trace locations)")
        return p

    _add("ls", help="recent records, one line each")
    _add("show", help="full request+response for one LLM call").add_argument("request_id")
    _add("search", help="grep across all record content").add_argument("query")
    _add("sessions", help="group records by session id")
    _add("stats", help="calls/tokens/errors per agent and model")
    p_watch = _add("watch", help="live full-screen panel of LLM traffic")
    p_watch.add_argument("--no-follow", action="store_true",
                         help="print current contents and exit (no TUI)")
    p_watch.add_argument("--history", type=int, default=0,
                         help="records of the current session to preload "
                              "(default 0: live tail only)")
    p_watch.add_argument("--all-sessions", action="store_true",
                         help="show every session instead of only the current one")
    return ap


DEFAULTS = {
    "agent": None, "model": None, "event": None, "session": None,
    "since": None, "contains": None, "limit": 40, "reverse": False,
}


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
    if not args.dirs:
        args.dirs = default_trace_dirs()
    else:
        args.dirs = [d.expanduser().resolve() for d in args.dirs]

    return {
        "ls": cmd_ls, "show": cmd_show, "search": cmd_search,
        "sessions": cmd_sessions, "stats": cmd_stats, "watch": cmd_watch,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
