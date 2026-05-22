from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import subprocess
import sys
import threading
from functools import wraps
from datetime import date as date_type, datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

import yaml
from flask import Blueprint, Flask, Response, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import and_, func, select
from werkzeug.security import check_password_hash, generate_password_hash

from db import AuditLog, CdrRecord, ImportBatch, ImportJob, User, create_session_factory
from import_runner import reconcile_stale_running_batches
from ingestion import _MAX_FILES_PER_RUN


BASE_DIR = Path(__file__).resolve().parent
PYTHON_BIN = Path(os.getenv("SMSC_PYTHON", sys.executable))
IMPORT_CLI = BASE_DIR / "import_once.py"
CONFIG_PATH = BASE_DIR / "config.yaml"
DB_URL = f"sqlite:///{(BASE_DIR / 'cdr_reporting.db').as_posix()}"
DB_FILE_PATH = str((BASE_DIR / "cdr_reporting.db").resolve())
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

URL_PREFIX = (os.getenv("SMSC_URL_PREFIX", "/smsc") or "/smsc").rstrip("/") or "/smsc"
PASSWORD_HASH_METHOD = "pbkdf2:sha256"

app = Flask(__name__)
app.secret_key = os.getenv("SMSC_REPORTING_SECRET", "change-this-secret-key")
app.config["APPLICATION_ROOT"] = URL_PREFIX
app.config["SESSION_COOKIE_PATH"] = URL_PREFIX
SessionFactory = create_session_factory(DB_URL)

bp = Blueprint("smsc", __name__, url_prefix=URL_PREFIX)

_IMPORT_PROGRESS_LOCK = threading.Lock()
_IMPORT_PROGRESS: dict = {
    "running": False,
    "batch_id": None,
    "source": "",
    "current_file": "",
    "processed": 0,
    "selected": 0,
    "status": "",
}

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


def write_audit(
    action: str,
    details: dict | None = None,
    username: str | None = None,
    remote_ip: str | None = None,
) -> None:
    user = username if username is not None else session.get("username", "unknown")
    ip = remote_ip if remote_ip is not None else client_ip()
    payload = json.dumps(details or {}, ensure_ascii=False)
    audit_logger.info("%s | %s | %s | %s | %s", datetime.utcnow().isoformat(), ip, user, action, payload)
    try:
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
    except Exception as exc:  # noqa: BLE001
        app_logger.warning("audit db write failed action=%s: %s", action, exc)


def write_audit_async(
    action: str,
    details: dict | None = None,
    username: str | None = None,
) -> None:
    ip = client_ip()
    threading.Thread(
        target=write_audit,
        kwargs={"action": action, "details": details, "username": username, "remote_ip": ip},
        daemon=True,
    ).start()


def _clear_session_cookies(response: Response) -> Response:
    cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
    for path in {URL_PREFIX, "/"}:
        response.delete_cookie(cookie_name, path=path)
    return response


def load_app_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_authenticated"):
            return redirect(url_for("smsc.login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped


def _user_is_admin() -> bool:
    """Admin flag from DB (session can be stale after role changes)."""
    if not session.get("is_authenticated"):
        return False
    username = session.get("username")
    if not username:
        return False
    try:
        with SessionFactory() as dbs:
            user = dbs.scalar(
                select(User).where(User.username == username, User.is_active.is_(True))
            )
            is_admin = bool(user and user.is_admin)
    except Exception as exc:  # noqa: BLE001
        app_logger.warning("is_admin lookup failed for %s: %s", username, exc)
        is_admin = bool(session.get("is_admin", False))
    session["is_admin"] = is_admin
    session.modified = True
    return is_admin


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_authenticated"):
            return redirect(url_for("smsc.login", next=request.path))
        if not _user_is_admin():
            return redirect(url_for("smsc.index"))
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
                    password_hash=generate_password_hash(
                        default_password, method=PASSWORD_HASH_METHOD
                    ),
                    is_active=True,
                    is_admin=True,
                )
            )
            dbs.commit()


def _parse_date_param(value: str) -> date_type | None:
    value = (value or "").strip()
    if not value:
        return None
    return date_type.fromisoformat(value)


def _set_import_progress(**kwargs) -> None:
    with _IMPORT_PROGRESS_LOCK:
        _IMPORT_PROGRESS.update(kwargs)


def _get_import_progress() -> dict:
    with _IMPORT_PROGRESS_LOCK:
        return dict(_IMPORT_PROGRESS)


