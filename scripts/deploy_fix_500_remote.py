"""Deploy session-name fix and admin-only import UI."""
from pathlib import Path

import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")
REMOTE = "/opt/smsc"
ROOT = Path(__file__).resolve().parents[1]

FILES = ["app.py", "templates/index.html"]

SETUP_EXTRA = ""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = ssh.open_sftp()
for rel in FILES:
    sftp.put(str(ROOT / rel), f"{REMOTE}/{rel}")
sftp.close()

cmd = (
    SETUP_EXTRA
    + r"""
export XDG_RUNTIME_DIR=/run/user/1000
systemctl --user restart smsc
sleep 2
systemctl --user is-active smsc
curl -s -o /dev/null -w 'login %{http_code}\n' http://127.0.0.1:8080/smsc/login
"""
)
_, o, _ = ssh.exec_command(cmd, timeout=40)
print(o.read().decode("utf-8", errors="replace"))
ssh.close()
print("deployed")
