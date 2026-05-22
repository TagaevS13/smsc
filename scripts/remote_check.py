import os
import paramiko

HOST = "172.16.6.183"
USER = "sorbon"
PASSWORD = os.environ.get("SMSC_DEPLOY_PASSWORD", "")

cmds = [
    "ls -la /opt/smsc",
    "head -5 /opt/smsc/requirements.txt 2>/dev/null || echo no requirements",
    "systemctl list-units --type=service --all 2>/dev/null | grep -i smsc || true",
    "ps aux | grep gunicorn | grep -v grep || true",
    "ps aux | grep python | grep smsc | grep -v grep || true",
    "ss -tlnp 2>/dev/null | grep 8080 || true",
    "ls -lh /opt/smsc/cdr_reporting.db 2>/dev/null || echo no db",
    "/opt/smsc/.venv/bin/python --version 2>/dev/null || echo no venv python",
]

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASSWORD, timeout=15)
for cmd in cmds:
    _, stdout, stderr = client.exec_command(cmd)
    print("===", cmd, "===")
    out = stdout.read().decode()
    print(out if out else "(empty)")
    err = stderr.read().decode().strip()
    if err:
        print("stderr:", err)
client.close()
