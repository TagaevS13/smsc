"""Upload critical files and restart smsc (no long import run)."""
from pathlib import Path

import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")
ROOT = Path(__file__).resolve().parents[1]
SERVICE = (ROOT / "deploy" / "smsc-user.service").read_text(encoding="utf-8")

BASH = (
    "set -e\ncd /opt/smsc\n"
    + """.venv/bin/python -c "import sqlite3; c=sqlite3.connect('cdr_reporting.db'); n=c.execute(\\"update import_batches set status='failed', finished_at=datetime('now') where status='running'\\").rowcount; c.commit(); print('cleared', n)"
rm -f logs/import.lock
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/smsc.service << 'UNIT'
"""
    + SERVICE
    + """UNIT
export XDG_RUNTIME_DIR=/run/user/1000
systemctl --user daemon-reload
systemctl --user restart smsc
sleep 2
systemctl --user is-active smsc
pgrep -af gunicorn | head -3
echo DONE
"""
)

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = ssh.open_sftp()
for name in ("app.py", "import_runner.py", "import_once.py"):
    sftp.put(str(ROOT / name), f"/opt/smsc/{name}")
sftp.close()
_, o, _ = ssh.exec_command(BASH, timeout=45)
print(o.read().decode("utf-8", errors="replace"))
ssh.close()
