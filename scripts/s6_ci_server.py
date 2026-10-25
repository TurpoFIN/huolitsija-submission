"""Start isolated CI API without printing or using stored demo credentials."""
import os,json,sys,secrets
from pathlib import Path
r=Path(__file__).resolve().parents[1]
secret_path=r/'.local-ci.json'
if not secret_path.exists():
 with os.fdopen(os.open(secret_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f: json.dump({'CI_DEMO_PASSWORD':secrets.token_urlsafe(24)},f)
e=os.environ.copy();e.update(json.loads((r/'.local-ci.json').read_text()))
e.update(DB_NAME=os.environ.get('DB_NAME','huo_s6_ci'),PORT=os.environ.get('PORT','8106'),PYTHON=str(r/'.venv/bin/python'),EMAIL_PROVIDER='log',LLM_PROVIDER='none',
 EMAIL_CHANNEL_WORKER='true',JOBS_WORKER_ENABLED='true',WEBHOOKS_ALLOW_PRIVATE='true',COOKIE_SECURE='false')
os.execve('/bin/bash',['bash',str(r/'scripts/run-ci-backend.sh')],e)
