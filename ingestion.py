from __future__ import annotations

import csv
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

import paramiko
from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db import CdrRecord, ImportCursor, ImportJob, SourceFileState

_COMMIT_CHUNK = 250
_RECENT_FILES_TO_RECHECK = 3
_MAX_FILES_PER_RUN = int(os.getenv("SMSC_MAX_FILES_PER_RUN", "500"))
_FILE_OK_RATIO = float(os.getenv("SMSC_FILE_OK_RATIO", "0.95"))
_BOOTSTRAP_YEAR = int(os.getenv("SMSC_BOOTSTRAP_YEAR", "2026"))
_BOOTSTRAP_MONTH = int(os.getenv("SMSC_BOOTSTRAP_MONTH", "5"))
# Safety net: unknown files with unparseable names but recent mtime (days before cursor)
_UNPARSEABLE_MTIME_BACKFILL_DAYS = int(os.getenv("SMSC_UNPARSEABLE_BACKFILL_DAYS", "3"))

import_logger = logging.getLogger("smsc_import")

ProgressCallback = Callable[[str, int, int, str], None]


@dataclass
class SourceSpec:
    name: str
    mode: str  # local | sftp
    path: str
    host: str = ""
    username: str = ""
    password: str = ""
    port: int = 22


@dataclass
class SourceFileEntry:
    file_name: str
    mtime: int
    size: int
    local_path: Path | None = None
    remote_path: str = ""


@dataclass
class ImportOptions:
    backfill_from: date | None = None
    backfill_to: date | None = None
    on_progress: ProgressCallback | None = None


@dataclass
class SelectionMeta:
    scan_mode: str
    recheck_count: int = 0
    new_count: int = 0
    skipped_unchanged: int = 0
    min_file_dt: datetime | None = None
    max_file_dt: datetime | None = None
    cursor_dt: datetime | None = None
    new_by_day: dict[str, int] = field(default_factory=dict)
    truncated: bool = False

    def summary_dict(self) -> dict:
        return {
            "scan_mode": self.scan_mode,
            "recheck": self.recheck_count,
            "new": self.new_count,
            "skipped_unchanged": self.skipped_unchanged,
            "min_file_date": self.min_file_dt.isoformat() if self.min_file_dt else None,
            "max_file_date": self.max_file_dt.isoformat() if self.max_file_dt else None,
            "cursor_date": self.cursor_dt.isoformat() if self.cursor_dt else None,
            "new_by_day": self.new_by_day,
            "truncated": self.truncated,
        }

    def format_details(self, selected_count: int) -> str:
        base = (
            f"{self.scan_mode}; selected={selected_count};"
            f" recheck={self.recheck_count}; new={self.new_count};"
            f" skipped_unchanged={self.skipped_unchanged}"
        )
        if self.min_file_dt and self.max_file_dt:
            base += (
                f"; min={self.min_file_dt.date().isoformat()};"
                f" max={self.max_file_dt.date().isoformat()}"
            )
        if self.cursor_dt:
            base += f"; cursor={self.cursor_dt.date().isoformat()}"
        if self.new_by_day:
            days = ",".join(f"{k}:{v}" for k, v in sorted(self.new_by_day.items()))
            base += f"; new_by_day={days}"
        if self.truncated:
            base += f"; truncated_at={_MAX_FILES_PER_RUN}"
        return base


_FILE_DT_RE = re.compile(r"(20\d{2})_(\d{2})_(\d{2})[-_](\d{2})_(\d{2})_(\d{2})")


def _file_dt(entry: SourceFileEntry) -> datetime | None:
    match = _FILE_DT_RE.search(entry.file_name)
    if not match:
        return None
    try:
        y, m, d, hh, mm, ss = [int(v) for v in match.groups()]
        return datetime(y, m, d, hh, mm, ss)
    except ValueError:
        return None


def _entry_sort_dt(entry: SourceFileEntry) -> datetime:
    return _file_dt(entry) or datetime.fromtimestamp(entry.mtime)


