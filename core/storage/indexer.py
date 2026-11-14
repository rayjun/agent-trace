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
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.convert.v1_to_v2 import EpisodeTracker, _digest, convert_record
from core.storage.discovery import iter_trace_files, resolved_path
from core.storage.sqlite_store import TraceStore

if TYPE_CHECKING:
    from core.workspace import GitSnapshot


@dataclass
class ScanReport:
    files_seen: int = 0
    files_ingested: int = 0
    files_invalidated: int = 0
    #: indexed files whose source is gone (deleted, not merely unreadable) —
    #: their rows are dropped in the same pass, because the projection follows
    #: the archive and a trace nobody can re-read must not stay queryable.
    files_dropped: int = 0
    #: Bytes the projection gave back to the filesystem, and what it holds now.
    #: A rebuild deletes every row before re-inserting it, so the file keeps the
    #: old pages until something compacts it.
    reclaimed_bytes: int = 0
    db_bytes: int = 0
    #: files whose stored key was respelled to the canonical (resolved) path —
    #: a one-time migration for keys written before paths were canonicalised.
    files_rekeyed: int = 0
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
        return (self.events_added > 0 or self.files_invalidated > 0
                or self.files_dropped > 0)

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


class _SourceChanged(OSError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"source changed while reading: {path}")
        self.path = path


