#!/usr/bin/env python3
"""`agenttrace show` must render the request as the classic `{model, messages}`.

Reference shape:

    {
      "model": "Qwen3-0.6B",
      "messages": [
        { "role": "system",  ... },   // ← written by developer
        { "role": "user",    ... }    // ← user input
      ]
    }

Printing the stored `request` verbatim did NOT produce that. The three
adapters reach the same prompt three different ways:

  hermes  system prompt in BOTH `messages[0]` and `system_prompt`
  pi      system prompt in `messages[0]` only
  codex   `messages: []`, system prompt in `system_prompt` + `instructions`

Measured over this machine's traces: 1812/1813 hermes records carry the
system prompt twice, byte-identical in both places — so `show` printed a
~10 KB block and then printed it again under a different heading, which reads
as two different prompts. Meanwhile `instructions: null` was rendered as
noise, `model` was nowhere near `messages`, and a codex record showed an
empty conversation with no prompt in sight.
"""
from __future__ import annotations

import io
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli"))

import agenttrace  # noqa: E402

fails: list[str] = []
SYS = "# Identity\n\nYou are the assistant.\n" * 5


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


def show(rec: dict) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        agenttrace.print_full(rec)
    return buf.getvalue()


def request_block(out: str) -> str:
    """Just the `=== request ===` block, or "" when there is none."""
    if "=== request ===" not in out:
        return ""
    blk = out.split("=== request ===", 1)[1].lstrip("\n")
    for header in ("=== meta ===", "=== response ===", "=== tool ===",
                   "=== error ==="):
        i = blk.find("\n" + header)
        if i > 0:
            blk = blk[:i]
    return blk


def times(out: str, text: str) -> int:
    """How often `text` occurs in `show` output.

    Compared against its JSON-escaped form, because content lands in the block
    via json.dumps — a raw newline in `text` would never match and the count
    would read zero for a prompt that is plainly on screen.
    """
    return out.count(json.dumps(text, ensure_ascii=False)[1:-1])


def strip_comments(text: str) -> str:
    """Remove the `// ← …` annotations so the block can be parsed as JSON."""
    return re.sub(r"\s*// ←.*$", "", text, flags=re.M)