def _file_calendar_date(entry: SourceFileEntry) -> date | None:
    dt = _file_dt(entry)
    return dt.date() if dt else None


def parse_cdr_line(row: list[str]) -> dict[str, str] | None:
    if len(row) < 9:
        return None
    return {
        "event_time": row[0].strip(),
        "cdr_id": row[1].strip(),
        "incoming_connection": row[2].strip(),
        "outgoing_route": row[3].strip(),
        "a_msisdn": row[4].strip(),
        "a_msisdn_translated": row[5].strip(),
        "b_msisdn": row[6].strip(),
        "b_msisdn_translated": row[7].strip(),
        "status": row[8].strip(),
    }


def list_local_files(path: str) -> list[SourceFileEntry]:
    base = Path(path)
    if not base.exists():
        return []
    out: list[SourceFileEntry] = []
    for file_path in [p for p in base.iterdir() if p.is_file()]:
        stat = file_path.stat()
        out.append(
            SourceFileEntry(
                file_name=file_path.name,
                mtime=int(stat.st_mtime),
                size=int(stat.st_size),
                local_path=file_path,
            )
        )
    out.sort(key=lambda f: (_entry_sort_dt(f), f.file_name))
    return out


def _sftp_list_entries(sftp: paramiko.SFTPClient, spec: SourceSpec) -> list[SourceFileEntry]:
    attrs = [a for a in sftp.listdir_attr(spec.path) if a.filename]
    out: list[SourceFileEntry] = []
    for attr in attrs:
        remote_path = f"{spec.path.rstrip('/')}/{attr.filename}"
        out.append(
            SourceFileEntry(
                file_name=attr.filename,
                mtime=int(attr.st_mtime),
                size=int(attr.st_size),
                remote_path=remote_path,
            )
        )
    out.sort(key=lambda f: (_entry_sort_dt(f), f.file_name))
    return out


def list_sftp_files(spec: SourceSpec) -> list[SourceFileEntry]:
    transport = paramiko.Transport((spec.host, spec.port))
    transport.connect(username=spec.username, password=spec.password)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
        return _sftp_list_entries(sftp, spec)
    finally:
        sftp.close()
        transport.close()


def iterate_local_selected(files: list[SourceFileEntry]) -> Iterable[tuple[SourceFileEntry, Iterable[list[str]]]]:
    for entry in files:
        if not entry.local_path:
            continue

        def row_iter(current: Path = entry.local_path) -> Iterable[list[str]]:
            with current.open("r", encoding="utf-8", errors="ignore") as fh:
                reader = csv.reader(fh)
                for row in reader:
                    if not row or row[0].startswith("date"):
                        continue
                    yield row

        yield entry, row_iter()


def iterate_sftp_selected(
    spec: SourceSpec, files: list[SourceFileEntry], sftp: paramiko.SFTPClient | None = None
) -> Iterable[tuple[SourceFileEntry, Iterable[list[str]]]]:
    if not files:
        return

    own_transport = None
    if sftp is None:
        own_transport = paramiko.Transport((spec.host, spec.port))
        own_transport.connect(username=spec.username, password=spec.password)
        sftp = paramiko.SFTPClient.from_transport(own_transport)

    try:
        for entry in files:
            if not entry.remote_path:
                continue

            def row_iter(current_remote: str = entry.remote_path) -> Iterable[list[str]]:
                with sftp.open(current_remote, "rb") as fh:
                    for raw in fh:
                        if isinstance(raw, bytes):
                            line = raw.decode("utf-8", errors="ignore").strip()
                        else:
                            line = str(raw).strip()
                        if not line or line.startswith("date"):
                            continue
                        yield next(csv.reader([line]))

            yield entry, row_iter()
    finally:
        if own_transport is not None:
            sftp.close()
            own_transport.close()


