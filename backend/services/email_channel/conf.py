"""E-mail channel configuration. Read at call time, so a changed environment needs no code change.

EMAIL_CHANNEL_ADDRESS        the address customers write to (default COMPANY_EMAIL)
EMAIL_CHANNEL_FROM           From: of replies (default "<COMPANY_NAME> <EMAIL_CHANNEL_ADDRESS>")
EMAIL_CHANNEL_PROVIDER       smtp | postmark | resend | log (default EMAIL_PROVIDER)
EMAIL_CHANNEL_SMTP_HOST/_PORT/_USER/_PASSWORD/_SECURITY   when the provider is smtp (core.email.smtp_settings)
EMAIL_CHANNEL_IMAP_HOST/_PORT/_USER/_PASSWORD/_SECURITY/_FOLDER   inbound mailbox polled every EMAIL_CHANNEL_POLL_SECONDS
EMAIL_CHANNEL_WEBHOOK_SECRET HMAC key of POST /api/email-channel/inbound (raw RFC 822 or Postmark inbound JSON)
EMAIL_CHANNEL_POSTMARK_TOKEN basic-auth password (user postmark) of native Postmark inbound webhooks
EMAIL_CHANNEL_CLOUDMAILIN_TOKEN basic-auth password (user cloudmailin) of CloudMailin "JSON (Raw)" inbound posts
EMAIL_CHANNEL_INBOUND_REQUIRE_AUTH  true = mail whose Authentication-Results lacks dmarc/dkim/spf=pass goes to the owner
EMAIL_CHANNEL_LINK_DAYS      lifetime of the portal links in our mails (default 14)
"""
from email.utils import parseaddr

from core import config


def address() -> str:
    return (config.env("EMAIL_CHANNEL_ADDRESS") or config.COMPANY_EMAIL).lower()


def sender() -> str:
    return config.env("EMAIL_CHANNEL_FROM") or config.EMAIL_FROM


def domain() -> str:
    return (parseaddr(sender())[1].split("@") + ["huolitsija.fi"])[1].lower()


def provider() -> str:
    return config.env("EMAIL_CHANNEL_PROVIDER") or config.EMAIL_PROVIDER


def smtp() -> dict | None:
    from core.email import smtp_settings

    s = smtp_settings("EMAIL_CHANNEL_SMTP")
    return s if s.get("host") else None


def imap() -> dict | None:
    host = config.env("EMAIL_CHANNEL_IMAP_HOST")
    if not host:
        return None
    security = config.env("EMAIL_CHANNEL_IMAP_SECURITY", "ssl").lower()
    return {"host": host, "port": int(config.env("EMAIL_CHANNEL_IMAP_PORT") or (993 if security == "ssl" else 143)),
            "user": config.env("EMAIL_CHANNEL_IMAP_USER"), "password": config.env("EMAIL_CHANNEL_IMAP_PASSWORD"),
            "security": security, "folder": config.env("EMAIL_CHANNEL_IMAP_FOLDER", "INBOX")}


def poll_seconds() -> float:
    try:
        return max(5.0, float(config.env("EMAIL_CHANNEL_POLL_SECONDS", "30")))
    except ValueError:
        return 30.0


def webhook_secret() -> str:
    return config.env("EMAIL_CHANNEL_WEBHOOK_SECRET")


def require_auth() -> bool:
    return config.env_bool("EMAIL_CHANNEL_INBOUND_REQUIRE_AUTH", False)


def link_days() -> int:
    try:
        return max(1, int(config.env("EMAIL_CHANNEL_LINK_DAYS", "14")))
    except ValueError:
        return 14


def postmark_token() -> str:
    """Basic-auth password for native Postmark inbound webhooks (username postmark)."""
    return config.env("EMAIL_CHANNEL_POSTMARK_TOKEN")


def cloudmailin_token() -> str:
    """Basic-auth password for CloudMailin inbound posts, JSON (Raw) format (username cloudmailin)."""
    return config.env("EMAIL_CHANNEL_CLOUDMAILIN_TOKEN")
