import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")

cmds = [
    "ps -fp 80705,80706,81724 2>/dev/null || ps aux | grep -E 'python|gunicorn' | grep -v grep",
    "tail -5 /opt/smsc/logs/import.log 2>/dev/null; wc -l /opt/smsc/logs/import.log",
    "curl -s -o /dev/null -w 'favicon time=%{time_total}s code=%{http_code}\\n' --max-time 10 http://127.0.0.1:8080/favicon.ico",
    "curl -s -o /dev/null -w 'root time=%{time_total}s code=%{http_code}\\n' --max-time 10 -L http://127.0.0.1:8080/",
    "curl -s -o /dev/null -w 'POST login time=%{time_total}s code=%{http_code}\\n' --max-time 65 -X POST -d 'username=admin&password=admin123' http://127.0.0.1:8080/smsc/login",
]

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
for cmd in cmds:
    print("===", cmd[:75], "===")
    _, o, e = ssh.exec_command(cmd, timeout=70)
    print(o.read().decode("utf-8", errors="replace") or "(empty)")
ssh.close()
