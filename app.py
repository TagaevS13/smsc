from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import threading
from functools import wraps
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

import yaml
from flask import Flask, Response, redirect, render_template, request, session, url_for
from sqlalchemy import and_, func, select
from werkzeug.security import check_password_hash, generate_password_hash

from db import AuditLog, CdrRecord, ImportBatch, ImportJob, User, create_session_factory
from ingestion import SourceSpec, run_import


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DB_URL = f"sqlite:///{(BASE_DIR / 'cdr_reporting.db').as_posix()}"
DB_FILE_PATH = str((BASE_DIR / "cdr_reporting.db").resolve())
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("SMSC_REPORTING_SECRET", "change-this-secret-key")
SessionFactory = create_session_factory(DB_URL)

# Only one import at a time (SQLite + long SFTP reads); avoids database is locked.
_IMPORT_LOCK = threading.Lock()

# Application logger: web app lifecycle, auth, routes.
app_logger = logging.getLogger("smsc_app")
app_logger.setLevel(logging.INFO)
if not app_logger.handlers:
    app_handler = RotatingFileHandler(
        LOGS_DIR / "app.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    app_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    app_logger.addHandler(app_handler)

# Import logger: detailed CDR import flow and failures.
import_logger = logging.getLogger("smsc_import")
import_logger.setLevel(logging.INFO)
if not import_logger.handlers:
    import_handler = RotatingFileHandler(
        LOGS_DIR / "import.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    import_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    import_logger.addHandler(import_handler)

# Audit trail: DB table audit_logs + logs/audit.log
audit_logger = logging.getLogger("smsc_audit")
audit_logger.setLevel(logging.INFO)
if not audit_logger.handlers:
    audit_handler = RotatingFileHandler(
        LOGS_DIR / "audit.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    audit_handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(audit_handler)


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or ""


def write_audit(action: str, details: dict | None = None, username: str | None = None) -> None:
    user = username if username is not None else session.get("username", "unknown")
    ip = client_ip()
    payload = json.dumps(details or {}, ensure_ascii=False)
    audit_logger.info("%s | %s | %s | %s | %s", datetime.utcnow().isoformat(), ip, user, action, payload)
    with SessionFactory() as dbs:
        dbs.add(
            AuditLog(
                username=user,
                action=action,
                details=payload,
                remote_addr=ip[:64],
            )
        )
        dbs.commit()


def load_app_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_authenticated"):
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped


def ensure_default_user() -> None:
    default_username = os.getenv("SMSC_ADMIN_USER", "admin")
    default_password = os.getenv("SMSC_ADMIN_PASSWORD", "admin123")
    with SessionFactory() as dbs:
        user_count = dbs.query(User).count()
        if user_count == 0:
            dbs.add(
                User(
                    username=default_username,
                    password_hash=generate_password_hash(default_password),
                    is_active=True,
                )
            )
            dbs.commit()


def load_sources() -> list[SourceSpec]:
    data = load_app_config()
    sources = []
    for raw in data.get("sources", []):
        raw_copy = dict(raw)
        if raw_copy.get("mode") == "local" and raw_copy.get("path"):
            source_path = Path(raw_copy["path"])
            if not source_path.is_absolute():
                raw_copy["path"] = str((BASE_DIR / source_path).resolve())
        sources.append(SourceSpec(**raw_copy))
    return sources


ensure_default_user()

CDR_PAGE_SIZE = 100


def _page_nav_items(current_page: int, total_pages: int) -> list[int | None]:
    """Page numbers for bottom nav; None means ellipsis."""
    if total_pages <= 1:
        return []
    anchors = {1, total_pages, current_page}
    for d in (-2, -1, 0, 1, 2):
        p = current_page + d
        if 1 <= p <= total_pages:
            anchors.add(p)
    nums = sorted(anchors)
    out: list[int | None] = []
    prev = 0
    for n in nums:
        if prev and n - prev > 1:
            out.append(None)
        out.append(n)
        prev = n
    return out


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("is_authenticated"):
        return redirect(url_for("index"))

    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with SessionFactory() as dbs:
            user = dbs.scalar(
                select(User).where(User.username == username, User.is_active.is_(True))
            )
        if user and check_password_hash(user.password_hash, password):
            session["is_authenticated"] = True
            session["username"] = username
            app_logger.info("login success for user=%s", username)
            write_audit("login_success", {}, username=username)
            next_url = request.args.get("next")
            return redirect(next_url or url_for("index"))
        app_logger.warning("login failed for user=%s", username)
        write_audit("login_failed", {}, username=username or "(empty)")
        error = "Invalid username or password"

    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    write_audit("logout", {})
    app_logger.info("logout for user=%s", session.get("username", "unknown"))
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    number = request.args.get("number", "").strip()
    status = request.args.get("status", "").strip().lower()
    source = request.args.get("source", "").strip()
    incoming = request.args.get("incoming", "").strip()

    filters = []
    if start:
        filters.append(CdrRecord.event_time >= datetime.fromisoformat(start))
    if end:
        filters.append(CdrRecord.event_time <= datetime.fromisoformat(end))
    if number:
        filters.append(
            (CdrRecord.a_msisdn.contains(number))
            | (CdrRecord.b_msisdn.contains(number))
            | (CdrRecord.a_msisdn_translated.contains(number))
            | (CdrRecord.b_msisdn_translated.contains(number))
        )
    if status:
        filters.append(CdrRecord.status == status)
    if source:
        filters.append(CdrRecord.source_server == source)
    if incoming:
        filters.append(CdrRecord.incoming_connection.contains(incoming))

    filter_snapshot = {
        "start": start,
        "end": end,
        "number": number,
        "status": status,
        "source": source,
        "incoming": incoming,
    }
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    if page < 1:
        page = 1

    with SessionFactory() as session:
        count_stmt = select(func.count()).select_from(CdrRecord)
        if filters:
            count_stmt = count_stmt.where(and_(*filters))
        total_matching = int(session.scalar(count_stmt) or 0)

        total_pages = math.ceil(total_matching / CDR_PAGE_SIZE) if total_matching else 0
        if total_pages == 0:
            page = 1
        elif page > total_pages:
            page = total_pages

        offset = (page - 1) * CDR_PAGE_SIZE
        stmt = (
            select(CdrRecord)
            .order_by(CdrRecord.event_time.desc())
            .offset(offset)
            .limit(CDR_PAGE_SIZE)
        )
        if filters:
            stmt = stmt.where(and_(*filters))
        records = list(session.scalars(stmt))
        jobs = list(
            session.scalars(select(ImportJob).order_by(ImportJob.started_at.desc()).limit(5))
        )
        batches = list(
            session.scalars(select(ImportBatch).order_by(ImportBatch.started_at.desc()).limit(5))
        )
        sources = sorted(set(session.scalars(select(CdrRecord.source_server))))
        incoming_values = sorted(
            {
                value
                for value in session.scalars(select(CdrRecord.incoming_connection))
                if value
            }
        )

    write_audit(
        "cdr_view",
        {
            "filters": filter_snapshot,
            "page": page,
            "per_page": CDR_PAGE_SIZE,
            "total_matching": total_matching,
        },
    )

    start_idx = offset + 1 if total_matching else 0
    end_idx = min(offset + CDR_PAGE_SIZE, total_matching) if total_matching else 0

    return render_template(
        "index.html",
        records=records,
        jobs=jobs,
        batches=batches,
        import_busy=request.args.get("import_busy"),
        sources=sources,
        incoming_values=incoming_values,
        db_file_path=DB_FILE_PATH,
        pagination={
            "page": page,
            "per_page": CDR_PAGE_SIZE,
            "total": total_matching,
            "total_pages": total_pages,
            "start_idx": start_idx,
            "end_idx": end_idx,
            "nav_items": _page_nav_items(page, total_pages) if total_pages > 1 else [],
        },
        filters={
            "start": start,
            "end": end,
            "number": number,
            "status": status,
            "source": source,
            "incoming": incoming,
        },
    )


@app.post("/import")
@login_required
def import_all():
    if not _IMPORT_LOCK.acquire(blocking=False):
        return redirect(url_for("index", import_busy="1"))

    specs = load_sources()
    audit_extra: dict = {}
    try:
        app_logger.info("import requested for %s source(s)", len(specs))
        with SessionFactory() as session:
            batch = ImportBatch(status="running")
            session.add(batch)
            session.commit()
            session.refresh(batch)

            per_source: list[dict] = []
            for spec in specs:
                app_logger.info("import start source=%s mode=%s", spec.name, spec.mode)
                job = run_import(session, spec, batch_id=batch.id)
                per_source.append(
                    {
                        "source": spec.name,
                        "status": job.status,
                        "inserted": job.inserted_rows,
                        "skipped": job.skipped_rows,
                        "success": job.is_success,
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
            batch.summary = json.dumps({"sources": per_source}, ensure_ascii=False)
            session.add(batch)
            session.commit()
            audit_extra = {
                "batch_id": batch.id,
                "batch_status": batch.status,
                "per_source": per_source,
            }
            app_logger.info(
                "import finished batch_id=%s status=%s",
                batch.id,
                batch.status,
            )
    finally:
        _IMPORT_LOCK.release()

    write_audit(
        "import",
        {"sources": [s.name for s in specs], "count": len(specs), **audit_extra},
    )
    return redirect(url_for("index"))


@app.get("/export.csv")
@login_required
def export_csv():
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    number = request.args.get("number", "").strip()
    status = request.args.get("status", "").strip().lower()
    source = request.args.get("source", "").strip()
    incoming = request.args.get("incoming", "").strip()

    filters = []
    if start:
        filters.append(CdrRecord.event_time >= datetime.fromisoformat(start))
    if end:
        filters.append(CdrRecord.event_time <= datetime.fromisoformat(end))
    if number:
        filters.append(
            (CdrRecord.a_msisdn.contains(number))
            | (CdrRecord.b_msisdn.contains(number))
            | (CdrRecord.a_msisdn_translated.contains(number))
            | (CdrRecord.b_msisdn_translated.contains(number))
        )
    if status:
        filters.append(CdrRecord.status == status)
    if source:
        filters.append(CdrRecord.source_server == source)
    if incoming:
        filters.append(CdrRecord.incoming_connection.contains(incoming))

    with SessionFactory() as session:
        stmt = select(CdrRecord).order_by(CdrRecord.event_time.desc())
        if filters:
            stmt = stmt.where(and_(*filters))
        rows = list(session.scalars(stmt))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "event_time",
            "cdr_id",
            "source_server",
            "incoming_connection",
            "outgoing_route",
            "a_msisdn",
            "a_msisdn_translated",
            "b_msisdn",
            "b_msisdn_translated",
            "status",
            "source_file",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.event_time.isoformat(),
                row.cdr_id,
                row.source_server,
                row.incoming_connection,
                row.outgoing_route,
                row.a_msisdn,
                row.a_msisdn_translated,
                row.b_msisdn,
                row.b_msisdn_translated,
                row.status,
                row.source_file,
            ]
        )
    write_audit(
        "cdr_export",
        {
            "filters": {
                "start": start,
                "end": end,
                "number": number,
                "status": status,
                "source": source,
                "incoming": incoming,
            },
            "rows": len(rows),
        },
    )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=cdr_report.csv"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
