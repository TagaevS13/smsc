"""Deploy critical import fixes + linger + clear stale batch."""
from pathlib import Path

import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")
REMOTE = "/opt/smsc"
ROOT = Path(__file__).resolve().parents[1]

SERVICE = (ROOT / "deploy" / "smsc-user.service").read_text(encoding="utf-8")

BASH = (
    "set -e\ncd /opt/smsc\n"
    + r"""
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('cdr_reporting.db'); n=c.execute(\"update import_batches set status='failed', finished_at=datetime('now') where status='running'\").rowcount; c.commit(); print('cleared running:', n)"
rm -f logs/import.lock
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/smsc.service << 'UNIT'
"""
    + SERVICE
    + r"""
UNIT
export XDG_RUNTIME_DIR=/run/user/1000
systemctl --user daemon-reload
systemctl --user restart smsc
sleep 2
systemctl --user is-active smsc
echo "$SMSC_DEPLOY_PASSWORD" | sudo -S loginctl enable-linger sorbon 2>/dev/null && echo linger=ok || echo linger=skipped
.venv/bin/python import_once.py 2>&1 | tail -5
echo DONE
"""
)

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = ssh.open_sftp()
for name in ("app.py", "import_runner.py", "import_once.py"):
    sftp.put(str(ROOT / name), f"{REMOTE}/{name}")
sftp.close()
_, o, e = ssh.exec_command(BASH, timeout=300)
print(o.read().decode("utf-8", errors="replace").encode("ascii", errors="replace").decode("ascii"))
if e.read().decode().strip():
    print("stderr:", e.read()[:400])
ssh.close()
