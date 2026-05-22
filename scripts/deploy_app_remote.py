"""Upload app.py and restart user systemd smsc."""
from pathlib import Path

import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")
ROOT = Path(__file__).resolve().parents[1]

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = ssh.open_sftp()
sftp.put(str(ROOT / "app.py"), "/opt/smsc/app.py")
sftp.close()

cmd = r"""
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user restart smsc
sleep 2
systemctl --user is-active smsc
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8080/smsc/login
"""
_, stdout, stderr = ssh.exec_command(cmd, timeout=60)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
print(out.encode("ascii", errors="replace").decode("ascii"))
if err.strip():
    print("stderr:", err[:500])
ssh.close()
print("done: app.py deployed")
