"""Deploy cron import, admin roles, Amirhon user; restart smsc."""
from __future__ import annotations

import secrets
from pathlib import Path

import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")
REMOTE = "/opt/smsc"
ROOT = Path(__file__).resolve().parents[1]

FILES = [
    "app.py",
    "db.py",
    "import_runner.py",
    "import_once.py",
    "manage_users.py",
    "templates/index.html",
    "templates/login.html",
]

AMIRHON_PASSWORD = secrets.token_urlsafe(14)

SETUP = f"""
set -e
cd {REMOTE}
export SMSC_CDR_RETENTION_DAYS=90

# User crontab: incremental import every 10 minutes
(crontab -l 2>/dev/null | grep -v 'import_once.py' || true
 echo '*/10 * * * * cd {REMOTE} && .venv/bin/python import_once.py >> {REMOTE}/logs/cron-import.log 2>&1') | crontab -

.venv/bin/python -c "from db import create_session_factory; create_session_factory('sqlite:////opt/smsc/cdr_reporting.db')"

.venv/bin/python manage_users.py add Amirhon --password '{AMIRHON_PASSWORD}' 2>/dev/null || \\
  .venv/bin/python manage_users.py set-password Amirhon --password '{AMIRHON_PASSWORD}'

.venv/bin/python manage_users.py list

export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user restart smsc
sleep 2
systemctl --user is-active smsc
echo AMIRHON_PASSWORD={AMIRHON_PASSWORD}
"""


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"connect {USER}@{HOST}")
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()
    for rel in FILES:
        local = ROOT / rel
        sftp.put(str(local), f"{REMOTE}/{rel}")
    sftp.close()

    _, stdout, stderr = ssh.exec_command(
        f"bash -s <<'EOF'\n{SETUP}\nEOF", timeout=120
    )
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    print(out.encode("ascii", errors="replace").decode("ascii"))
    if err.strip():
        print("stderr:", err[:800])
    ssh.close()

    cred_path = ROOT / "deploy" / "amirhon_credentials.txt"
    cred_path.write_text(
        f"User: Amirhon\nPassword: {AMIRHON_PASSWORD}\nURL: http://172.16.6.183:8080/smsc/login\n",
        encoding="utf-8",
    )
    print(f"\nCredentials saved to: {cred_path}")
    print(f"Amirhon password: {AMIRHON_PASSWORD}")


if __name__ == "__main__":
    main()
