import os
import paramiko
import time

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)

cmds = [
    "curl -s -o /dev/null -w 'login time_total=%{time_total}s http=%{http_code}\\n' --max-time 15 http://127.0.0.1:8080/smsc/login",
    "curl -s -o /dev/null -w 'index time_total=%{time_total}s http=%{http_code}\\n' --max-time 15 http://127.0.0.1:8080/smsc/",
    "tail -20 /opt/smsc/logs/app.log 2>/dev/null || echo no app.log",
    "export XDG_RUNTIME_DIR=/run/user/$(id -u); journalctl --user -u smsc -n 15 --no-pager 2>/dev/null | tail -15",
    "fuser /opt/smsc/cdr_reporting.db 2>/dev/null || lsof /opt/smsc/cdr_reporting.db 2>/dev/null | head -5 || echo db not locked info",
]

for cmd in cmds:
    print("===", cmd[:70], "===")
    _, o, e = ssh.exec_command(cmd, timeout=25)
    print(o.read().decode("utf-8", errors="replace") or "(empty)")
    err = e.read().decode().strip()
    if err:
        print("err:", err[:200])

ssh.close()
