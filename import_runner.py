"""Shared CDR import batch logic (web UI and cron)."""
from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from sqlalchemy import delete, select

from db import CdrRecord, ImportBatch, create_session_factory
from ingestion import ImportOptions, SourceSpec, run_import

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DB_URL = f"sqlite:///{(BASE_DIR / 'cdr_reporting.db').as_posix()}"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
LOCK_PATH = LOGS_DIR / "import.lock"

SessionFactory = create_session_factory(DB_URL)
CDR_RETENTION_DAYS = int(os.getenv("SMSC_CDR_RETENTION_DAYS", "90"))
IMPORT_STALE_MINUTES = int(os.getenv("SMSC_IMPORT_STALE_MINUTES", "600"))  # 10 hours
IMPORT_PROGRESS_STALE_MINUTES = int(os.getenv("SMSC_IMPORT_PROGRESS_STALE_MINUTES", "30"))
IMPORT_ORPHAN_GRACE_MINUTES = int(os.getenv("SMSC_IMPORT_ORPHAN_GRACE_MINUTES", "3"))

import_logger = logging.getLogger("smsc_import")
if not import_logger.handlers:
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(
        LOGS_DIR / "import.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    import_logger.addHandler(handler)
    import_logger.setLevel(logging.INFO)


def load_sources() -> list[SourceSpec]:
    if not CONFIG_PATH.exists():
        return []
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    sources = []
    for raw in data.get("sources", []):
        raw_copy = dict(raw)
        if raw_copy.get("mode") == "local" and raw_copy.get("path"):
            source_path = Path(raw_copy["path"])
            if not source_path.is_absolute():
                raw_copy["path"] = str((BASE_DIR / source_path).resolve())
        sources.append(SourceSpec(**raw_copy))
    return sources


def _format_batch_summary(batch_status: str, per_source: list[dict]) -> str:
    lines = [f"Overall: {batch_status}"]
    for item in per_source:
        line = (
            f"{item.get('source')}: {item.get('status')} "
            f"(inserted={item.get('inserted', 0)}, skipped={item.get('skipped', 0)})"
        )
        details = item.get("details") or {}
        if isinstance(details, dict):
            mode = details.get("scan_mode")
            if mode:
                line += f" mode={mode}"
        lines.append(line)
    return "\n".join(lines)


def _import_lock_is_free() -> bool:
    """True if no other process holds the import file lock."""
    LOCK_PATH.touch(exist_ok=True)
    fh = LOCK_PATH.open("w", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        fh.close()


def _progress_last_updated(progress_json: str | None) -> datetime | None:
    if not progress_json:
        return None
    try:
        data = json.loads(progress_json)
    except json.JSONDecodeError:
        return None
    raw = data.get("updated_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", ""))
    except ValueError:
        return None


def _batch_is_stale(batch: ImportBatch, now: datetime) -> tuple[bool, str]:
    if batch.started_at and batch.started_at < now - timedelta(minutes=IMPORT_STALE_MINUTES):
        return True, f"started>{IMPORT_STALE_MINUTES}m ago"
    last = _progress_last_updated(batch.progress_json)
    if last and last < now - timedelta(minutes=IMPORT_PROGRESS_STALE_MINUTES):
        return True, f"no progress>{IMPORT_PROGRESS_STALE_MINUTES}m"
    if (
        batch.started_at
        and batch.started_at < now - timedelta(minutes=IMPORT_ORPHAN_GRACE_MINUTES)
        and _import_lock_is_free()
    ):
        return True, "orphan (no import lock)"
    return False, ""


def reconcile_stale_running_batches() -> bool:
    """Mark stuck running batches failed; return True if a live import remains."""
    now = datetime.utcnow()
    with SessionFactory() as session:
        running = session.scalars(
            select(ImportBatch).where(ImportBatch.status == "running")
        ).all()
        still_active = False
        for batch in running:
            stale, reason = _batch_is_stale(batch, now)
            if stale:
                batch.status = "failed"
                batch.finished_at = now
                batch.summary = (batch.summary or "") + f"\n[stale: {reason}]"
                import_logger.warning(
                    "stale import batch_id=%s marked failed (%s)", batch.id, reason
                )
            else:
                still_active = True
        session.commit()
        return still_active


@contextmanager
def import_file_lock():
    """Cross-process exclusive lock (cron vs web)."""
    LOCK_PATH.touch(exist_ok=True)
    fh = LOCK_PATH.open("w", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as exc:
        raise RuntimeError("import already running (lock held)") from exc
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def purge_old_cdr_records(retention_days: int | None = None) -> int:
    days = retention_days if retention_days is not None else CDR_RETENTION_DAYS
    cutoff = datetime.utcnow() - timedelta(days=days)
    with SessionFactory() as session:
        result = session.execute(delete(CdrRecord).where(CdrRecord.event_time < cutoff))
        deleted = result.rowcount or 0
        session.commit()
    if deleted:
        import_logger.info("purged cdr_records older than %s days: deleted=%s", days, deleted)
    return deleted


def run_import_batch(
    backfill_from: date | None = None,
    backfill_to: date | None = None,
    triggered_by: str = "manual",
) -> str:
    specs = load_sources()
    if not specs:
        import_logger.error("no sources configured")
        return "failed"

    if reconcile_stale_running_batches():
        import_logger.info("skip import: batch already running")
        return "skipped_running"

    with import_file_lock():
        if reconcile_stale_running_batches():
            return "skipped_running"

        batch_status = "done"
        try:
            import_logger.info(
                "import batch start triggered_by=%s sources=%s backfill=%s..%s",
                triggered_by,
                len(specs),
                backfill_from,
                backfill_to,
            )
            with SessionFactory() as session:
                batch = ImportBatch(status="running")
                session.add(batch)
                session.commit()
                session.refresh(batch)

                def on_progress(
                    source_name: str, processed: int, selected: int, current_file: str
                ) -> None:
                    progress = {
                        "running": True,
                        "batch_id": batch.id,
                        "source": source_name,
                        "current_file": current_file,
                        "processed": processed,
                        "selected": selected,
                        "status": "running",
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                    batch.progress_json = json.dumps(progress, ensure_ascii=False)
                    session.add(batch)
                    session.commit()

                per_source: list[dict] = []
                import_options = ImportOptions(
                    backfill_from=backfill_from,
                    backfill_to=backfill_to,
                    on_progress=on_progress,
                )

                for spec in specs:
                    job = run_import(session, spec, batch_id=batch.id, options=import_options)
                    job_details: dict | str = job.details or ""
                    try:
                        job_details = json.loads(job.details) if job.details else {}
                    except json.JSONDecodeError:
                        job_details = {"raw": job.details}
                    per_source.append(
                        {
                            "source": spec.name,
                            "status": job.status,
                            "inserted": job.inserted_rows,
                            "skipped": job.skipped_rows,
                            "success": job.is_success,
                            "details": job_details,
                        }
                    )

                batch.finished_at = datetime.utcnow()
                if not per_source:
                    batch.status = "failed"
                elif all(s["success"] for s in per_source):
                    batch.status = "success"
                elif any(s["success"] for s in per_source):
                    batch.status = "partial"
                else:
                    batch.status = "failed"
                batch.summary = _format_batch_summary(batch.status, per_source)
                batch.progress_json = json.dumps(
                    {"running": False, "batch_id": batch.id, "status": batch.status},
                    ensure_ascii=False,
                )
                session.add(batch)
                session.commit()
                batch_status = batch.status
                import_logger.info(
                    "import finished batch_id=%s status=%s triggered_by=%s",
                    batch.id,
                    batch.status,
                    triggered_by,
                )
        except Exception as exc:  # noqa: BLE001
            import_logger.exception("import batch failed: %s", exc)
            batch_status = "failed"

        purge_old_cdr_records()
        return batch_status
