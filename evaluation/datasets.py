"""Dataset CRUD + trace import + JSONL interchange.

Boundary (proposal §7.1): an example stores inputs / expected / metadata —
never a wholesale copy of the source trace. `from_trace` extracts the task
text and pointers; the trace itself stays in the events archive, referenced
by id, so privacy policy is enforced in exactly one place.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from core.query import get_trace
from core.schema.model import now_iso
from core.storage.sqlite_store import TraceStore
from evaluation.model import Example


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def create_dataset(store: TraceStore, name: str,
                   description: str = "") -> dict[str, Any]:
    dataset_id = _uid()
    with store._write_lock:
        store._conn().execute(
            "INSERT INTO datasets (dataset_id, name, description, version,"
            " created_at) VALUES (?,?,?,1,?)",
            (dataset_id, name, description, now_iso()))
        store._conn().commit()
    return get_dataset(store, dataset_id)  # type: ignore[return-value]


def list_datasets(store: TraceStore) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT d.*, COUNT(e.example_id) AS examples"
        " FROM datasets d LEFT JOIN dataset_examples e"
        "   ON e.dataset_id = d.dataset_id"
        " GROUP BY d.dataset_id ORDER BY d.created_at DESC")
    return [dict(r) for r in rows]


def get_dataset(store: TraceStore, dataset_id: str) -> dict[str, Any] | None:
    row = store.one("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,))
    if row is None:
        return None
    examples = store.query(
        "SELECT * FROM dataset_examples WHERE dataset_id = ?"
        " ORDER BY created_at", (dataset_id,))
    return {**dict(row),
            "examples": [_row_to_example(e).__dict__ | {"metadata": _j(e["metadata"])}
                         for e in examples]}


def add_example(store: TraceStore, dataset_id: str, inputs: dict[str, Any],
                expected: dict[str, Any] | None = None,
                metadata: dict[str, Any] | None = None) -> Example:
    example = Example(example_id=_uid(), dataset_id=dataset_id, inputs=inputs,
                      expected=expected, metadata=metadata or {})
    with store._write_lock:
        conn = store._conn()
        conn.execute(
            "INSERT INTO dataset_examples"
            " (example_id, dataset_id, inputs, expected, metadata, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (example.example_id, dataset_id, json.dumps(inputs, ensure_ascii=False),
             json.dumps(expected, ensure_ascii=False) if expected is not None else None,
             json.dumps(metadata or {}, ensure_ascii=False), now_iso()))
        conn.execute("UPDATE datasets SET version = version + 1"
                     " WHERE dataset_id = ?", (dataset_id,))
        conn.commit()
    return example


def from_trace(store: TraceStore, dataset_id: str, trace_id: str,
               expected: dict[str, Any] | None = None) -> Example | None:
    """Turn a recorded execution into a test sample: task text + pointers.

    Deliberately extracts ONLY the user task (first user_prompt, else the
    task span's input) — the full prompt history and tool outputs stay in
    the archive where the redaction policy already applies.
    """
    trace = get_trace(store, trace_id)
    if trace is None:
        return None
    task = ""
    for e in trace["events"]:
        if e.get("name") == "user_prompt":
            task = str((e.get("attributes") or {}).get("text") or "")
            break
    inputs: dict[str, Any] = {"task": task}
    metadata = {
        "source_trace_id": trace_id,
        "adapter_id": trace["adapter_id"],
        "captured_at": trace["start"],
    }
    models = [s.get("model") for s in trace["spans"] if s.get("model")]
    if models:
        metadata["model"] = models[0]
    return add_example(store, dataset_id, inputs, expected, metadata)


def export_jsonl(store: TraceStore, dataset_id: str) -> str:
    ds = get_dataset(store, dataset_id)
    if ds is None:
        return ""
    lines = [json.dumps({"type": "dataset", "name": ds["name"],
                         "description": ds["description"],
                         "version": ds["version"]}, ensure_ascii=False)]
    for ex in ds["examples"]:
        lines.append(json.dumps(
            {"type": "example", "inputs": _j(ex["inputs"]) if isinstance(ex["inputs"], str) else ex["inputs"],
             "expected": _j(ex["expected"]) if isinstance(ex["expected"], str) else ex["expected"],
             "metadata": ex["metadata"]}, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def import_jsonl(store: TraceStore, path_or_text: str,
                 dataset_id: str | None = None) -> dict[str, Any]:
    """Import a JSONL dataset. With dataset_id, append to it; otherwise the
    file's dataset header names a new dataset."""
    text = (Path(path_or_text).read_text(encoding="utf-8")
            if Path(path_or_text).is_file() else path_or_text)
    target = dataset_id
    imported = skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if rec.get("type") == "dataset" and target is None:
            target = create_dataset(store, rec.get("name") or "imported",
                                    rec.get("description") or "")["dataset_id"]
        elif rec.get("type") == "example":
            if target is None:
                target = create_dataset(store, "imported")["dataset_id"]
            inputs = rec.get("inputs")
            if not isinstance(inputs, dict) or not inputs:
                skipped += 1
                continue
            add_example(store, target, inputs, rec.get("expected"),
                        rec.get("metadata") or {})
            imported += 1
    if target is None:
        target = create_dataset(store, "imported")["dataset_id"]
    return {"dataset_id": target, "imported": imported, "skipped": skipped}


def _row_to_example(row: Any) -> Example:
    return Example(
        example_id=row["example_id"], dataset_id=row["dataset_id"],
        inputs=_j(row["inputs"]) or {},
        expected=_j(row["expected"]),
        metadata={})


def _j(raw: Any) -> Any:
    if raw is None or isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