def _spawn_import_process(
    backfill_from: date_type | None,
    backfill_to: date_type | None,
) -> bool:
    """Start import in a separate process (not inside Gunicorn worker)."""
    if not IMPORT_CLI.is_file():
        app_logger.error("import CLI missing: %s", IMPORT_CLI)
        return False

    if reconcile_stale_running_batches():
        return False

    cmd = [str(PYTHON_BIN), str(IMPORT_CLI), "--triggered-by", "web"]
    if backfill_from:
        cmd.extend(["--from", backfill_from.isoformat()])
    if backfill_to:
        cmd.extend(["--to", backfill_to.isoformat()])

    log_path = LOGS_DIR / "web-import.log"
    log_fh = log_path.open("a", encoding="utf-8")
    subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_fh.close()

    write_audit_async(
        "import_started",
        {
            "backfill_from": backfill_from.isoformat() if backfill_from else None,
            "backfill_to": backfill_to.isoformat() if backfill_to else None,
        },
    )
    app_logger.info("spawned import subprocess: %s", " ".join(cmd))
    return True


ensure_default_user()

try:
    reconcile_stale_running_batches()
    app_logger.info("startup: reconciled stale import batches")
except Exception as exc:  # noqa: BLE001
    app_logger.warning("startup reconcile failed: %s", exc)

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


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/")
def root_redirect():
    return redirect(url_for("smsc.index"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("is_authenticated"):
        return redirect(url_for("smsc.index"))

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
            session["is_admin"] = bool(user.is_admin)
            session.modified = True
            app_logger.info("login success for user=%s", username)
            write_audit_async("login_success", {}, username=username)
            next_url = request.args.get("next")
            target = next_url or url_for("smsc.index")
            if target and not target.startswith(URL_PREFIX):
                target = url_for("smsc.index")
            return redirect(target)
        app_logger.warning("login failed for user=%s", username)
        write_audit_async("login_failed", {}, username=username or "(empty)")
        error = "Invalid username or password"

    return render_template("login.html", error=error)


@bp.post("/logout")
def logout():
    username = session.get("username", "unknown")
    session.clear()
    session.modified = True
    app_logger.info("logout for user=%s", username)
    write_audit_async("logout", {}, username=username)
    resp = redirect(url_for("smsc.login"))
    return _clear_session_cookies(resp)


@bp.get("/")
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

    user_is_admin = _user_is_admin()
    active_batch = None
    jobs: list = []
    batches: list = []

    with SessionFactory() as dbs:
        count_stmt = select(func.count()).select_from(CdrRecord)
        if filters:
            count_stmt = count_stmt.where(and_(*filters))
        total_matching = int(dbs.scalar(count_stmt) or 0)

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
        records = list(dbs.scalars(stmt))
        active_batch = dbs.scalar(
            select(ImportBatch)
            .where(ImportBatch.status == "running")
            .order_by(ImportBatch.started_at.desc())
        )
        if user_is_admin:
            jobs = list(
                dbs.scalars(select(ImportJob).order_by(ImportJob.started_at.desc()).limit(5))
            )
            batches = list(
                dbs.scalars(select(ImportBatch).order_by(ImportBatch.started_at.desc()).limit(5))
            )
        sources = sorted(set(dbs.scalars(select(CdrRecord.source_server))))
        incoming_values = sorted(
            {
                value
                for value in dbs.scalars(select(CdrRecord.incoming_connection))
                if value
            }
        )

    write_audit_async(
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

    import_progress = _get_import_progress()
    if active_batch and active_batch.progress_json:
        try:
            import_progress = {**import_progress, **json.loads(active_batch.progress_json)}
        except json.JSONDecodeError:
            pass

    return render_template(
        "index.html",
        records=records,
        jobs=jobs,
        batches=batches,
        import_busy=request.args.get("import_busy") if user_is_admin else None,
        import_started=request.args.get("import_started") if user_is_admin else None,
        import_progress=import_progress,
        max_files_per_run=_MAX_FILES_PER_RUN,
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
        is_admin=user_is_admin,
    )


@bp.get("/import/status")
@login_required
def import_status():
    progress = _get_import_progress()
    with SessionFactory() as dbs:
        active = dbs.scalar(
            select(ImportBatch)
            .where(ImportBatch.status == "running")
            .order_by(ImportBatch.started_at.desc())
        )
        if active and active.progress_json:
            try:
                progress = {**progress, **json.loads(active.progress_json)}
            except json.JSONDecodeError:
                pass
    return jsonify(progress)


@bp.post("/import")
@admin_required
def import_all():
    backfill_from = _parse_date_param(request.form.get("backfill_from", ""))
    backfill_to = _parse_date_param(request.form.get("backfill_to", ""))
    if backfill_from and backfill_to and backfill_from > backfill_to:
        return redirect(url_for("smsc.index", import_busy="1"))

    if not _spawn_import_process(backfill_from, backfill_to):
        return redirect(url_for("smsc.index", import_busy="1"))

    return redirect(url_for("smsc.index", import_started="1"))


@bp.get("/export.csv")
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

    with SessionFactory() as dbs:
        stmt = select(CdrRecord).order_by(CdrRecord.event_time.desc())
        if filters:
            stmt = stmt.where(and_(*filters))
        rows = list(dbs.scalars(stmt))

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


app.register_blueprint(bp)


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=debug)
