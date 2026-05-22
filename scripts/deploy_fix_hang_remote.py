"""Kill sqlite3 locks, deploy app+login template, update gunicorn workers, restart."""
from pathlib import Path

import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")
ROOT = Path(__file__).resolve().parents[1]

SERVICE = (ROOT / "deploy" / "smsc-user.service").read_text(encoding="utf-8")

SETUP = r"""
set -e
pkill -f 'sqlite3.*/opt/smsc/cdr_reporting.db' 2>/dev/null || true
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/smsc.service << 'UNIT'
""" + SERVICE + r"""
UNIT
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user daemon-reload
systemctl --user restart smsc
sleep 2
systemctl --user is-active smsc
fuser /opt/smsc/cdr_reporting.db 2>/dev/null || true
curl -s -o /dev/null -w 'login GET %{time_total}s %{http_code}\n' http://127.0.0.1:8080/smsc/login
curl -s -o /dev/null -w 'login POST %{time_total}s %{http_code}\n' -X POST -d 'username=admin&password=admin123' http://127.0.0.1:8080/smsc/login
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = ssh.open_sftp()
for rel in ("app.py", "templates/login.html"):
    sftp.put(str(ROOT / rel), f"/opt/smsc/{rel}")
sftp.close()
_, stdout, _ = ssh.exec_command(f"bash -s <<'EOF'\n{SETUP}\nEOF", timeout=60)
print(stdout.read().decode("utf-8", errors="replace").encode("ascii", errors="replace").decode("ascii"))
ssh.close()
print("done")
