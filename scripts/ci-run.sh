#!/bin/bash
# Runs each lane's test file in its own pytest process (each binds the Motor client to its own event loop)
# against one combined backend started with scripts/run-ci-backend.sh.
# Usage: CI_DEMO_PASSWORD=... bash scripts/ci-run.sh <port> <db> <outdir>
PORT=$1; DB=$2; OUT=$3
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PORT DB_NAME="$DB"
if [ "${CI_LOCAL_SERVICES:-false}" = true ]; then
  source "$ROOT/scripts/ci-local-env.sh" || exit $?
fi
# Refuse a run against an unready backend: API suites otherwise fail connection
# checks or skip entire modules, producing misleading totals.
if ! curl --fail --silent --show-error --max-time 10 "http://localhost:$PORT/api/health" >/dev/null; then
  echo "CI backend is not ready on port $PORT; start run-ci-backend.sh and wait for application startup." >&2
  exit 2
fi
# API health alone is insufficient: credit, insurance and tender flows need a live job worker.
if ! timeout 30 ${PYTHON:-python} - "$DB" <<'PY_WORKER'
import os
import sys
import time
from datetime import datetime, timezone
from pymongo import MongoClient

since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
with MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"),
                 serverSelectionTimeoutMS=5000) as client:
    workers = client[sys.argv[1]].worker_heartbeats
    for attempt in range(20):
        if workers.find_one({"beat_at": {"$gte": since}}):
            break
        time.sleep(1)
    else:
        sys.exit("No current job-worker heartbeat; start the CI backend with JOBS_WORKER_ENABLED=true.")
PY_WORKER
then
  echo "CI worker is not ready; no test files were run." >&2
  exit 2
fi
failed_files=0
ran_files=0
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$OUT"
export PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"
export DEMO_OPS_PASSWORD="$CI_DEMO_PASSWORD" DEMO_CUSTOMER_PASSWORD="$CI_DEMO_PASSWORD" DEMO_PARTNER_PASSWORD="$CI_DEMO_PASSWORD"
export V3_DEMO_PASSWORD="$CI_DEMO_PASSWORD" DB_NAME=$DB
# in-process service calls replay the same recorded register answers as the CI backend (see run-ci-backend.sh)
export REGISTER_RECORDINGS="${REGISTER_RECORDINGS-$ROOT/backend/tests/fixtures/registers}"
for v in 1 2 3 4 7; do export V${v}_API=http://localhost:$PORT/api V${v}_DB=$DB; done
# step suites (test_s<N>_*.py) read S<N>_API / S<N>_DB
for v in $(seq 2 20); do export S${v}_API=http://localhost:$PORT/api S${v}_DB=$DB; done  # s14 server needs WEBHOOKS_ALLOW_PRIVATE=true for its webhook receiver
# s12 suites also read S12_DEMO_PASSWORD / S12_PASSWORD
export S12_DEMO_PASSWORD="$CI_DEMO_PASSWORD" S12_PASSWORD="$CI_DEMO_PASSWORD"
for f in "$ROOT"/backend/tests/test_v[1-4]_*.py "$ROOT"/backend/tests/test_core_*.py "$ROOT"/backend/tests/test_s[0-9]*_*.py; do
  case "$f" in
    *_support.py) continue;;
    */test_s13_pay_bank_api.py)
      echo "Excluded test_s13_pay_bank_api: bank operations require a separately selected run."
      continue;;
  esac
  n=$(basename "$f" .py)
  domain_db=$DB
  case "$n" in test_core_domain*) domain_db="${DB}_domain";; esac
  DB_NAME=$domain_db timeout 900 ${PYTHON:-python} -m pytest -q -p no:cacheprovider --rootdir "$ROOT/backend" "$f" > "$OUT/$n.txt" 2>&1
  result=$?
  ran_files=$((ran_files + 1))
  if [ "$result" -ne 0 ]; then failed_files=$((failed_files + 1)); fi
  echo "$n exit=$result $(tail -1 "$OUT/$n.txt")"
done

echo "CI files: $ran_files; failed files: $failed_files"
[ "$failed_files" -eq 0 ]
