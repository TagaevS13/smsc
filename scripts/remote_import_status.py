import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = ssh.open_sftp()
with sftp.file("/tmp/check_import.py", "w") as f:
    f.write("""
from sqlalchemy import select
from db import ImportBatch, create_session_factory
sf = create_session_factory('sqlite:////opt/smsc/cdr_reporting.db')
with sf() as dbs:
    for b in dbs.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(3)):
        pj = (b.progress_json or '')[:150]
        print(b.id, b.status, b.started_at, pj)
""")
sftp.close()
cmds = [
    "export XDG_RUNTIME_DIR=/run/user/1000; systemctl --user is-active smsc",
    "pgrep -af import_once || echo no import_once running",
    "tail -12 /opt/smsc/logs/cron-import.log 2>/dev/null || echo no cron log",
    "tail -6 /opt/smsc/logs/import.log 2>/dev/null || echo no import log",
    "cd /opt/smsc && .venv/bin/python /tmp/check_import.py",
]
for c in cmds:
    print("===", c[:60])
    _, o, _ = ssh.exec_command(c, timeout=30)
    print(o.read().decode() or "(empty)")
ssh.close()
