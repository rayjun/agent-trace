#!/usr/bin/env python3
"""Redaction must never change the record's structure.

The bug this pins: v1 redaction ran on the finished JSON LINE. A redactor is a
text transformer, so when a secret pattern ended inside a JSON escape
(`... Bearer abc123\\"`), the replacement consumed the closing backslash — the
line became invalid JSON and every reader silently skipped it. Five real
`llm_request` records were lost that way while the agent kept writing.

The fix redacts the record's values BEFORE json.dumps. What must stay true:

  * the emitted line always parses, whatever the redactor does to the text
  * the secret is gone
  * the rest of the record survives (no field swallowed)
  * AGENTTRACE_REDACT=0 still means "write what I gave you"
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "common"))

import agenttrace_common as common  # noqa: E402

fails: list[str] = []


def check(name: str, cond: object, detail: object = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


def host_redactor(text: str) -> str:
    """The shape that broke: the match swallows a trailing backslash-quote.

    Hermes' own `redact_sensitive_text` behaves this way on a serialised line
    (`Bearer <token>\\"` -> `Bearer ***`), which is why the line-level path was
    unsafe rather than merely inelegant.
    """
    return re.sub(r"(?i)bearer\s+\S+", "Bearer ***", text)


TMP = Path(tempfile.mkdtemp(prefix="redact-test-"))
os.environ["AGENTTRACE_PLATFORM_DIR"] = str(TMP / "platform")
os.environ.pop("AGENTTRACE_REDACT", None)
os.environ.pop("AGENTTRACE_FAULTS", None)

REC = {
    "v": 1,
    "ts": "2026-10-21T10:00:00.000Z",
    "agent": "hermes",
    "event": "tool_call",
    "session_id": "s1",
    "tool": {"name": "terminal",
             "args": {"cmd": 'curl -H \\"Authorization: Bearer abc123def456\\"'}},
}

# --------------------------------------------------------------------------
# 1. the mechanism, stated as a test
# --------------------------------------------------------------------------
line_level = host_redactor(json.dumps(REC, ensure_ascii=False))
try:
    json.loads(line_level)
    broke = False
except ValueError:
    broke = True
check("line-level redaction can corrupt JSON (the bug)", broke)

value_level = json.dumps(common.redact_record(REC, host_redactor),
                         ensure_ascii=False)
try:
    parsed = json.loads(value_level)
    valid = True
except ValueError:
    parsed, valid = None, False
check("value-level redaction keeps the record parseable", valid)
check("the secret is gone", "abc123def456" not in value_level)
check("the rest of the record survives",
      valid and parsed["tool"]["name"] == "terminal"
      and parsed["session_id"] == "s1", parsed)


# --------------------------------------------------------------------------
# 2. end to end through emit_record
# --------------------------------------------------------------------------
traces = TMP / "traces"
path = common.emit_record(traces, "hermes", REC, extra_redact=host_redactor)
check("emit_record wrote a file", path is not None and path.exists())
raw = path.read_text(encoding="utf-8").splitlines() if path else []
check("exactly one line", len(raw) == 1, len(raw))
try:
    written = json.loads(raw[0])
    ok = True
except (ValueError, IndexError):
    written, ok = None, False
check("the written line parses", ok, raw[:1])
check("no token on disk", raw and "abc123def456" not in raw[0])
check("the tool call is still identifiable",
      ok and written["tool"]["args"]["cmd"].startswith("curl -H"), written)

# --------------------------------------------------------------------------
# 3. the built-in pattern, nested in values
# --------------------------------------------------------------------------
nested = {"v": 1, "ts": "2026-10-21T10:00:00.000Z", "agent": "hermes",
          "event": "llm_request", "session_id": "s2",
          "request": {"messages": [
              {"role": "user", "content": [
                  {"type": "text", "text": "key sk-abcdefghijklmnopqrstuvwx"}]}]},
          "meta": {"note": "sk-zyxwvutsrqponmlkjihgfedc"}}
out = common.redact_record(nested)
blob = json.dumps(out)
check("sk- inside a nested list is redacted",
      "sk-abcdefghijklmnopqrstuvwx" not in blob, blob[:200])
check("sk- inside a nested dict is redacted",
      "sk-zyxwvutsrqponmlkjihgfedc" not in blob)

# --------------------------------------------------------------------------
# 4. opting out still works, and still yields JSON
# --------------------------------------------------------------------------
os.environ["AGENTTRACE_REDACT"] = "0"
try:
    path2 = common.emit_record(TMP / "traces2", "hermes", REC,
                               extra_redact=host_redactor)
    text2 = path2.read_text(encoding="utf-8") if path2 else ""
    check("AGENTTRACE_REDACT=0 writes the original text",
          "abc123def456" in text2)
    check("...and it is still valid JSON", bool(text2) and
          json.loads(text2.splitlines()[0])["event"] == "tool_call")
finally:
    os.environ.pop("AGENTTRACE_REDACT", None)

# --------------------------------------------------------------------------
# 5. a redactor that raises must not lose the field or the record
# --------------------------------------------------------------------------
def angry(_text: str) -> str:
    raise RuntimeError("host redactor exploded")


rec3 = common.redact_record(REC, angry)
line3 = json.dumps(rec3)
check("a failing host redactor degrades to the built-in pass",
      json.loads(line3)["tool"]["name"] == "terminal")

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("redaction integrity OK")
