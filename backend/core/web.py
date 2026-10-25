from fastapi import Request

from core import config


def base_url(request: Request | None) -> str:
    """Public origin of the web app for links in e-mails and payment redirects."""
    if config.APP_BASE_URL:
        return config.APP_BASE_URL
    if request is None:
        return ""
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{proto}://{host}".rstrip("/")
