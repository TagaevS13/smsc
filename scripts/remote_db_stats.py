import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")
PY = r"""
import sqlite3
c = sqlite3.connect("/opt/smsc/cdr_reporting.db")
print("SQLite", c.execute("select sqlite_version()").fetchone()[0])
for t in ("users", "cdr_records", "source_file_states", "import_jobs", "import_batches"):
    try:
        print(t, c.execute("select count(*) from " + t).fetchone()[0])
    except Exception as e:
        print(t, e)
r = c.execute("select min(event_time), max(event_time) from cdr_records").fetchone()
print("event_time", r)
c.close()
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=15)
sftp = c.open_sftp()
with sftp.file("/tmp/smsc_db_check.py", "w") as fh:
    fh.write(PY)
sftp.close()
_, o, e = c.exec_command("/opt/smsc/.venv/bin/python /tmp/smsc_db_check.py", timeout=30)
print(o.read().decode("utf-8", errors="replace") or "(no output)")
err = e.read().decode("utf-8", errors="replace").strip()
if err:
    print("stderr:", err)
c.close()