class Indexer:
    def __init__(self, store: TraceStore, dirs: list[Path]) -> None:
        self.store = store
        self.dirs = dirs
        #: cwd -> workspace snapshot (or None when it is not a repository), so a
        #: session's hundreds of events cost one `git status`.
        self._workspaces: dict[str, GitSnapshot | None] = {}
        #: Per PASS: which human turn each session is inside, and the episode of
        #: every record already placed. Both span files on purpose — a session is
        #: split across daily files, and a backfill file repeats records the live
        #: one also holds.
        self._tracker = EpisodeTracker()
        self._seen: dict[str, str | None] = {}

    def _enrich(self, event: dict[str, Any]) -> dict[str, Any]:
        """Attach the workspace when the adapter did not record one.

        Recording it at capture time is better — it says what the agent SAW.
        Most traces on disk predate that, and for those a snapshot read now is
        still worth having, as long as it is labelled `at: index` so nobody
        compares a months-old trace against today's diff and believes it.

        The raw JSONL is not touched: this lands in the projection, which is
        derived data by definition.
        """
        resource = event.get("resource")
        cwd = (resource or {}).get("cwd") if isinstance(resource, dict) else None
        if not cwd or (isinstance(resource, dict) and resource.get("git")):
            return event
        if cwd not in self._workspaces:
            from core.workspace import snapshot
            self._workspaces[cwd] = snapshot(cwd, at="index")
        git = self._workspaces[cwd]
        if not git:
            self._workspaces[cwd] = None
            return event
        base_resource = resource if isinstance(resource, dict) else {}
        return {**event, "resource": {**base_resource, "git": git.to_dict()}}

    def scan_once(self) -> ScanReport:
        # BEGIN IMMEDIATE must precede seed acquisition AND cursor decisions:
        # the per-store RLock cannot exclude another store/process on this DB.
        # Tradeoff: even idle scans take the SQLite writer slot, and slow file
        # reads/workspace enrichment hold it for the whole pass (other writers
        # wait up to busy_timeout). WAL readers retain their committed snapshot.
        with self.store._write_lock:
            for _ in range(3):
                try:
                    with self.store.transaction():
                        report = self._scan_pass()
                    # Publish the seed/cursor unit before derived refresh. The
                    # durable dirty marker recovers a crash here; refresh itself
                    # takes a writer transaction and derives from current rows.
                    if report.changed or report.files_rekeyed:
                        try:
                            self.store.refresh_projections()
                        except sqlite3.Error as exc:
                            report.errors.append(f"refresh_projections: {exc}")
                    return report
                except _SourceChanged as exc:
                    # Nested transactions have no savepoints. Unwind the OUTER
                    # transaction before invalidating, then acquire fresh seeds.
                    self.store.invalidate_file(resolved_path(exc.path))
                except OSError as exc:
                    return ScanReport(errors=[str(exc)])
            return ScanReport(errors=["source repeatedly changed; retry next scan"])

    def _scan_pass(self) -> ScanReport:
        report = ScanReport()
        self.store.recover_projections()
        before = self._counters()
        files = list(iter_trace_files(self.dirs))
        present = {str(p) for p in files}
        report.files_rekeyed = self._rekey(present)
        report.files_dropped = self._drop_vanished(present)
        # Preflight all invalidations before choosing the replay start state.
        # invalidate_file clears seeds as well as rewinding every file cursor.
        for path in files:
            try:
                state = self.store.file_state(resolved_path(path))
                if state is not None and self._replaced(path.stat(), state):
                    self.store.invalidate_file(resolved_path(path))
                    report.files_invalidated += 1
            except OSError as exc:
                report.errors.append(f"{path}: {exc}")
        if self.store.replay_required():
            # The enclosing scan transaction also makes ordered replay atomic.
            self.store.prepare_replay([resolved_path(p) for p in files])
            self._scan_files(files, report, before)
            if report.errors:
                raise OSError("source replay incomplete: " + "; ".join(report.errors))
            self.store.finish_replay()
        else:
            self._scan_files(files, report, before)
        # A caught per-file error cannot commit partial nested transactions:
        # they defer commits but do not provide rollback savepoints.
        if report.errors:
            raise OSError("source scan incomplete: " + "; ".join(report.errors))
        return report

    def _scan_files(self, files: list[Path], report: ScanReport,
                    before: tuple[int, int, int, int]) -> None:
        self._tracker = EpisodeTracker(self.store.session_seeds())
        self._seen = {}
        for path in files:
            report.files_seen += 1
            try:
                self._scan_file(Path(path), report)
            except _SourceChanged:
                raise
            except OSError as exc:
                report.errors.append(f"{path}: {exc}")
        # Counters BEFORE the refresh decision: `report.changed` reads
        # events_added, which is a diff of the store's counters, so asking the
        # question first and computing the diffs after silently answered "nothing
        # changed" on every pass and the projection stopped updating entirely.
        self._report_counters(report, before)

    def _rekey(self, present: set[str]) -> int:
        """One-time: move keys written before paths were canonicalised.

        Only rows whose file is present under a different spelling are touched;
        a row for a genuinely missing file is left to _drop_vanished.
        """
        present_resolved = {resolved_path(p) for p in present}
        fixed = 0
        for path in self.store.indexed_files():
            canon = resolved_path(path)
            if canon != path and canon in present_resolved:
                self.store.rekey_file(path, canon)
                fixed += 1
        return fixed

    def _drop_vanished(self, present: set[str]) -> int:
        """Forget indexed files whose source no longer exists.

        The index is a projection of the files, so a file that is gone has no
        rows to project: without this, deleting a trace's JSONL left it
        queryable forever and unreachable from `--full` (which only re-reads
        files that exist).

        The parent directory is the guard: a *file* missing from a directory
        that is still there is a deletion, a whole directory missing is a
        detached volume or an unmounted backup drive, and wiping the index
        because a disk was unplugged would be the worse mistake.
        """
        gone = []
        # Compare resolved paths. The stored key comes from whatever the indexer
        # was given (`args.dirs` is resolved by the CLI), while a scan yields
        # paths spelled as the directory was written — on macOS `/var` is a
        # symlink to `/private/var`, so a raw string compare called a live file
        # deleted, dropped its rows, and the next scan re-ingested them: a
        # permanent drop/re-add loop instead of a no-op.
        present_resolved = {resolved_path(p) for p in present}
        for path in self.store.indexed_files():
            if resolved_path(path) in present_resolved:
                continue
            parent = Path(path).parent
            try:
                if parent.is_dir():
                    gone.append(path)
            except OSError:
                continue
        if not gone:
            return 0
        self.store.drop_files(gone)
        return len(gone)

    def full_rebuild(self) -> ScanReport:
        with self.store._write_lock:
            for _ in range(3):
                try:
                    # Clearing the archive, rebuilding seeds and reading files
                    # must exclude independent indexers just like a normal pass.
                    with self.store.transaction():
                        report = self._full_rebuild()
                    break
                except _SourceChanged:
                    # Nothing from this attempt survived the outer rollback.
                    continue
                except OSError as exc:
                    return ScanReport(errors=[str(exc)])
            else:
                return ScanReport(errors=["source repeatedly changed; retry next rebuild"])
            # VACUUM/checkpoint cannot run inside the rebuild transaction.
            prior, now = self.store.compact()
            report.reclaimed_bytes = max(0, prior - now)
            report.db_bytes = now
            return report

    def _full_rebuild(self) -> ScanReport:
        """Re-ingest everything from the JSONL files.

        The archive is CLEARED first. Without that this was not a rebuild at
        all: re-reading the files produced the same event ids, ingest dedups by
        id, and the stored payloads kept whatever an older converter had
        written — so a converter fix (a new attribute, a corrected field) never
        reached a trace that was already indexed. The JSONL files are the truth
        and they are still there, which is exactly why the archive may be
        dropped.
        """
        report = ScanReport()
        self.store.clear_archive()
        # A rebuild starts from nothing: the archive it just cleared described
        # the old ids, and the files are the truth.
        self._tracker = EpisodeTracker()
        self._seen = {}
        before = self._counters()
        for path in iter_trace_files(self.dirs):
            report.files_seen += 1
            try:
                self._ingest_from(Path(path), 0, report, full=True)
            except _SourceChanged:
                raise
            except OSError as exc:
                report.errors.append(f"{path}: {exc}")
        if report.errors:
            raise OSError("source rebuild incomplete: " + "; ".join(report.errors))
        self.store.rebuild_projections()
        # The archive now holds ids this build mints, so the rebuild marker goes.
        self.store.mark_rebuilt()
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
        key = resolved_path(path)   # one spelling per file, whatever dir was passed
        state = self.store.file_state(key)

        if state is not None and self._replaced(stat, state):
            raise _SourceChanged(path)
        if state is not None and stat.st_size == state["indexed_bytes"]:
            return  # unchanged

        offset = state["indexed_bytes"] if state is not None else 0
        self._ingest_from(path, offset, report, full=state is None)

    @staticmethod
    def _replaced(stat: os.stat_result, state: sqlite3.Row) -> bool:
        return bool(stat.st_size < state["size"]
                or (state["inode"] is not None and state["inode"] != stat.st_ino)
                or stat.st_mtime < (state["mtime"] or 0))

    def _ingest_from(self, path: Path, offset: int, report: ScanReport,
                     full: bool) -> None:
        seeds, initial_seen = self._tracker.snapshot(), self._seen.copy()
        try:
            with self.store.transaction():
                key = resolved_path(path)   # one spelling per file, whatever dir was passed
                corrupt = 0
                last_error: str | None = None
                lineno = 0
                # A normal tail continues the committed episode seed. A source
                # replay starts with empty seeds and visits files in discovery
                # order, sharing both tracker and digest map across those files.
                tracker = self._tracker
                seen = self._seen
                touched: dict[tuple[str, str], tuple[Any, ...]] = {}
                complete = offset
                with open(path, "rb") as fh:
                    opened = os.fstat(fh.fileno())
                    previous = self.store.file_state(key)
                    if (previous is not None and self._replaced(opened, previous)) or opened.st_size < offset:
                        raise _SourceChanged(path)
                    fh.seek(offset)
                    # Bound this read to the opening snapshot; later appends are next scan's work.
                    while fh.tell() < opened.st_size:
                        raw = fh.readline(opened.st_size - fh.tell())
                        if not raw.endswith(b"\n"):
                            break  # Preserve partial bytes on disk, including partial UTF-8.
                        complete = fh.tell()
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
                        episode = None
                        if rec.get("v") == 1:
                            digest = _digest(rec)
                            if digest in seen:
                                # The same record, already placed in this pass: reuse its
                                # episode so the duplicate gets the same event ids.
                                episode = seen[digest]
                            else:
                                episode = tracker.step(rec)
                                seen[digest] = episode
                            skey = (rec.get("agent") or "unknown",
                                    rec.get("session_id") or "")
                            st = tracker.snapshot().get(skey)
                            if st is not None:
                                touched[skey] = st
                        events = [self._enrich(e)
                                  for e in self._to_canonical(rec, episode)]
                        source_line: int | None = lineno if full else None
                        for event in events:
                            # Counters live on the store (it is what knows why an
                            # insert did not land); the report diffs them at the end.
                            self.store.ingest(event, source_file=key,
                                              source_line=source_line)
                # Persist once per file, not per record: the episode only has to be
                # correct for the NEXT scan's first record.
                for (adapter, session), state in touched.items():
                    self.store.note_session_seed(adapter, session, *state)
                stat = path.stat()
                if ((stat.st_dev, stat.st_ino) != (opened.st_dev, opened.st_ino)
                        or stat.st_size < opened.st_size or stat.st_mtime_ns < opened.st_mtime_ns
                        or (stat.st_size == opened.st_size and stat.st_mtime_ns != opened.st_mtime_ns)):
                    raise _SourceChanged(path)
                self.store.note_file(key, stat.st_size, stat.st_mtime, complete,
                                     corrupt_lines=corrupt, last_error=last_error,
                                     inode=getattr(stat, "st_ino", None))
                report.corrupt_lines += corrupt
                report.files_ingested += 1
        except BaseException:
            self._tracker = EpisodeTracker(seeds)
            self._seen = initial_seen
            raise

    @staticmethod
    def _to_canonical(rec: dict[str, Any],
                      episode: str | None = None) -> list[dict[str, Any]]:
        """v1 record -> converted event(s); v2 -> itself; else -> nothing."""
        v = rec.get("v")
        if v == 1:
            converted = convert_record(rec, episode)
            return [converted] if converted is not None else []
        if v == 2:
            return [rec]
        return []
