"""Install smsc as sorbon user systemd service (no root sudo for unit file)."""
from __future__ import annotations

import sys
from pathlib import Path

import os
import paramiko

HOST = "172.16.6.183"
USER = "sorbon"
PASSWORD = os.environ.get("SMSC_DEPLOY_PASSWORD", "")
ROOT = Path(__file__).resolve().parents[1]
SERVICE = (ROOT / "deploy" / "smsc-user.service").read_text(encoding="utf-8")

SETUP = r"""
set -e
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/smsc.service << 'UNIT'
""" + SERVICE + r"""
UNIT

pkill -f 'gunicorn.*app:app' 2>/dev/null || true
sleep 2

export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user daemon-reload
systemctl --user enable smsc
systemctl --user restart smsc
sleep 2
systemctl --user is-active smsc
systemctl --user status smsc --no-pager | head -14
curl -s -o /dev/null -w 'health %{http_code}\n' http://127.0.0.1:8080/smsc/login
"""


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"connect {USER}@{HOST}")
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    _, stdout, stderr = ssh.exec_command(f"bash -s <<'EOF'\n{SETUP}\nEOF", timeout=90)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if out:
        print(out.encode("ascii", errors="replace").decode("ascii"))
    if err:
        print(err, file=sys.stderr)
    code = stdout.channel.recv_exit_status()
    if "health 200" not in out:
        sys.exit(1 if code != 0 else 1)
    print("user systemd smsc OK (Restart=always)")
    ssh.close()


if __name__ == "__main__":
    main()