def _flush_cdr_chunk(db_session: Session, chunk: list[CdrRecord]) -> tuple[int, int]:
    if not chunk:
        return 0, 0
    db_session.add_all(chunk)
    try:
        db_session.commit()
        return len(chunk), 0
    except IntegrityError:
        db_session.rollback()
        inserted = 0
        skipped = 0
        for rec in chunk:
            db_session.add(rec)
            try:
                db_session.commit()
                inserted += 1
            except IntegrityError:
                db_session.rollback()
                skipped += 1
                import_logger.debug(
                    "duplicate skipped source=%s cdr_id=%s file=%s",
                    rec.source_server,
                    rec.cdr_id,
                    rec.source_file,
                )
        return inserted, skipped


def _known_files_for_source(db_session: Session, source_name: str) -> set[str]:
    names = set(
        db_session.scalars(
            select(SourceFileState.file_name).where(SourceFileState.source_server == source_name)
        )
    )
    if names:
        return names
    return {
        n
        for n in db_session.scalars(
            select(CdrRecord.source_file)
            .where(CdrRecord.source_server == source_name, CdrRecord.source_file != "")
            .distinct()
        )
        if n
    }


def _file_states_map(db_session: Session, source_name: str) -> dict[str, SourceFileState]:
    states = db_session.scalars(
        select(SourceFileState).where(SourceFileState.source_server == source_name)
    )
    return {s.file_name: s for s in states}


def _get_or_create_cursor(db_session: Session, source_name: str) -> ImportCursor:
    cursor = db_session.get(ImportCursor, source_name)
    if cursor is None:
        cursor = ImportCursor(source_server=source_name)
        db_session.add(cursor)
        db_session.commit()
    return cursor


def _cursor_dt_from_states(states: dict[str, SourceFileState]) -> datetime | None:
    dts: list[datetime] = []
    for state in states.values():
        dt = _file_dt(SourceFileEntry(file_name=state.file_name, mtime=state.last_mtime, size=state.last_size))
        if dt:
            dts.append(dt)
    return max(dts) if dts else None


def _file_unchanged(entry: SourceFileEntry, state: SourceFileState | None) -> bool:
    if state is None:
        return False
    return int(state.last_mtime) == int(entry.mtime) and int(state.last_size) == int(entry.size)


