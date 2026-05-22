"""Kill stuck import, mark batch failed, restart service, run import_once."""
import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")

CMD = r"""
set -e
cd /opt/smsc

echo '=== processes ==='
pgrep -af 'import_once|gunicorn' || true
fuser -v logs/import.lock 2>/dev/null || echo no lock file users

echo '=== batches before ==='
.venv/bin/python -c "
from sqlalchemy import select, text
from datetime import datetime
from db import ImportBatch, create_session_factory
sf = create_session_factory('sqlite:////opt/smsc/cdr_reporting.db')
with sf() as dbs:
    for b in dbs.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(3)):
        print(b.id, b.status, b.started_at, (b.summary or '')[:80])
    running = dbs.scalars(select(ImportBatch).where(ImportBatch.status=='running')).all()
    for b in running:
        b.status = 'failed'
        b.finished_at = datetime.utcnow()
        b.summary = (b.summary or '') + '\n[restarted: stuck import cleared]'
    dbs.commit()
    print('marked failed:', len(running))
"

# Release file lock if held
rm -f logs/import.lock 2>/dev/null || true
pkill -f 'import_once.py' 2>/dev/null || true

export XDG_RUNTIME_DIR=/run/user/1000
systemctl --user restart smsc
sleep 2
systemctl --user is-active smsc

echo '=== starting fresh incremental import ==='
nohup .venv/bin/python import_once.py >> logs/cron-import.log 2>&1 &
sleep 3
tail -5 logs/cron-import.log 2>/dev/null || true
tail -5 logs/import.log 2>/dev/null || true

echo '=== batches after ==='
.venv/bin/python -c "
from sqlalchemy import select
from db import ImportBatch, create_session_factory
sf = create_session_factory('sqlite:////opt/smsc/cdr_reporting.db')
with sf() as dbs:
    for b in dbs.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(2)):
        print(b.id, b.status, b.started_at)
"
echo DONE
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
_, o, e = ssh.exec_command(CMD, timeout=120)
print(o.read().decode("utf-8", errors="replace"))
err = e.read().decode("utf-8", errors="replace")
if err.strip():
    print("stderr:", err[:500])
ssh.close()
