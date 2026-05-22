import os
import paramiko
import time

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)

steps = [
    "cd /opt/smsc && rm -f logs/import.lock",
    """cd /opt/smsc && .venv/bin/python -c "import sqlite3; c=sqlite3.connect('cdr_reporting.db'); n=c.execute(\\\"update import_batches set status='failed', finished_at=datetime('now') where status='running'\\\").rowcount; c.commit(); print('sql cleared', n); print(c.execute('select id,status from import_batches order by id desc limit 2').fetchall())" """,
    "export XDG_RUNTIME_DIR=/run/user/1000 && systemctl --user restart smsc",
    "sleep 3",
    "export XDG_RUNTIME_DIR=/run/user/1000 && systemctl --user is-active smsc",
    "cd /opt/smsc && nohup .venv/bin/python import_once.py >> logs/cron-import.log 2>&1 &",
    "sleep 8",
    "tail -15 /opt/smsc/logs/cron-import.log",
    "tail -8 /opt/smsc/logs/import.log",
    """cd /opt/smsc && .venv/bin/python -c "import sqlite3; c=sqlite3.connect('cdr_reporting.db'); print(c.execute('select id,status,substr(progress_json,1,120) from import_batches order by id desc limit 1').fetchone())" """,
    "pgrep -af 'import_once|gunicorn' | head -6",
]

for step in steps:
    print(">>>", step[:70])
    _, o, e = ssh.exec_command(step, timeout=90)
    time.sleep(0.5)
    out = o.read().decode("utf-8", errors="replace").strip()
    err = e.read().decode("utf-8", errors="replace").strip()
    if out:
        print(out)
    if err and "Warning" not in err:
        print("err:", err[:200])

ssh.close()
print("done")
