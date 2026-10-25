#!/bin/bash
# Throwaway CI backend: every demo user gets the same password from CI_DEMO_PASSWORD (use a fresh DB_NAME per run).
# Usage: CI_DEMO_PASSWORD=... DB_NAME=huo_ci_1 PORT=8020 bash scripts/run-ci-backend.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ "${CI_LOCAL_SERVICES:-false}" = true ]; then
  source "$ROOT/scripts/ci-local-env.sh" || exit $?
fi
# Explicit local-only execution profile; never enabled by the application itself.
if [ "${CI_EXECUTION_TESTS:-false}" = "true" ]; then
  source "$ROOT/scripts/ci-execution-env.sh" || exit 2
fi
export DEMO_OPS_PASSWORD="$CI_DEMO_PASSWORD" DEMO_CUSTOMER_PASSWORD="$CI_DEMO_PASSWORD" DEMO_PARTNER_PASSWORD="$CI_DEMO_PASSWORD"
# CI logs in with the CI password even on a reused database (seed_core keeps app-changed passwords otherwise).
export DEMO_RESET_PASSWORDS="${DEMO_RESET_PASSWORDS:-true}"
# CI keeps the deterministic sandbox register answers for 399xxxx test companies; production asks the real registers.
export TEST_TENANT_REAL_REGISTERS="${TEST_TENANT_REAL_REGISTERS:-false}"
# This script binds plain HTTP on loopback. Secure cookies cannot be returned
# by HTTP clients, so local authenticated cookie-path suites need this default.
export COOKIE_SECURE="${COOKIE_SECURE:-false}"
# Public registers (PRH/YTJ, PRH XBRL, EU VIES/EORI) answer from recorded real responses when the exact request was
# recorded (backend/core/recorded_http.py), so CI does not depend on them being reachable. REGISTER_RECORDINGS= (empty)
# runs fully live; REGISTER_RECORD=1 re-records.
export REGISTER_RECORDINGS="${REGISTER_RECORDINGS-$ROOT/backend/tests/fixtures/registers}"
exec ${PYTHON:-python} -m uvicorn server:app --app-dir "$ROOT/backend" --host 127.0.0.1 --port "${PORT:-8020}"
