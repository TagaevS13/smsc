from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class CdrRecord(Base):
    __tablename__ = "cdr_records"
    __table_args__ = (UniqueConstraint("cdr_id", "source_server", name="uq_cdr_server"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    cdr_id: Mapped[str] = mapped_column(String(64), index=True)
    incoming_connection: Mapped[str] = mapped_column(String(128), default="")
    outgoing_route: Mapped[str] = mapped_column(String(128), default="")
    a_msisdn: Mapped[str] = mapped_column(String(64), index=True)
    a_msisdn_translated: Mapped[str] = mapped_column(String(64), default="")
    b_msisdn: Mapped[str] = mapped_column(String(64), index=True)
    b_msisdn_translated: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    source_server: Mapped[str] = mapped_column(String(64), index=True)
    source_file: Mapped[str] = mapped_column(String(255), default="")
    raw_line: Mapped[str] = mapped_column(String(2048), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ImportBatch(Base):
    """One user import action: multiple sources; overall success only if all succeed."""

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("import_batches.id"), nullable=True, index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="running")
    inserted_rows: Mapped[int] = mapped_column(Integer, default=0)
    skipped_rows: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[str] = mapped_column(String(2048), default="")
    is_success: Mapped[bool] = mapped_column(Boolean, default=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    """Who did what: login, search filters, export, import, clear."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    username: Mapped[str] = mapped_column(String(64), index=True, default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    details: Mapped[str] = mapped_column(Text, default="")
    remote_addr: Mapped[str] = mapped_column(String(64), default="")


def _ensure_sqlite_schema(engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    insp = inspect(engine)
    if "import_batches" not in insp.get_table_names():
        ImportBatch.__table__.create(engine, checkfirst=True)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("import_jobs")}
    if "batch_id" not in cols:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE import_jobs ADD COLUMN batch_id INTEGER REFERENCES import_batches(id)")
            )


def create_session_factory(db_url: str) -> sessionmaker[Session]:
    if db_url.startswith("sqlite:"):
        engine = create_engine(db_url, future=True, connect_args={"timeout": 60.0})
    else:
        engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    _ensure_sqlite_schema(engine)
    return sessionmaker(bind=engine, future=True)
