#!/usr/bin/env bash
# Source before BOTH the CI backend and pytest/ci-run.sh, in the same shell.
# Real SMTP/IMAP protocol fixture; no external delivery. Production never sources this.
# CI_AP_MAIL_DB (or DB_NAME) gives each concurrent CI backend a separate inbox.
ci_ap_mail_setup() {
  local ci_mail_db="${CI_AP_MAIL_DB:-${DB_NAME:-}}"
  if [ -z "$ci_mail_db" ]; then
    echo 'CI AP mail needs CI_AP_MAIL_DB or DB_NAME to isolate its inbox.' >&2
    return 2
  fi
  export SMTP_HOST=127.0.0.1 SMTP_PORT=3025 SMTP_SECURITY=none
  export EMAIL_PROVIDER=smtp AP_EMAIL_PROVIDER=smtp
  export AP_SMTP_HOST=127.0.0.1 AP_SMTP_PORT=3025 AP_SMTP_SECURITY=none
  export AP_IMAP_HOST=127.0.0.1 AP_IMAP_PORT=3143 AP_IMAP_SECURITY=none
  export AP_IMAP_USER="ostolaskut+${ci_mail_db}@huolitsija.test"
  export AP_IMAP_PASSWORD=ci-local-only AP_INBOUND_REQUIRE_AUTH=false
  export S13_SMTP_HOST="$SMTP_HOST" S13_SMTP_PORT="$SMTP_PORT"
  export S13_IMAP_HOST="$AP_IMAP_HOST" S13_IMAP_PORT="$AP_IMAP_PORT"
  export S13_AP_MAILBOX="$AP_IMAP_USER" S13_IMAP_PASSWORD="$AP_IMAP_PASSWORD"
  unset SMTP_USER SMTP_PASSWORD AP_SMTP_USER AP_SMTP_PASSWORD HUOLITSIJA_ALLOW_BANK_UPLOAD
  local ci_mail_container=huolitsija-ci-ap-mail
  if ! "${PYTHON:-python}" - <<'PY'
import imaplib, smtplib
try:
    with smtplib.SMTP('127.0.0.1',3025,timeout=3) as smtp:
        assert smtp.noop()[0] == 250
    with imaplib.IMAP4('127.0.0.1',3143,timeout=3) as imap:
        assert b'GreenMail' in imap.welcome
except (OSError, AssertionError, smtplib.SMTPException, imaplib.IMAP4.error):
    raise SystemExit(1)
PY
  then
    if timeout 15 docker container inspect "$ci_mail_container" >/dev/null 2>&1; then
      timeout 30 docker start "$ci_mail_container" >/dev/null || return 2
    else
      timeout 120 docker run -d --name "$ci_mail_container" \
        -p 127.0.0.1:3025:3025 -p 127.0.0.1:3143:3143 \
        -e 'GREENMAIL_OPTS=-Dgreenmail.setup.test.smtp -Dgreenmail.setup.test.imap -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled' \
        greenmail/standalone:2.1.3 >/dev/null || return 2
    fi
  fi
  "${PYTHON:-python}" - <<'PY'
import imaplib, os, smtplib, time
for attempt in range(30):
    try:
        with smtplib.SMTP(os.environ['SMTP_HOST'],int(os.environ['SMTP_PORT']),timeout=2) as smtp:
            assert smtp.noop()[0] == 250
        with imaplib.IMAP4(os.environ['AP_IMAP_HOST'],int(os.environ['AP_IMAP_PORT']),timeout=2) as imap:
            assert b'GreenMail' in imap.welcome
            imap.login(os.environ['AP_IMAP_USER'],os.environ['AP_IMAP_PASSWORD'])
            assert imap.select('INBOX')[0] == 'OK'
        print('CI AP mail ready: local SMTP and IMAP; isolated inbox; bank upload disabled.')
        break
    except (OSError, AssertionError, smtplib.SMTPException, imaplib.IMAP4.error):
        if attempt == 29:
            raise SystemExit('CI AP mail not ready on loopback 3025/3143. Do not run the AP suites.')
        time.sleep(0.5)
PY
}
ci_ap_mail_setup
