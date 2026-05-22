import os
import paramiko

HOST, USER, PASSWORD = "172.16.6.183", "sorbon", os.environ.get("SMSC_DEPLOY_PASSWORD", "")
cmds = [
    "tail -80 /opt/smsc/logs/app.log 2>/dev/null || echo no app log",
    "export XDG_RUNTIME_DIR=/run/user/1000; journalctl --user -u smsc -n 50 --no-pager 2>/dev/null | tail -50",
    "pgrep -a sqlite3 || echo no sqlite3",
    "curl -s -o /dev/null -w 'login %{http_code}\\n' http://127.0.0.1:8080/smsc/login",
    "curl -s -o /dev/null -w 'index %{http_code}\\n' -b /tmp/c.txt -c /tmp/c.txt http://127.0.0.1:8080/smsc/ 2>/dev/null; curl -s -w 'index_auth %{http_code}\\n' -b /tmp/c.txt http://127.0.0.1:8080/smsc/ -o /dev/null | tail -1",
]
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
for cmd in cmds:
    print("===", cmd[:70])
    _, o, e = ssh.exec_command(cmd, timeout=40)
    print(o.read().decode("utf-8", errors="replace")[:5000])
ssh.close()
