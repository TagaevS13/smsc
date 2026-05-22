# Deploy scripts

SSH credentials via environment (never commit passwords):

```bash
set SMSC_DEPLOY_HOST=172.16.6.183
set SMSC_DEPLOY_USER=sorbon
set SMSC_DEPLOY_PASSWORD=your_ssh_password
python scripts/deploy_app_remote.py
```
