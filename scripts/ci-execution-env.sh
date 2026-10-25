#!/bin/bash
# Source only for a throwaway local CI backend. The s9 tests use these exact
# GreenMail ports/mailboxes. Intentionally override inherited execution settings
# so this profile cannot send mail to a configured production SMTP server.
if [ -z "${CI_DEMO_PASSWORD:-}" ] || [ -z "${DB_NAME:-}" ]; then
  echo 'Execution CI requires CI_DEMO_PASSWORD and a throwaway DB_NAME.' >&2
  return 2
fi

export EXECUTION_TEST_API_ENABLED=true
export EXECUTION_SMTP_HOST=127.0.0.1 EXECUTION_SMTP_PORT=3025 EXECUTION_SMTP_SECURITY=none
export EXECUTION_SMTP_USER='' EXECUTION_SMTP_PASSWORD=''
export EXECUTION_IMAP_HOST=127.0.0.1 EXECUTION_IMAP_PORT=3143 EXECUTION_IMAP_SECURITY=none
export EXECUTION_IMAP_USER=toteutus@huolitsija.test EXECUTION_IMAP_PASSWORD="$CI_DEMO_PASSWORD"
export EXECUTION_IMAP_FOLDER=INBOX
export EXECUTION_FROM=toteutus@huolitsija.test EXECUTION_DOMAIN=huolitsija.test
export EXECUTION_SANDBOX_DOMAIN=sandbox.test
export CARRIER_HAPAG_SI_MODE=email CARRIER_HAPAG_BOOKING_DESK_EMAIL=si-desk@hapag-lloyd.sandbox.test

# Fail before seeding/starting an API that would silently log instead of deliver
# mail. GreenMail's auth-disabled profile accepts the disposable CI password.
"${PYTHON:-python}" - <<'PY'
import imaplib
import os
import smtplib
import sys

try:
    with smtplib.SMTP("127.0.0.1", 3025, timeout=5) as smtp:
        if smtp.noop()[0] != 250:
            raise RuntimeError("SMTP not ready")
    with imaplib.IMAP4("127.0.0.1", 3143, timeout=5) as imap:
        imap.login(os.environ["EXECUTION_IMAP_USER"], os.environ["EXECUTION_IMAP_PASSWORD"])
except (OSError, RuntimeError, smtplib.SMTPException, imaplib.IMAP4.error):
    print("Execution CI requires local GreenMail SMTP :3025 and IMAP :3143 (auth disabled).", file=sys.stderr)
    sys.exit(2)
PY
