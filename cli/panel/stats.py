"""agenttrace watch — the one-line spend row above the footer

Part of the `agenttrace watch` TUI; split out of agenttrace_watch.py so each
piece has one job and one test surface. The main-loop module re-exports the
names the CLI and the tests reach for.
"""
from __future__ import annotations

from panel.scoring import score_prompt
from panel.text import (C_AI, C_BOLD, C_DIM, C_RED, C_RESET, C_YELLOW, _as_text,
                        _bar, _fmt_tok, strip_ansi, vlen)

from agenttrace_common import cache_hit_rate, total_input_tokens

def session_stats(recs: list[dict], width: int | None = None) -> str | None:
    """One row: session token spend, KV-cache hit rate, average prompt score.

    Sums the usage inside `recs` — already scoped to the current session and
    filter — so the line answers "what has THIS conversation cost", not what
    the whole trace file ever contained. Cache rate is cached_read/input
    across all replies: the single number that says how much of the input
    the provider served from prompt cache instead of recomputing it.

    The input denominator goes through `total_input_tokens`, not straight off
    `input_tokens`: records written by the Pi adapter before the usage fix
    carried pi-ai's *uncached* input, which made this rate come out at 2703%
    against the real traces on this machine.

    With `width`, whole segments drop from the right until the line fits
    (token spend first: it is the headline number; a mid-segment hard clip
    would leave `cache 90.` — a truncated percentage is worse than none).
    """
    in_t = out_t = cached = 0
    scores: list[int] = []

    def _i(u: dict, k: str) -> int:
        try:
            return int(u.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    for r in recs:
        ev = r.get("event")
        if ev == "llm_response":
            u = (r.get("response") or {}).get("usage") or {}
            in_t += total_input_tokens(u)
            out_t += _i(u, "output_tokens")
            cached += _i(u, "cache_read_tokens")
        elif ev == "user_prompt":
            msgs = ((r.get("request") or {}).get("messages") or [])
            txt = "".join(_as_text(m.get("content")) for m in msgs
                          if isinstance(m, dict))
            scores.append(score_prompt(txt)[0])
    if not (in_t or out_t or scores):
        return None
    bits: list[str] = []
    if in_t or out_t:
        bits.append(f"Σ tok {C_BOLD}{_fmt_tok(in_t + out_t)}{C_RESET}"
                    f"{C_DIM} (in {_fmt_tok(in_t)} · out {_fmt_tok(out_t)}){C_RESET}")
        pct = cache_hit_rate(cached, in_t) if in_t and cached else None
        if pct is not None:
            hue = C_AI if pct >= 80 else (C_YELLOW if pct >= 50 else C_RED)
            # Number + meter together: the % reads at a glance, the bar makes
            # 92% vs 60% visible without parsing digits.
            bits.append(f"{C_DIM}cache {hue}{pct:.1f}%{C_RESET}"
                        f" {hue}{_bar(pct)}{C_RESET}")
    if scores:
        avg = round(sum(scores) / len(scores))
        hue = C_AI if avg >= 70 else (C_YELLOW if avg >= 50 else C_RED)
        bits.append(f"{C_DIM}prompt {hue}{avg}{C_RESET}{C_DIM} avg · {len(scores)} turns{C_RESET}")
    if width is not None:
        while bits:
            line = f"  {C_DIM}" + " · ".join(bits) + C_RESET
            if vlen(strip_ansi(line)) <= width:
                return line
            bits.pop()               # drop whole segments, right to left
        return None
    return f"  {C_DIM}" + " · ".join(bits) + C_RESET


