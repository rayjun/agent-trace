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

# The CLI is installed as a symlink into ~/.local/bin, so resolve() lands back
# in the repo and the shared helpers are reachable from there.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
import agenttrace_common as common  # noqa: E402

# Sibling modules (trace_index, panel/) live beside this file — the CLI is
# invoked through a symlink, so resolve() first lands us back in the repo.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import trace_index  # noqa: E402

SCHEMA_V = common.SCHEMA_V


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

    p_watch = _add("watch", help="live full-screen panel of LLM traffic")
    p_watch.add_argument("--no-follow", action="store_true",
                         help="print current contents and exit (no TUI)")
    p_watch.add_argument("--history", type=int, default=0,
                         help="records of the current session to preload "
                              "(default 0: live tail only)")
    p_watch.add_argument("--all-sessions", action="store_true",
                         help="show every session instead of only the current one")
    _dirs(p_watch)
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
