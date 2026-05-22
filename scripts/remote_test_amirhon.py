import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")
# password from deploy - read from credentials file if exists
from pathlib import Path

cred = Path(__file__).resolve().parents[1] / "deploy" / "amirhon_credentials.txt"
amirhon_pass = os.environ.get("SMSC_TEST_PASSWORD", "")
if cred.exists():
    for line in cred.read_text(encoding="utf-8").splitlines():
        if line.startswith("Password:"):
            amirhon_pass = line.split(":", 1)[1].strip()

cmd = f"""
curl -s -c /tmp/amir.c -b /tmp/amir.c -X POST -d 'username=Amirhon&password={amirhon_pass}' -o /dev/null -w 'login_post %{{http_code}}\\n' http://127.0.0.1:8080/smsc/login
curl -s -b /tmp/amir.c -o /tmp/amir.html -w 'index %{{http_code}}\\n' http://127.0.0.1:8080/smsc/
grep -c 'Import CDR from configured servers' /tmp/amir.html || true
grep -c 'Internal Server Error' /tmp/amir.html || true
echo 'banner_visible_if_import_running: check Import in progress when cron active'
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
_, o, e = ssh.exec_command(cmd, timeout=30)
print(o.read().decode())
ssh.close()