def main() -> int:
    base = {"v": 1, "ts": "2026-09-30T10:00:00.000Z", "session_id": "s",
            "request_id": "r1", "model": "space-bunny-free"}

    print("1. hermes shape — system prompt stored twice")
    hermes_rec = {**base, "agent": "hermes", "event": "llm_request",
                  "provider": "opencode-go", "api_mode": "chat_completions",
                  "request": {
                      "messages": [{"role": "system", "content": SYS},
                                   {"role": "user", "content": "hello"}],
                      "system_prompt": SYS,      # the duplicate
                      "instructions": None,      # noise
                      "tools": ["read", "write"],
                      "message_count": 2, "tool_count": 2, "char_count": 99,
                  }}
    out = show(hermes_rec)
    blk = request_block(out)
    check("system prompt appears exactly once", times(out, SYS) == 1,
          f"count={times(out, SYS)}")
    check("`system_prompt` field not repeated as its own heading",
          '"system_prompt"' not in out, blk[:300])
    check("`instructions: null` noise gone", '"instructions"' not in out, blk[:300])
    lines = blk.splitlines()
    model_at = next((i for i, l in enumerate(lines) if '"model"' in l), -1)
    msgs_at = next((i for i, l in enumerate(lines) if '"messages"' in l), -1)
    check("`model` leads the request object",
          0 <= model_at < msgs_at, str(lines[:4]))
    check("`messages` follows it",
          any(l.strip().startswith('"messages"') for l in blk.splitlines()), blk[:300])
    parsed = json.loads(strip_comments(blk))
    check("block is parseable once annotations are stripped",
          isinstance(parsed, dict), blk[:300])
    check("parsed shape is {model, messages, tools, counts}",
          list(parsed)[:3] == ["model", "messages", "tools"], str(list(parsed)))
    check("system message comes first",
          parsed["messages"][0]["role"] == "system",
          str([m["role"] for m in parsed["messages"]]))

    print("2. role annotations sit on the `role` line")
    role_lines = [l for l in blk.splitlines() if '"role"' in l]
    check("every message is annotated", len(role_lines) == 2, str(role_lines))
    check("system annotated as developer-written",
          role_lines[0].rstrip().endswith("// ← written by developer"),
          role_lines[0])
    check("user annotated as user input",
          role_lines[1].rstrip().endswith("// ← user input"), role_lines[1])
    check("annotations align in one column",
          len({l.index("//") for l in role_lines}) == 1, str(role_lines))

    print("3. codex shape — prompt only exists as a field")
    codex_rec = {**base, "agent": "codex", "event": "llm_request",
                 "model": "gpt-5.5",
                 "request": {"messages": [], "system_prompt": SYS,
                             "instructions": SYS}}
    out = show(codex_rec)
    blk = request_block(out)
    parsed = json.loads(strip_comments(blk))
    check("system prompt promoted into messages[0]",
          parsed["messages"][0]["role"] == "system", str(parsed.get("messages")))
    check("system prompt appears exactly once (instructions is the same text)",
          times(out, SYS) == 1, f"count={times(out, SYS)}")
    check("conversation no longer looks empty", len(parsed["messages"]) == 1,
          str(len(parsed.get("messages", []))))

    print("4. pi shape — already canonical")
    pi_rec = {**base, "agent": "pi", "event": "llm_request", "model": "m",
              "request": {"messages": [{"role": "system", "content": SYS},
                                       {"role": "user", "content": "hi"}],
                          "tool_count": 4}}
    out = show(pi_rec)
    parsed = json.loads(strip_comments(request_block(out)))
    check("left alone, not double-wrapped", times(out, SYS) == 1,
          f"count={times(out, SYS)}")
    check("tool_count carried through", parsed.get("tool_count") == 4,
          str(parsed.get("tool_count")))
    check("two messages preserved", len(parsed["messages"]) == 2,
          str(len(parsed["messages"])))

    print("5. a response record gets no empty request block")
    resp_rec = {**base, "agent": "hermes", "event": "llm_response",
                "response": {"content": "the answer", "usage": {
                    "input_tokens": 10, "output_tokens": 2}}}
    out = show(resp_rec)
    check("no request block", "=== request ===" not in out, out[:300])
    check("meta still printed", "=== meta ===" in out, out[:200])
    check("response printed", "the answer" in out, out[:400])

    print("6. `user_prompt` records render their own request")
    up_rec = {**base, "agent": "hermes", "event": "user_prompt",
              "request": {"messages": [{"role": "user", "content": "do it"}],
                          "message_count": 1}}
    out = show(up_rec)
    blk = request_block(out)
    check("request block present", bool(blk), out[:200])
    parsed = json.loads(strip_comments(blk))
    check("user message rendered", parsed["messages"][0]["content"] == "do it",
          str(parsed)[:200])

    print("7. unknown roles are left unannotated, not mislabelled")
    odd_rec = {**base, "agent": "pi", "event": "llm_request",
               "request": {"messages": [{"role": "narrator", "content": "x"}]}}
    blk = request_block(show(odd_rec))
    check("no bogus annotation", "// ←" not in blk, blk)

    print("8. non-string / list-shaped request bodies survive")
    weird_rec = {**base, "agent": "pi", "event": "llm_request",
                 "request": [{"role": "user", "content": "list-shaped"}]}
    blk = request_block(show(weird_rec))
    check("list-shaped request rendered", "list-shaped" in blk, blk)

    if fails:
        print(f"\nFAILED: {', '.join(fails)}", file=sys.stderr)
        return 1
    print("PASS — `show` renders the classic {model, messages} shape for all "
          "three adapter layouts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
