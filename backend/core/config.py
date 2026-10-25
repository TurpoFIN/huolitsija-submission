"""Runtime configuration. Every value comes from the environment (backend/.env on Emergent).

Switching an integration from test to live is a configuration change only; see
shared/s1/integrations.md for what each variable needs.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


NO_SANDBOX = ("", "none", "off", "false", "0", "-")


SHARED_MAIL_DOMAINS = ("postmarkapp.com", "smtp2go.com", "smtpservice.net", "gmail.com", "outlook.com")


def company_mail_domain(addr: str | None, sender: str | None = None) -> bool:
    """Whether addr is on a mail domain the company itself controls (TEST leads and partners on aliases such as
    marc+tag@heliosone.fi). COMPANY_MAIL_DOMAINS (comma list) names them; unset, the sender's domain counts unless it is
    a shared provider domain (an inbound.postmarkapp.com From must never make other Postmark inboxes "ours")."""
    from email.utils import parseaddr
    dom = (addr or "").strip().lower().rpartition("@")[2]
    own = {d.strip().lower() for d in env("COMPANY_MAIL_DOMAINS").split(",") if d.strip()}
    if not own:
        s = parseaddr(sender or "")[1].lower().rpartition("@")[2]
        own = {s} if s and not any(s == d or s.endswith("." + d) for d in SHARED_MAIL_DOMAINS) else set()
    return bool(dom) and dom in own


def sandbox_domain(name: str) -> str:
    """A *_SANDBOX_DOMAIN value, or "" when there is no sandbox. "none"/"off" count as unset: the host keeps a secret's
    last value when the key is deleted, so going live is done by editing the value to none."""
    v = env(name).lower()
    return "" if v in NO_SANDBOX else v


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


MONGO_URL = env("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = env("DB_NAME", "huolitsija")
CORS_ORIGINS = [o for o in env("CORS_ORIGINS", "*").split(",") if o]

# Public base URL of the web app, used in e-mails and payment redirects. When empty the
# request's Origin / Host header is used instead.
APP_BASE_URL = env("APP_BASE_URL").rstrip("/")

# Auth. If JWT_SECRET is empty a random secret is generated once and stored in MongoDB.
JWT_SECRET = env("JWT_SECRET")
SESSION_DAYS = int(env("SESSION_DAYS", "14"))
COOKIE_SECURE = env_bool("COOKIE_SECURE", True)

# Company identity printed on documents (quotes, invoices, declarations).
COMPANY_NAME = env("COMPANY_NAME", "Huolitsija Oy")
COMPANY_BUSINESS_ID = env("COMPANY_BUSINESS_ID", "")  # Y-tunnus, set after incorporation
COMPANY_VAT_ID = env("COMPANY_VAT_ID", "")
COMPANY_EORI = env("COMPANY_EORI", "")
COMPANY_ADDRESS = env("COMPANY_ADDRESS", "Helsinki, Finland")
COMPANY_EMAIL = env("COMPANY_EMAIL", "ops@huolitsija.fi")
COMPANY_PHONE = env("COMPANY_PHONE", "")
COMPANY_IBAN = env("COMPANY_IBAN", "")
COMPANY_BIC = env("COMPANY_BIC", "")

# AI: OpenAI only. All agents and the settings status use this same configuration.
OPENAI_API_KEY = env("OPENAI_API_KEY")
OPENAI_BASE_URL = env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_PROVIDER = env("LLM_PROVIDER", "openai")
# Verified against GET /v1/models on 2026-10-24. Fast calls inherit the verified main model.
LLM_MODEL = env("LLM_MODEL", "gpt-6.1-sol")
LLM_REASONING_EFFORT = env("LLM_REASONING_EFFORT")
LLM_MODEL_FAST = env("LLM_MODEL_FAST", LLM_MODEL)

# Payments (Stripe). sk_test_... in test mode, sk_live_... in production.
STRIPE_API_KEY = env("STRIPE_API_KEY")
# Emergent reserves the STRIPE_WEBHOOK_SECRET name for its own Stripe integration, so hosts there set the alias.
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET") or env("PAYMENT_WEBHOOK_SIGNING_SECRET")

# Transactional e-mail: postmark | resend | smtp | log (log = stored in email_outbox only). Production: postmark.
EMAIL_PROVIDER = env("EMAIL_PROVIDER", "log")
EMAIL_FROM = env("EMAIL_FROM", "Huolitsija <noreply@huolitsija.fi>")
POSTMARK_SERVER_TOKEN = env("POSTMARK_SERVER_TOKEN")
RESEND_API_KEY = env("RESEND_API_KEY")
SMTP2GO_API_KEY = env("SMTP2GO_API_KEY")
# Svix signing secret of the Resend webhook (email.delivered / bounced / complained → email_outbox.delivery).
RESEND_WEBHOOK_SECRET = env("RESEND_WEBHOOK_SECRET")
# Basic-auth password Postmark sends with its delivery webhooks (set in the webhook URL).
POSTMARK_WEBHOOK_TOKEN = env("POSTMARK_WEBHOOK_TOKEN")

# Text messages: twilio | log (log = stored in sms_outbox only). TWILIO_FROM_NUMBER is the Twilio number in E.164
# (TWILIO_FROM is accepted as the older name). A trial account only texts verified recipients.
SMS_PROVIDER = env("SMS_PROVIDER", "log")
TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN")
TWILIO_FROM = env("TWILIO_FROM_NUMBER") or env("TWILIO_FROM")
TWILIO_MESSAGING_SERVICE_SID = env("TWILIO_MESSAGING_SERVICE_SID")
TWILIO_STATUS_CALLBACK_URL = env("TWILIO_STATUS_CALLBACK_URL")

# Finnish Customs message exchange: disabled | test | production.
TULLI_MODE = env("TULLI_MODE", "disabled")
TULLI_IMPORT_MODE = env("TULLI_IMPORT_MODE")
TULLI_EXPORT_MODE = env("TULLI_EXPORT_MODE")
TULLI_ENDPOINT = env("TULLI_ENDPOINT")
TULLI_CLIENT_CERT = env("TULLI_CLIENT_CERT")  # path to PEM
TULLI_CLIENT_KEY = env("TULLI_CLIENT_KEY")  # path to PEM
TULLI_DECLARANT_ID = env("TULLI_DECLARANT_ID")  # message declarant id assigned by Tulli

# Demo data and the demo ops login.
SEED_DEMO = env_bool("SEED_DEMO", True)
DEMO_OPS_EMAIL = env("DEMO_OPS_EMAIL", "ops.demo@huolitsija.fi")
DEMO_OPS_PASSWORD = env("DEMO_OPS_PASSWORD")  # if empty the ops demo user is not created

# Live data sources (open data, no key needed).
DIGITRAFFIC_ENABLED = env_bool("DIGITRAFFIC_ENABLED", True)
POSITION_TICK_SECONDS = int(env("POSITION_TICK_SECONDS", "30"))

# Business rules (defaults; ops can override in Settings).
QUOTE_APPROVAL_THRESHOLD_EUR = float(env("QUOTE_APPROVAL_THRESHOLD_EUR", "15000"))
DEFAULT_PAYMENT_TERMS_DAYS = int(env("DEFAULT_PAYMENT_TERMS_DAYS", "14"))

# EDI gateway (s14): EDIFACT IFTMIN in, IFTMCS/IFTSTA out, DCSA JSON. Transports: SFTP poll (per partner record),
# AS2 (pyas2lib, signed + encrypted, sync MDN) behind its own switch, and HTTPS (HMAC per partner).
EDI_SFTP_ENABLED = env_bool("EDI_SFTP_ENABLED", False)
EDI_POLL_SECONDS = int(env("EDI_POLL_SECONDS", "60"))
EDI_OUR_UNB_ID = env("EDI_OUR_UNB_ID", "HUOLITSIJA")
EDI_AS2_ENABLED = env_bool("EDI_AS2_ENABLED", False)
EDI_AS2_ID = env("EDI_AS2_ID", "HUOLITSIJA")
EDI_AS2_KEY_PEM = env("EDI_AS2_KEY_PEM")  # PEM text or a file path (private key)
EDI_AS2_CERT_PEM = env("EDI_AS2_CERT_PEM")  # PEM text or a file path (our certificate, given to partners)
EDI_AS2_KEY_PASS = env("EDI_AS2_KEY_PASS")
