"""Install systemd unit on server and enable auto-restart."""
from __future__ import annotations

import sys
from pathlib import Path

import os
import paramiko

HOST = "172.16.6.183"
USER = "sorbon"
PASSWORD = os.environ.get("SMSC_DEPLOY_PASSWORD", "")
REMOTE_DIR = "/opt/smsc"
ROOT = Path(__file__).resolve().parents[1]
SERVICE = (ROOT / "deploy" / "smsc.service").read_text(encoding="utf-8")

SETUP = """
set -e
# Stop manual gunicorn if running
pkill -f 'gunicorn.*app:app' 2>/dev/null || true
sleep 2

cp /tmp/smsc.service /etc/systemd/system/smsc.service
systemctl daemon-reload
systemctl enable smsc
systemctl restart smsc
sleep 2
systemctl is-active smsc
systemctl status smsc --no-pager | head -12
curl -s -o /dev/null -w 'health %{http_code}\n' http://127.0.0.1:8080/smsc/login
"""


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"connect {USER}@{HOST}")
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()
    with sftp.file("/tmp/smsc.service", "w") as fh:
        fh.write(SERVICE)
    sftp.close()

    # Try sudo with SSH password
    cmd = f"echo {PASSWORD!r} | sudo -S bash -s <<'EOF'\n{SETUP}\nEOF"
    _, stdout, stderr = ssh.exec_command(cmd, timeout=120)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    code = stdout.channel.recv_exit_status()
    if code != 0:
        print("\n--- sudo failed: run on server as root/sudo ---")
        print(f"sudo cp /tmp/smsc.service /etc/systemd/system/smsc.service")
        print("sudo systemctl daemon-reload && sudo systemctl enable --now smsc")
        sys.exit(1)
    print("systemd smsc installed and active")
    ssh.close()


if __name__ == "__main__":
    main()
