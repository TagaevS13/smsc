"""Set SMSC_IMPORT_STALE_MINUTES=600 (10h) on server and deploy import_runner."""
from pathlib import Path

import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")
ROOT = Path(__file__).resolve().parents[1]

CRON_LINE = (
    "*/10 * * * * cd /opt/smsc && set -a && [ -f .env ] && . ./.env; set +a && "
    ".venv/bin/python import_once.py >> /opt/smsc/logs/cron-import.log 2>&1"
)

BASH = f"""
set -e
cd /opt/smsc
grep -q SMSC_IMPORT_STALE_MINUTES .env 2>/dev/null && \\
  sed -i 's/^SMSC_IMPORT_STALE_MINUTES=.*/SMSC_IMPORT_STALE_MINUTES=600/' .env || \\
  echo 'SMSC_IMPORT_STALE_MINUTES=600' >> .env
grep SMSC_IMPORT_STALE .env

(crontab -l 2>/dev/null | grep -v import_once.py || true; echo '{CRON_LINE}') | crontab -

export XDG_RUNTIME_DIR=/run/user/1000
systemctl --user restart smsc
sleep 2
systemctl --user is-active smsc
.venv/bin/python -c "from import_runner import IMPORT_STALE_MINUTES; print('IMPORT_STALE_MINUTES', IMPORT_STALE_MINUTES)"
echo DONE
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = ssh.open_sftp()
sftp.put(str(ROOT / "import_runner.py"), "/opt/smsc/import_runner.py")
sftp.close()
_, o, _ = ssh.exec_command(BASH, timeout=60)
print(o.read().decode("utf-8", errors="replace"))
ssh.close()
