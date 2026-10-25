#!/bin/bash
# Opt-in local integration services. Source in BOTH the API and pytest processes.
# AP mail helper provisions and checks local SMTP/IMAP.
# Never source this file in a deployed environment.
case "${DB_NAME:-}" in
  huo_ci_*) ;;
  *) echo 'Local CI requires a dedicated DB_NAME beginning huo_ci_' >&2; return 2;;
esac
: "${CI_DEMO_PASSWORD:?Set a local CI demo password}"
: "${JWT_SECRET:?Set the same local JWT_SECRET for API and tests}"
export SEED_DEMO=true COOKIE_SECURE=false
# Inline execution tests own the drain; asynchronous suites retain a worker.
export JOBS_WORKER_ENABLED="${CI_BACKGROUND_WORKER:-true}"
export APP_BASE_URL="http://127.0.0.1:${PORT:-8020}"
export CI_AP_MAIL_DB="$DB_NAME"
source "$(dirname "${BASH_SOURCE[0]}")/ci-ap-mail.sh" || return $?
export EXECUTION_TEST_API_ENABLED=true
export EXECUTION_SMTP_HOST=127.0.0.1 EXECUTION_SMTP_PORT=3025 EXECUTION_SMTP_SECURITY=none
export EXECUTION_IMAP_HOST=127.0.0.1 EXECUTION_IMAP_PORT=3143 EXECUTION_IMAP_SECURITY=none
export EXECUTION_IMAP_USER=toteutus@huolitsija.test EXECUTION_IMAP_PASSWORD=sandbox
export EXECUTION_FROM=toteutus@huolitsija.test EXECUTION_SANDBOX_DOMAIN=sandbox.test
export CARRIER_HAPAG_BOOKING_DESK_EMAIL=si-desk@hapag-lloyd.sandbox.test
export CRM_EMAIL_PROVIDER=smtp CRM_EMAIL_FROM=myynti@huolitsija.test CRM_SANDBOX_DOMAIN=prospects.sandbox.test
export CRM_IMAP_HOST=127.0.0.1 CRM_IMAP_PORT=3143 CRM_IMAP_SECURITY=none
export CRM_IMAP_USER=myynti@huolitsija.test CRM_IMAP_PASSWORD=sandbox
# No bank execution or obsolete insurance stub is enabled by this harness.
unset HUOLITSIJA_ALLOW_BANK_UPLOAD INSURANCE_PROVIDER
${PYTHON:-python} - <<'PY'
import importlib
import socket
for name in ('openapi_schema_validator', 'rdflib', 'yaml'):
    importlib.import_module(name)
for port in (3025, 3143):
    with socket.create_connection(('127.0.0.1', port), timeout=5):
        pass
PY
