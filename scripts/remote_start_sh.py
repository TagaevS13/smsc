import os
import paramiko

START_SH = """#!/bin/bash
set -e
cd /opt/smsc
mkdir -p logs
pkill -f 'gunicorn.*app:app' 2>/dev/null || true
sleep 1
nohup .venv/bin/gunicorn -w 1 -b 0.0.0.0:8080 --timeout 300 --chdir /opt/smsc app:app >> logs/gunicorn.log 2>&1 &
echo started gunicorn pid=$!
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("172.16.6.183", username="sorbon", password=os.environ.get("SMSC_DEPLOY_PASSWORD", ""))
_, o, _ = c.exec_command("wc -l /opt/smsc/ingestion.py /opt/smsc/app.py")
print(o.read().decode())
sftp = c.open_sftp()
with sftp.file("/opt/smsc/start.sh", "w") as f:
    f.write(START_SH)
sftp.chmod("/opt/smsc/start.sh", 0o755)
sftp.close()
c.close()
