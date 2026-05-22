import os
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("172.16.6.183", username="sorbon", password=os.environ.get("SMSC_DEPLOY_PASSWORD", ""))
paths = ["/", "/smsc/", "/smsc/login", "/login"]
for path in paths:
    cmd = f"curl -s -o /dev/null -w '%{{http_code}}' -L http://127.0.0.1:8080{path}"
    _, o, _ = c.exec_command(cmd)
    code = o.read().decode().strip()
    print(f"{path} -> {code}")
c.close()
