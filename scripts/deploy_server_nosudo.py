"""Finish deploy without sudo: venv + gunicorn as sorbon user."""
import os
import paramiko

HOST = "172.16.6.183"
USER = "sorbon"
PASSWORD = os.environ.get("SMSC_DEPLOY_PASSWORD", "")
REMOTE_DIR = "/opt/smsc"

SETUP = f"""
set -e
cd {REMOTE_DIR}
mkdir -p logs
if [ ! -d .venv ]; then python3 -m venv .venv; fi
.venv/bin/pip install -U pip -q
.venv/bin/pip install -r requirements.txt -q
pkill -f 'gunicorn.*app:app' 2>/dev/null || true
sleep 1
nohup .venv/bin/gunicorn -w 1 -b 0.0.0.0:8080 --timeout 300 --chdir {REMOTE_DIR} app:app >> logs/gunicorn.log 2>&1 &
sleep 2
pgrep -af gunicorn || true
ss -tlnp 2>/dev/null | grep 8080 || netstat -tlnp 2>/dev/null | grep 8080 || true
curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:8080/login || true
"""

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = client.open_sftp()
with sftp.file("/tmp/smsc_start.sh", "w") as fh:
    fh.write(SETUP)
sftp.chmod("/tmp/smsc_start.sh", 0o755)
sftp.close()
_, stdout, stderr = client.exec_command("bash /tmp/smsc_start.sh", timeout=180)
print(stdout.read().decode())
err = stderr.read().decode()
if err:
    print("stderr:", err)
code = stdout.channel.recv_exit_status()
print("exit:", code)
client.close()
