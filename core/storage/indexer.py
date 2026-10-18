"""The index-time pipeline: JSONL tail → validate → dedup → materialize.

This is the collector's second half (capture-time lives in the adapters;
see docs/decisions.md D1). It runs as a thread inside `agenttrace serve`,
or one-shot via `agenttrace index`.

Per scan:

* discover *.jsonl under the trace dirs
* unchanged (size == indexed_bytes) → skip
* grew → parse only the new bytes (append-only, same rule as trace_index)
* shrank → invalidate_file (delete its events, rebuild projections, re-ingest)
* each line: v1 → convert_record; v2 → ingest as-is; corrupt → quarantine count
* one refresh_projections() at the end (synthetic tasks + sessions)

Line numbers (`source.line`) are only recorded on a full-file ingest: for a
tail read we know the byte offset but not the line count without re-reading
the file, and honesty about provenance beats a guessed number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.convert.v1_to_v2 import convert_record
from core.storage.discovery import iter_trace_files
from core.storage.sqlite_store import TraceStore


@dataclass
class ScanReport:
    files_seen: int = 0
    files_ingested: int = 0
    files_invalidated: int = 0
    events_added: int = 0
    #: already present with the same payload — an idempotent re-read, i.e. the
    #: dedup working. NOT an error, and no longer counted as one.
    events_deduped: int = 0
    #: same event_id, DIFFERENT payload: an adapter identity bug.
    event_id_conflicts: int = 0
    #: failed the canonical contract: an adapter bug.
    events_invalid: int = 0
    corrupt_lines: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.events_added > 0 or self.files_invalidated > 0

    @property
    def problems(self) -> list[str]:
        """Things that indicate an adapter bug, as opposed to routine dedup."""
        out = []
        if self.event_id_conflicts:
            out.append(f"{self.event_id_conflicts} event_id(s) reused with a "
                       "different payload")
        if self.events_invalid:
            out.append(f"{self.events_invalid} event(s) failed the contract")
        if self.corrupt_lines:
            out.append(f"{self.corrupt_lines} unparseable line(s)")
        return out


class Indexer:
    def __init__(self, store: TraceStore, dirs: list[Path]) -> None:
        self.store = store
        self.dirs = dirs

    def scan_once(self) -> ScanReport:
        report = ScanReport()
        before = self._counters()
        files = list(iter_trace_files(self.dirs))
        for path in files:
            report.files_seen += 1
            try:
                self._scan_file(Path(path), report)
            except OSError as exc:
                report.errors.append(f"{path}: {exc}")
        # A shrink rewinds EVERY file's offset (see invalidate_file), but files
        # already visited earlier in this pass were skipped as "unchanged"
        # before the rewind happened — so a deduped event could stay missing
        # until the next scan. One extra pass over the file set closes that
        # window; event_id dedup keeps it idempotent.
        if report.files_invalidated:
            for path in files:
                try:
                    self._scan_file(Path(path), report)
                except OSError as exc:
                    report.errors.append(f"{path}: {exc}")
        self.store.refresh_projections()
        self._report_counters(report, before)
        return report

    def full_rebuild(self) -> ScanReport:
        """Re-ingest everything from scratch (recovery / first run)."""
        report = ScanReport()
        before = self._counters()
        for path in iter_trace_files(self.dirs):
            report.files_seen += 1
            try:
                self._ingest_from(Path(path), 0, report, full=True)
            except OSError as exc:
                report.errors.append(f"{path}: {exc}")
        self.store.rebuild_projections()
        self._report_counters(report, before)
        return report

    def _counters(self) -> tuple[int, int, int, int]:
        store = self.store
        return (store.added, store.duplicates, store.duplicate_conflicts,
                store.invalid)

    def _report_counters(self, report: ScanReport,
                         before: tuple[int, int, int, int]) -> None:
        after = self._counters()
        report.events_added = after[0] - before[0]
        report.events_deduped = after[1] - before[1]
        report.event_id_conflicts = after[2] - before[2]
        report.events_invalid = after[3] - before[3]

    def _scan_file(self, path: Path, report: ScanReport) -> None:
        stat = path.stat()
        key = str(path)
        state = self.store.file_state(key)

        # Compare against the file's last OBSERVED size, not `indexed_bytes`:
        # invalidate_file rewinds every file's offset to 0, so a file whose
        # own shrink happens later in the same pass would compare its new size
        # against 0, look "unchanged", and keep its stale events forever.
        baseline = state["size"] if state is not None else 0
        replaced = False
        if state is not None:
            # A file swapped for a DIFFERENT one is not an append, even when it
            # is bigger: the stored offset then lands in the middle of a record
            # and the tail read ingests fragments. Size alone could not see it.
            if "inode" in state.keys() and state["inode"] is not None \
                    and getattr(stat, "st_ino", None) is not None \
                    and state["inode"] != stat.st_ino:
                replaced = True
            elif stat.st_mtime < (state["mtime"] or 0):
                replaced = True      # restored from a backup
        if state is not None and (stat.st_size < baseline or replaced):
            self.store.invalidate_file(key)
            report.files_invalidated += 1
            state = None
        if state is not None and stat.st_size == state["indexed_bytes"]:
            return  # unchanged

        offset = state["indexed_bytes"] if state is not None else 0
        self._ingest_from(path, offset, report, full=state is None)

    def _ingest_from(self, path: Path, offset: int, report: ScanReport,
                     full: bool) -> None:
        key = str(path)
        corrupt = 0
        last_error: str | None = None
        lineno = 0
        with open(path, "rb") as fh:
            fh.seek(offset)
            for raw in fh:
                lineno += 1
                try:
                    line = raw.decode("utf-8", "replace").strip()
                except UnicodeDecodeError:  # pragma: no cover - decode(replace) cannot raise
                    continue
                if not line:
                    continue
                try:
                    rec: Any = json.loads(line)
                except json.JSONDecodeError:
                    corrupt += 1
                    last_error = f"line not JSON: {line[:120]}"
                    continue
                if not isinstance(rec, dict):
                    corrupt += 1
                    last_error = "line is not a JSON object"
                    continue
                events = self._to_canonical(rec)
                source_line: int | None = lineno if full else None
                for event in events:
                    # Counters live on the store (it is what knows why an
                    # insert did not land); the report diffs them at the end.
                    self.store.ingest(event, source_file=key,
                                      source_line=source_line)
        stat = path.stat()
        self.store.note_file(key, stat.st_size, stat.st_mtime, stat.st_size,
                             corrupt_lines=corrupt, last_error=last_error,
                             inode=getattr(stat, "st_ino", None))
        report.corrupt_lines += corrupt
        report.files_ingested += 1

    @staticmethod
    def _to_canonical(rec: dict[str, Any]) -> list[dict[str, Any]]:
        """v1 record -> converted event(s); v2 -> itself; else -> nothing."""
        v = rec.get("v")
        if v == 1:
            converted = convert_record(rec)
            return [converted] if converted is not None else []
        if v == 2:
            return [rec]
        return []
