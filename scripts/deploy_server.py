"""Upload project files to 172.16.6.183 and configure systemd service."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

HOST = os.environ.get("SMSC_DEPLOY_HOST", "172.16.6.183")
USER = os.environ.get("SMSC_DEPLOY_USER", "sorbon")
PASSWORD = os.environ.get("SMSC_DEPLOY_PASSWORD", "")
REMOTE_DIR = os.environ.get("SMSC_DEPLOY_DIR", "/opt/smsc")
ROOT = Path(__file__).resolve().parents[1]

UPLOAD_FILES = [
    "app.py",
    "db.py",
    "ingestion.py",
    "manage_users.py",
    "requirements.txt",
    "config.yaml",
    "README.md",
    "templates/index.html",
    "templates/login.html",
]

SYSTEMD_UNIT = """[Unit]
Description=SMSC CDR Reporting
After=network.target

[Service]
Type=simple
User=sorbon
Group=sorbon
WorkingDirectory=/opt/smsc
EnvironmentFile=-/opt/smsc/.env
ExecStart=/opt/smsc/.venv/bin/gunicorn -w 1 -b 0.0.0.0:8080 --timeout 300 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

ENV_FILE = """SMSC_REPORTING_SECRET=smsc-prod-change-me-2026
SMSC_ADMIN_USER=admin
SMSC_ADMIN_PASSWORD=admin123
SMSC_MAX_FILES_PER_RUN=500
"""


def upload_files(sftp: paramiko.SFTPClient) -> None:
    for rel in UPLOAD_FILES:
        local = ROOT / rel
        if not local.is_file():
            print(f"skip missing {rel}")
            continue
        remote = f"{REMOTE_DIR}/{rel.replace(chr(92), '/')}"
        remote_parent = "/".join(remote.split("/")[:-1])
        try:
            sftp.stat(remote_parent)
        except OSError:
            parts = remote_parent.split("/")
            cur = ""
            for p in parts:
                if not p:
                    continue
                cur = f"{cur}/{p}" if cur else f"/{p}"
                try:
                    sftp.stat(cur)
                except OSError:
                    sftp.mkdir(cur)
        print(f"upload {rel} -> {remote}")
        sftp.put(str(local), remote)


def run_remote(ssh: paramiko.SSHClient, script: str, sudo_password: str = PASSWORD) -> None:
    print("--- remote setup ---")
    wrapped = f"echo {repr(sudo_password)} | sudo -S bash -s <<'REMOTE_SETUP'\n{script}\nREMOTE_SETUP"
    _, stdout, stderr = ssh.exec_command(wrapped, timeout=300)
    out = stdout.read().decode()
    err = stderr.read().decode()
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    if stdout.channel.recv_exit_status() != 0:
        raise SystemExit("remote setup failed")


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"connect {USER}@{HOST}")
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()
    try:
        upload_files(sftp)
        with sftp.file(f"{REMOTE_DIR}/.env", "w") as fh:
            fh.write(ENV_FILE)
        print("wrote .env")
    finally:
        sftp.close()

    sftp = ssh.open_sftp()
    service_path = "/tmp/smsc.service"
    with sftp.file(service_path, "w") as fh:
        fh.write(SYSTEMD_UNIT)
    sftp.close()

    setup = f"""
set -e
cd {REMOTE_DIR}
mkdir -p logs templates
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
cp /tmp/smsc.service /etc/systemd/system/smsc.service
systemctl daemon-reload
systemctl enable smsc
systemctl restart smsc
sleep 2
systemctl status smsc --no-pager || true
ss -tlnp | grep 8080 || true
"""
    run_remote(ssh, setup)
    ssh.close()
    print(f"done: http://{HOST}:8080")


if __name__ == "__main__":
    main()
