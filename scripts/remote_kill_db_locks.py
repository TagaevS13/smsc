"""Kill interactive sqlite3 sessions locking cdr_reporting.db."""
import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")

cmd = r"""
echo 'Before:'
fuser -v /opt/smsc/cdr_reporting.db 2>/dev/null || true
pkill -f 'sqlite3.*/opt/smsc/cdr_reporting.db' 2>/dev/null && echo 'killed sqlite3 locks' || echo 'no sqlite3 to kill'
sleep 1
echo 'After:'
fuser -v /opt/smsc/cdr_reporting.db 2>/dev/null || echo 'only gunicorn (expected)'
curl -s -o /dev/null -w 'POST login %{time_total}s http=%{http_code}\n' --max-time 10 \
  -X POST -d 'username=admin&password=admin123' http://127.0.0.1:8080/smsc/login
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
_, o, _ = ssh.exec_command(cmd, timeout=30)
print(o.read().decode("utf-8", errors="replace"))
ssh.close()
