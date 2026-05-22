import os
import paramiko

HOST = "172.16.6.183"
USER = "sorbon"
PASSWORD = os.environ.get("SMSC_DEPLOY_PASSWORD", "")

cmds = [
    "hostname; uptime",
    "pgrep -af gunicorn || echo 'gunicorn: not running'",
    "ss -tlnp 2>/dev/null | grep 8080 || echo 'port 8080: not listening'",
    "curl -s -o /dev/null -w 'HTTP /smsc/login -> %{http_code}\n' http://127.0.0.1:8080/smsc/login",
    "ls -lh /opt/smsc/cdr_reporting.db /opt/smsc/.env 2>/dev/null",
    "systemctl is-active smsc 2>/dev/null || echo 'system smsc: not configured'",
    "export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user is-active smsc 2>/dev/null || echo 'user smsc: inactive'",
    "export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user status smsc --no-pager 2>/dev/null | head -12 || true",
    "python3 -c \"import sqlite3; c=sqlite3.connect('/opt/smsc/cdr_reporting.db'); r=c.execute('select sqlite_version()').fetchone(); print('SQLite version:', r[0]); c.close()\"",
]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=15)
for cmd in cmds:
    print("===", cmd[:60], "===")
    _, o, e = c.exec_command(cmd, timeout=20)
    print(o.read().decode() or "(empty)")
    err = e.read().decode().strip()
    if err:
        print("stderr:", err[:300])
c.close()
