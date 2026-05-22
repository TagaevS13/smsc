#!/usr/bin/env python3
"""Quick DB stats for cdr_reporting.db (run on server: .venv/bin/python scripts/check_db.py).

Do not keep `sqlite3 cdr_reporting.db` open in another terminal — it locks the DB and the web UI hangs.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "cdr_reporting.db"


def main() -> None:
    print(f"Database file: {DB}")
    print(f"Size: {DB.stat().st_size / 1024:.1f} KB")
    con = sqlite3.connect(DB)
    cur = con.cursor()
    ver = cur.execute("select sqlite_version()").fetchone()[0]
    print(f"Engine: SQLite {ver} (file-based, not MySQL/PostgreSQL)")
    print()
    tables = [
        "users",
        "cdr_records",
        "source_file_states",
        "import_cursors",
        "import_jobs",
        "import_batches",
        "audit_logs",
    ]
    for t in tables:
        try:
            n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {n}")
        except sqlite3.OperationalError:
            print(f"  {t}: (missing)")
    cur.execute("SELECT MIN(event_time), MAX(event_time) FROM cdr_records")
    row = cur.fetchone()
    if row and row[0]:
        print(f"\nCDR event_time range: {row[0]} .. {row[1]}")
    cur.execute("SELECT * FROM import_cursors")
    rows = cur.fetchall()
    if rows:
        cur.execute("PRAGMA table_info(import_cursors)")
        cols = [c[1] for c in cur.fetchall()]
        print("\nimport_cursors:")
        for r in rows:
            print(" ", dict(zip(cols, r)))
    else:
        print("\nimport_cursors: empty (no incremental import finished yet)")
    con.close()


if __name__ == "__main__":
    main()
