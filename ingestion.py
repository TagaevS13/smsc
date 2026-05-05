from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import paramiko
from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db import CdrRecord, ImportJob

# Fewer, larger commits reduce SQLite lock contention with concurrent web requests.
_COMMIT_CHUNK = 250
_RECENT_FILES_TO_RECHECK = 3


import_logger = logging.getLogger("smsc_import")


@dataclass
class SourceSpec:
    name: str
    mode: str  # local | sftp
    path: str
    host: str = ""
    username: str = ""
    password: str = ""
    port: int = 22


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


def iterate_local_files(
    path: str, recent_only: int | None = None
) -> Iterable[tuple[str, Iterable[list[str]]]]:
    base = Path(path)
    if not base.exists():
        return []
    files = [p for p in base.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if recent_only is not None:
        files = files[:recent_only]
    files.sort(key=lambda p: p.stat().st_mtime)
    for file_path in files:
        def row_iter(current: Path = file_path) -> Iterable[list[str]]:
            with current.open("r", encoding="utf-8", errors="ignore") as fh:
                reader = csv.reader(fh)
                for row in reader:
                    if not row or row[0].startswith("date"):
                        continue
                    yield row
        yield file_path.name, row_iter()


def iterate_sftp_files(
    spec: SourceSpec, recent_only: int | None = None
) -> Iterable[tuple[str, Iterable[list[str]]]]:
    transport = paramiko.Transport((spec.host, spec.port))
    transport.connect(username=spec.username, password=spec.password)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
        attrs = [a for a in sftp.listdir_attr(spec.path) if a.filename]
        attrs.sort(key=lambda a: a.st_mtime, reverse=True)
        if recent_only is not None:
            attrs = attrs[:recent_only]
        attrs.sort(key=lambda a: a.st_mtime)
        for attr in attrs:
            if not attr.filename:
                continue
            remote_path = f"{spec.path.rstrip('/')}/{attr.filename}"

            def row_iter(current_remote: str = remote_path) -> Iterable[list[str]]:
                # Read as bytes to keep behavior consistent across SFTP servers.
                with sftp.open(current_remote, "rb") as fh:
                    for raw in fh:
                        if isinstance(raw, bytes):
                            line = raw.decode("utf-8", errors="ignore").strip()
                        else:
                            line = str(raw).strip()
                        if not line or line.startswith("date"):
                            continue
                        yield next(csv.reader([line]))

            yield attr.filename, row_iter()
    finally:
        sftp.close()
        transport.close()


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


def run_import(db_session: Session, source: SourceSpec, batch_id: int | None = None) -> ImportJob:
    import_logger.info("run_import start source=%s mode=%s path=%s", source.name, source.mode, source.path)
    job = ImportJob(source=source.name, status="running", batch_id=batch_id)
    db_session.add(job)
    db_session.commit()

    inserted = 0
    skipped = 0
    try:
        source_has_data = (
            db_session.scalar(
                select(CdrRecord.id).where(CdrRecord.source_server == source.name).limit(1)
            )
            is not None
        )
        recent_only = _RECENT_FILES_TO_RECHECK if source_has_data else None
        scan_mode = (
            f"recent_{_RECENT_FILES_TO_RECHECK}_files" if source_has_data else "full_scan"
        )
        job.details = scan_mode
        db_session.add(job)
        db_session.commit()

        if source.mode == "sftp":
            files_iter = iterate_sftp_files(source, recent_only=recent_only)
        else:
            files_iter = iterate_local_files(source.path, recent_only=recent_only)
        import_logger.info(
            "run_import source=%s mode=%s scan_mode=%s",
            source.name,
            source.mode,
            scan_mode,
        )

        chunk: list[CdrRecord] = []
        for file_name, rows in files_iter:
            import_logger.info("processing file source=%s file=%s", source.name, file_name)
            for row in rows:
                parsed = parse_cdr_line(row)
                if not parsed:
                    skipped += 1
                    continue
                try:
                    event_time = date_parser.isoparse(parsed["event_time"])
                except ValueError:
                    skipped += 1
                    continue

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
                    source_file=file_name,
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

        job.status = "finished"
        job.inserted_rows = inserted
        job.skipped_rows = skipped
        job.finished_at = datetime.utcnow()
        job.is_success = True
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
