#!/usr/bin/env python3
"""`agenttrace doctor` and the ingest-fault log.

The checks are only worth having if they fail for the RIGHT reason, so each
case below builds a small broken or healthy world and asserts the verdict:

  * no trace dir / writable dir / corrupt line / stale records
  * an index that is behind the files
  * a deployed adapter that no longer matches the checkout
  * an event that could not be written (the fault log, and its throttle)
  * the JSON form, because every other surface consumes it

Hermetic by construction: HOME and AGENTTRACE_PLATFORM_DIR point into a temp
dir, no real trace dir is read, and the coverage scan is stubbed (it reads the
agent's own databases and is not this test's subject).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "common"))

import agenttrace_common as common  # noqa: E402
from core import doctor, ingest_faults  # noqa: E402

fails: list[str] = []


def check(name: str, cond: object, detail: object = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


def iso(offset_s: float = 0.0) -> str:
    from datetime import datetime, timedelta, timezone
    when = datetime.now(timezone.utc) + timedelta(seconds=offset_s)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def v1(event: str = "llm_request", ts: str | None = None, **kw) -> dict:
    rec = {"v": 1, "ts": ts or iso(), "agent": "hermes", "event": event,
           "session_id": "s1", "turn_id": "t1"}
    rec.update(kw)
    return rec


def write(path: Path, lines: list[dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write((json.dumps(line) if isinstance(line, dict) else line) + "\n")


tmp = Path(tempfile.mkdtemp(prefix="doctor-test-"))
home = tmp / "home"
home.mkdir()
os.environ["AGENTTRACE_PLATFORM_DIR"] = str(tmp / "platform")
os.environ["AGENTTRACE_REDACT"] = "0"          # keep fixtures byte-exact
os.environ.pop("AGENTTRACE_FAULTS", None)

#: A deployed adapter that matches the checkout, so the adapter check is ok
#: unless a case deliberately breaks it.
deployed = home / ".hermes" / "plugins" / "agent-trace"
deployed.mkdir(parents=True)
(deployed / "__init__.py").write_bytes((ROOT / "hermes" / "__init__.py").read_bytes())
(deployed / "plugin.yaml").write_bytes((ROOT / "hermes" / "plugin.yaml").read_bytes())
(deployed / "agenttrace_common.py").write_bytes(
    (ROOT / "common" / "agenttrace_common.py").read_bytes())

# --------------------------------------------------------------------------
# 1. nothing at all
# --------------------------------------------------------------------------
empty = tmp / "empty"
empty.mkdir()
r = doctor.run([empty], db_path=None, fast=True, home=home)
by_name = {c.name: c for c in r.checks}
check("no records → Capture warns", by_name["Capture"].status == "warn")
check("no records → not ok", not r.ok)
check("missing dir → Trace dirs fails",
      doctor.run([tmp / "nope"], db_path=None, fast=True, home=home)
      .checks[0].status == "fail")

# --------------------------------------------------------------------------
# 2. healthy: fresh v1 records, matching deployment
# --------------------------------------------------------------------------
good_dir = tmp / "good"
write(good_dir / "hermes-20261021.jsonl", [
    v1("session_start"), v1("llm_request", request={"messages": []}),
])
r = doctor.run([good_dir], db_path=None, fast=True, home=home)
by_name = {c.name: c for c in r.checks}
check("healthy capture → ok", by_name["Capture"].status == "ok",
      by_name["Capture"].summary)
check("healthy schema → v1 ok", by_name["Schema"].status == "ok")
check("matching deployment → adapters ok", by_name["Adapters"].status == "ok",
      by_name["Adapters"].summary)
check("no index → info, not a failure", by_name["Index"].status == "info")
check("healthy world exits 0", r.ok, str([(c.name, c.status) for c in r.checks]))

# 3. stale deployment
(deployed / "agenttrace_common.py").write_text("# stale\n", encoding="utf-8")
r = doctor.run([good_dir], db_path=None, fast=True, home=home)
adapters = {c.name: c for c in r.checks}["Adapters"]
check("stale deployed plugin → warn", adapters.status == "warn")
check("...and says how to fix it", "install.sh" in adapters.advice)
(deployed / "agenttrace_common.py").write_bytes(
    (ROOT / "common" / "agenttrace_common.py").read_bytes())

# --------------------------------------------------------------------------
# 3. corrupt line in the middle (crash / bad redaction) and stale records
# --------------------------------------------------------------------------
bad_dir = tmp / "bad"
write(bad_dir / "hermes-20261021.jsonl", [
    v1("session_start"), '{"v": 1, "ts": "broken', v1("llm_response"),
])
r = doctor.run([bad_dir], db_path=None, fast=True, home=home)
capture = {c.name: c for c in r.checks}["Capture"]
check("corrupt middle line → warn", capture.status == "warn")
check("...named as unparseable", "unparseable" in capture.advice)

old_dir = tmp / "old"
write(old_dir / "hermes-20261018.jsonl", [v1("session_end", ts=iso(-3 * 86400))])
r = doctor.run([old_dir], db_path=None, fast=True, home=home)
capture = {c.name: c for c in r.checks}["Capture"]
check("no events for 3 days → warn", capture.status == "warn")
check("...reports the silence", "no events" in capture.advice)

# --------------------------------------------------------------------------
# 4. unwritable trace dir
# --------------------------------------------------------------------------
ro = tmp / "ro"
write(ro / "hermes-20261021.jsonl", [v1()])
ro.chmod(0o500)
try:
    r = doctor.run([ro], db_path=None, fast=True, home=home)
    dirs_check = {c.name: c for c in r.checks}["Trace dirs"]
    if os.geteuid() == 0:
        print("  skip unwritable-dir check (running as root)")
    else:
        check("unwritable dir → fail", dirs_check.status == "fail",
              dirs_check.summary)
        check("...says writes are being dropped",
              "dropped" in dirs_check.advice or "writable" in dirs_check.advice)
finally:
    ro.chmod(0o700)

# --------------------------------------------------------------------------
# 5. index present, then behind
# --------------------------------------------------------------------------
from core.storage.indexer import Indexer  # noqa: E402
from core.storage.sqlite_store import TraceStore  # noqa: E402

idx_dir = tmp / "indexed"
write(idx_dir / "hermes-20261021.jsonl", [
    v1("session_start"), v1("llm_request", request_id="r1",
                            request={"messages": []}),
])
db = tmp / "agenttrace.db"
store = TraceStore(db)
Indexer(store, [idx_dir]).scan_once()
store.close()
r = doctor.run([idx_dir], db_path=db, fast=True, home=home)
index = {c.name: c for c in r.checks}["Index"]
check("fresh index → ok", index.status == "ok", index.summary)
check("...counts the events", "2 events" in index.summary, index.summary)

time.sleep(0.01)
with open(idx_dir / "hermes-20261021.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(v1("llm_response", request_id="r1")) + "\n")
r = doctor.run([idx_dir], db_path=db, fast=True, home=home)
index = {c.name: c for c in r.checks}["Index"]
check("a new line → index behind", index.status == "warn", index.summary)
check("...recommends reindexing", "agenttrace index" in index.advice)

# --------------------------------------------------------------------------
# 6. the fault log: write failure is recorded, and throttled
# --------------------------------------------------------------------------
common.record_fault.__globals__["_last_fault"].clear()
common.FAULT_WINDOW_S = 0.0
wrote = common.record_fault("hermes", "write", "OSError: 28 no space", "/tmp/x")
check("fault writer writes", wrote and ingest_faults.path().exists())
common.record_fault.__globals__["_last_fault"].clear()
common.FAULT_WINDOW_S = 60.0
first = common.record_fault("throttle-demo", "write", "OSError: 28 no space")
second = common.record_fault("throttle-demo", "write", "OSError: 28 no space")
check("fault writer throttles a burst", first and not second, f"{first}/{second}")
r = doctor.run([good_dir], db_path=None, fast=True, home=home)
faults = {c.name: c for c in r.checks}["Ingest faults"]
check("faults → Ingest faults warns", faults.status == "warn")
check("...names the adapter and kind", "/write" in faults.advice, faults.advice)

groups = ingest_faults.summarize()
check("summary groups by adapter+kind", any(g["adapter_id"] == "hermes"
                                            and g["kind"] == "write"
                                            for g in groups), groups)
check("summary reports lines, not fake event counts",
      all("lines" in g for g in groups))

# --------------------------------------------------------------------------
# 7. emit_record: a failing append leaves a fault, not silence
# --------------------------------------------------------------------------
failing = tmp / "failing"
failing.mkdir()
failing.chmod(0o500)
common.FAULT_WINDOW_S = 0.0
common.record_fault.__globals__["_last_fault"].clear()
before = len(ingest_faults.read())
try:
    out = common.emit_record(failing, "hermes", v1("llm_request"))
finally:
    failing.chmod(0o700)
common.FAULT_WINDOW_S = 60.0
if os.geteuid() == 0:
    print("  skip emit-fault check (running as root)")
else:
    check("unwritable append returns None", out is None)
    after = ingest_faults.read()
    check("...and records a write fault", len(after) == before + 1, len(after))
    check("...with the adapter id",
          after and after[-1].get("adapter_id") == "hermes" and
          after[-1].get("kind") == "write", after[-1] if after else None)

# --------------------------------------------------------------------------
# 8. the sink: a contract violation is a fault too
# --------------------------------------------------------------------------
from adapter_sdk.event_sink import JsonlEventSink  # noqa: E402

common.FAULT_WINDOW_S = 0.0
common.record_fault.__globals__["_last_fault"].clear()
sink = JsonlEventSink(tmp / "sink.jsonl", redact=False)
ok = sink.emit({"v": 2, "type": "span_start", "adapter_id": "demo",
                "event_id": "x" * 10})       # missing ts/span/trace
check("sink refuses an invalid event", ok is False and sink.write_errors == 1)
recs = ingest_faults.read()
check("...and says so in the fault log",
      recs and recs[-1].get("kind") == "contract"
      and recs[-1].get("adapter_id") == "demo", recs[-1:] or None)
common.FAULT_WINDOW_S = 60.0

# --------------------------------------------------------------------------
# 9. JSON form
# --------------------------------------------------------------------------
doctor._coverage_check = lambda: doctor.Check("Coverage", "ok", "stubbed")
r = doctor.run([good_dir], db_path=None, fast=False, home=home)
payload = json.loads(doctor.render(r, as_json=True))
check("json has every check", {c["name"] for c in payload["checks"]} >=
      {"Trace dirs", "Disk", "Capture", "Schema", "Ingest faults", "Index",
       "Adapters", "Coverage"})
check("json marks the overall verdict", payload["ok"] is False)
check("coverage stub ran", any(c["name"] == "Coverage" for c in payload["checks"]))
text = doctor.render(r)
check("text rendering names the warnings", "Warnings:" in text or
      "No problems found." in text)

print()
if fails:
    print(f"{len(fails)} FAILURES", file=sys.stderr)
    sys.exit(1)
print("doctor + ingest faults OK")