def _count_new_by_day(files: list[SourceFileEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in files:
        cal = _file_calendar_date(f)
        key = cal.isoformat() if cal else "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _apply_selection_meta(meta: SelectionMeta, files: list[SourceFileEntry], new_only: list[SourceFileEntry]) -> None:
    if not files:
        return
    dts = [_entry_sort_dt(f) for f in files]
    meta.min_file_dt = min(dts)
    meta.max_file_dt = max(dts)
    meta.new_by_day = _count_new_by_day(new_only)


def _merge_unique(
    *groups: list[SourceFileEntry], limit: int
) -> tuple[list[SourceFileEntry], bool]:
    selected: list[SourceFileEntry] = []
    seen: set[str] = set()
    truncated = False
    for group in groups:
        for entry in group:
            if entry.file_name in seen:
                continue
            if len(selected) >= limit:
                truncated = True
                return selected, truncated
            selected.append(entry)
            seen.add(entry.file_name)
    return selected, truncated


def _phased_bootstrap_selection(
    all_files: list[SourceFileEntry],
    known_files: set[str],
    cursor: ImportCursor,
    limit: int,
) -> tuple[list[SourceFileEntry], SelectionMeta]:
    year = cursor.bootstrap_year or _BOOTSTRAP_YEAR
    month = cursor.bootstrap_month or _BOOTSTRAP_MONTH
    start_day = (cursor.bootstrap_day or 0) + 1

    month_files = [
        f
        for f in all_files
        if (dt := _file_dt(f)) is not None and dt.year == year and dt.month == month
    ]

    meta = SelectionMeta(scan_mode=f"bootstrap_{year}_{month:02d}_pending")
    for day in range(start_day, 32):
        day_files = [f for f in month_files if _file_dt(f) is not None and _file_dt(f).day == day]
        if not day_files:
            continue
        unknown = [f for f in day_files if f.file_name not in known_files]
        if not unknown:
            continue
        unknown.sort(key=lambda f: (_entry_sort_dt(f), f.file_name))
        picked = unknown[:limit]
        meta.scan_mode = f"bootstrap_day_{year}_{month:02d}_{day:02d}"
        meta.new_count = len(picked)
        _apply_selection_meta(meta, picked, picked)
        return picked, meta

    meta.scan_mode = f"bootstrap_{year}_{month:02d}_complete"
    return [], meta


def _select_files_for_import(
    db_session: Session,
    source_name: str,
    all_files: list[SourceFileEntry],
    known_files: set[str],
    states: dict[str, SourceFileState],
    options: ImportOptions | None = None,
) -> tuple[list[SourceFileEntry], SelectionMeta]:
    options = options or ImportOptions()
    meta = SelectionMeta(scan_mode="unknown")

    if not all_files:
        meta.scan_mode = "no_files_found"
        return [], meta

    if options.backfill_from and options.backfill_to:
        backfill_files: list[SourceFileEntry] = []
        skipped_unchanged = 0
        for f in all_files:
            cal = _file_calendar_date(f)
            if cal is None:
                mtime_dt = datetime.fromtimestamp(f.mtime).date()
                if not (options.backfill_from <= mtime_dt <= options.backfill_to):
                    continue
            elif not (options.backfill_from <= cal <= options.backfill_to):
                continue
            if _file_unchanged(f, states.get(f.file_name)):
                skipped_unchanged += 1
                continue
            backfill_files.append(f)
        backfill_files.sort(key=lambda f: (_entry_sort_dt(f), f.file_name))
        selected, truncated = _merge_unique(backfill_files, limit=_MAX_FILES_PER_RUN)
        meta.scan_mode = (
            f"backfill_{options.backfill_from.isoformat()}_{options.backfill_to.isoformat()}"
        )
        meta.new_count = len(selected)
        meta.skipped_unchanged = skipped_unchanged
        meta.truncated = truncated
        _apply_selection_meta(meta, selected, selected)
        return selected, meta

    state_count = len(states)
    cursor = _get_or_create_cursor(db_session, source_name)

    if state_count == 0 and not known_files:
        selected, meta = _phased_bootstrap_selection(all_files, known_files, cursor, _MAX_FILES_PER_RUN)
        if selected:
            meta.cursor_dt = None
        return selected, meta

    cursor_dt = cursor.last_file_dt or _cursor_dt_from_states(states)
    meta.cursor_dt = cursor_dt

    known_candidates = [f for f in all_files if f.file_name in known_files]
    known_candidates.sort(key=lambda f: (_entry_sort_dt(f), f.file_name), reverse=True)

    recheck: list[SourceFileEntry] = []
    recheck_seen: set[str] = set()
    for f in known_candidates[:_RECENT_FILES_TO_RECHECK]:
        if f.file_name in recheck_seen:
            continue
        if _file_unchanged(f, states.get(f.file_name)):
            meta.skipped_unchanged += 1
            continue
        recheck.append(f)
        recheck_seen.add(f.file_name)

    for f in known_candidates:
        if f.file_name in recheck_seen:
            continue
        if not _file_unchanged(f, states.get(f.file_name)):
            recheck.append(f)
            recheck_seen.add(f.file_name)

    recheck.sort(key=lambda f: (_entry_sort_dt(f), f.file_name))
    meta.recheck_count = len(recheck)

    new_gap: list[SourceFileEntry] = []
    new_forward: list[SourceFileEntry] = []
    mtime_floor = (
        cursor_dt - timedelta(days=_UNPARSEABLE_MTIME_BACKFILL_DAYS) if cursor_dt else None
    )

    for f in all_files:
        if f.file_name in known_files:
            continue
        dt = _file_dt(f)
        if dt is not None:
            if cursor_dt is None or dt > cursor_dt:
                new_forward.append(f)
            else:
                new_gap.append(f)
        elif mtime_floor is not None and datetime.fromtimestamp(f.mtime) >= mtime_floor:
            new_forward.append(f)

    new_gap.sort(key=lambda f: (_entry_sort_dt(f), f.file_name))
    new_forward.sort(key=lambda f: (_entry_sort_dt(f), f.file_name))
    new_files = new_gap + new_forward
    meta.new_count = len(new_files)

    selected, truncated = _merge_unique(recheck, new_files, limit=_MAX_FILES_PER_RUN)
    meta.truncated = truncated
    _apply_selection_meta(meta, selected, new_files[: max(0, len(selected) - len(recheck))])

    if recheck and new_files:
        meta.scan_mode = "incremental_recheck_plus_cursor_new"
    elif recheck:
        meta.scan_mode = "incremental_recheck_only"
    elif new_files:
        meta.scan_mode = "incremental_cursor_new"
    else:
        meta.scan_mode = "incremental_nothing_to_do"

    return selected, meta


def _update_cursor_after_file(
    db_session: Session,
    source_name: str,
    entry: SourceFileEntry,
    bootstrap_day: int | None = None,
) -> None:
    cursor = _get_or_create_cursor(db_session, source_name)
    file_dt = _file_dt(entry)
    if file_dt and (cursor.last_file_dt is None or file_dt > cursor.last_file_dt):
        cursor.last_file_dt = file_dt
    if bootstrap_day is not None:
        cal = _file_dt(entry)
        if cal:
            cursor.bootstrap_year = cal.year
            cursor.bootstrap_month = cal.month
            cursor.bootstrap_day = max(cursor.bootstrap_day or 0, bootstrap_day)
    cursor.updated_at = datetime.utcnow()
    db_session.add(cursor)
    db_session.commit()


def _upsert_file_state(db_session: Session, source_name: str, entry: SourceFileEntry) -> None:
    state = db_session.scalar(
        select(SourceFileState).where(
            SourceFileState.source_server == source_name,
            SourceFileState.file_name == entry.file_name,
        )
    )
    if not state:
        state = SourceFileState(
            source_server=source_name,
            file_name=entry.file_name,
        )
        db_session.add(state)
    state.last_mtime = int(entry.mtime)
    state.last_size = int(entry.size)
    state.last_imported_at = datetime.utcnow()
    db_session.commit()


def _parse_bootstrap_day_from_mode(scan_mode: str) -> int | None:
    match = re.search(r"bootstrap_day_\d{4}_\d{2}_(\d{2})", scan_mode)
    if match:
        return int(match.group(1))
    return None


def run_import(
    db_session: Session,
    source: SourceSpec,
    batch_id: int | None = None,
    options: ImportOptions | None = None,
) -> ImportJob:
    options = options or ImportOptions()
    import_logger.info("run_import start source=%s mode=%s path=%s", source.name, source.mode, source.path)
    job = ImportJob(source=source.name, status="running", batch_id=batch_id)
    db_session.add(job)
    db_session.commit()

    inserted = 0
    skipped = 0
    sftp = None
    transport = None

    try:
        if source.mode == "sftp":
            transport = paramiko.Transport((source.host, source.port))
            transport.connect(username=source.username, password=source.password)
            sftp = paramiko.SFTPClient.from_transport(transport)
            all_files = _sftp_list_entries(sftp, source)
        else:
            all_files = list_local_files(source.path)

        known_files = _known_files_for_source(db_session, source.name)
        states = _file_states_map(db_session, source.name)
        selected_files, meta = _select_files_for_import(
            db_session, source.name, all_files, known_files, states, options
        )

        job.details = meta.format_details(len(selected_files))
        db_session.add(job)
        db_session.commit()

        import_logger.info(
            "run_import source=%s mode=%s scan_mode=%s selected=%s recheck=%s new=%s "
            "skipped_unchanged=%s min=%s max=%s cursor=%s new_by_day=%s",
            source.name,
            source.mode,
            meta.scan_mode,
            len(selected_files),
            meta.recheck_count,
            meta.new_count,
            meta.skipped_unchanged,
            meta.min_file_dt.date().isoformat() if meta.min_file_dt else None,
            meta.max_file_dt.date().isoformat() if meta.max_file_dt else None,
            meta.cursor_dt.date().isoformat() if meta.cursor_dt else None,
            meta.new_by_day,
        )

        if source.mode == "sftp":
            files_iter = iterate_sftp_selected(source, selected_files, sftp=sftp)
        else:
            files_iter = iterate_local_selected(selected_files)

        bootstrap_day = _parse_bootstrap_day_from_mode(meta.scan_mode)
        processed_count = 0
        chunk: list[CdrRecord] = []

        for file_entry, rows in files_iter:
            processed_count += 1
            if options.on_progress:
                options.on_progress(source.name, processed_count, len(selected_files), file_entry.file_name)

            import_logger.info("processing file source=%s file=%s", source.name, file_entry.file_name)
            rows_total = 0
            rows_parsed = 0

            for row in rows:
                rows_total += 1
                parsed = parse_cdr_line(row)
                if not parsed:
                    skipped += 1
                    continue
                try:
                    event_time = date_parser.isoparse(parsed["event_time"])
                except ValueError:
                    skipped += 1
                    continue

                rows_parsed += 1
                record = CdrRecord(
                    event_time=event_time,
                    cdr_id=parsed["cdr_id"],
                    incoming_connection=parsed["incoming_connection"],
                    outgoing_route=parsed["outgoing_route"],
                    a_msisdn=parsed["a_msisdn"],
                    a_msisdn_translated=parsed["a_msisdn_translated"],
                    b_msisdn=parsed["b_msisdn"],
                    b_msisdn_translated=parsed["b_msisdn_translated"],
                    status=parsed["status"].lower(),
                    source_server=source.name,
                    source_file=file_entry.file_name,
                    raw_line=",".join(row),
                )
                chunk.append(record)
                if len(chunk) >= _COMMIT_CHUNK:
                    ins, sk = _flush_cdr_chunk(db_session, chunk)
                    inserted += ins
                    skipped += sk
                    chunk = []

            if chunk:
                ins, sk = _flush_cdr_chunk(db_session, chunk)
                inserted += ins
                skipped += sk
                chunk = []

            ok_ratio = (rows_parsed / rows_total) if rows_total else 1.0
            if rows_total == 0 or ok_ratio >= _FILE_OK_RATIO:
                _upsert_file_state(db_session, source.name, file_entry)
                _update_cursor_after_file(db_session, source.name, file_entry, bootstrap_day=bootstrap_day)
            else:
                import_logger.warning(
                    "file not marked imported source=%s file=%s parsed=%s total=%s ratio=%.2f",
                    source.name,
                    file_entry.file_name,
                    rows_parsed,
                    rows_total,
                    ok_ratio,
                )

        job.status = "finished"
        job.inserted_rows = inserted
        job.skipped_rows = skipped
        job.finished_at = datetime.utcnow()
        job.is_success = True
        extra = meta.summary_dict()
        extra["inserted"] = inserted
        extra["skipped"] = skipped
        job.details = json.dumps(extra, ensure_ascii=False)
        db_session.commit()
        import_logger.info(
            "run_import finished source=%s inserted=%s skipped=%s",
            source.name,
            inserted,
            skipped,
        )
        return job
    except Exception as exc:  # noqa: BLE001
        db_session.rollback()
        job.status = "failed"
        job.details = str(exc)
        job.finished_at = datetime.utcnow()
        job.is_success = False
        db_session.add(job)
        db_session.commit()
        import_logger.exception("run_import failed source=%s error=%s", source.name, exc)
        return job
    finally:
        if sftp is not None:
            sftp.close()
        if transport is not None:
            transport.close()
