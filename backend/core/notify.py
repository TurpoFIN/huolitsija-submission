"""In-app notifications (bell) plus optional e-mail.

Every title and body is given in both UI languages with bi(fi, en); the bell shows the reader's UI language (the
frontend picks title_i18n / body_i18n) and the e-mail goes out in the recipient's locale:

    await notify_company(company_id, bi("Tarjous valmis", "Quote ready"), bi(f"{n} vaihtoehtoa", f"{n} options"),
                         link="/portal/quotes/<id>")
    await notify_role("customs_broker", bi("Tarkistettava ilmoitus", "Declaration to review"), ..., link="/ops/customs/<id>")

Text the user wrote (a message preview, a decision note) is the same in both: bi(note, note) or same(note).
backend/tests/test_core_display_text_fi.py fails CI on a notify_* title or body that is not bi(...) / same(...).
"""
from core.db import db
from core.email import render, send_email
from core.util import new_id, now_iso


def bi(fi: str, en: str) -> dict:
    """A notification text in Finnish and English."""
    return {"fi": fi or en or "", "en": en or fi or ""}


def same(text: str | None) -> dict:
    """User-written or source text shown as written in both languages (message previews, notes, names)."""
    return {"fi": text or "", "en": text or ""}


def _split(text) -> tuple[str, dict | None]:
    """(plain text, variants). The plain field keeps the English for older clients; variants hold both."""
    if isinstance(text, dict):
        en = text.get("en") or text.get("fi") or ""
        return en, {"fi": text.get("fi") or en, "en": en}
    return text or "", None


def lang_of(user: dict) -> str:
    return "en" if (user.get("locale") or "fi") == "en" else "fi"


async def notify_user(user: dict, title, body="", link: str | None = None, *, email: bool = False,
                      kind: str = "info", base_url: str = ""):
    title_en, title_i18n = _split(title)
    body_en, body_i18n = _split(body)
    doc = {"id": new_id(), "user_id": user["id"], "company_id": user.get("company_id"), "title": title_en,
           "body": body_en, "link": link, "kind": kind, "read": False, "created_at": now_iso()}
    if title_i18n:
        doc["title_i18n"] = title_i18n
    if body_i18n:
        doc["body_i18n"] = body_i18n
    await db.notifications.insert_one(doc)
    if email and user.get("email") and user.get("notify_email", True):
        lang = lang_of(user)
        subject = (title_i18n or {}).get(lang) or title_en
        text_body = (body_i18n or {}).get(lang) or body_en
        url = f"{base_url}{link}" if (base_url and link) else None
        button = ("Avaa Huolitsijassa" if lang == "fi" else "Open in Huolitsija") if url else None
        html, text = render(subject, [text_body] if text_body else [], url, button)
        await send_email(user["email"], subject, html, text, tag=kind, company_id=user.get("company_id"))


async def notify_company(company_id: str, title, body="", link: str | None = None, *,
                         email: bool = False, kind: str = "info", base_url: str = "", admins_only: bool = False):
    q = {"company_id": company_id, "disabled": {"$ne": True}}
    if admins_only:
        q["role"] = "customer_admin"
    async for u in db.users.find(q, {"_id": 0, "password_hash": 0}):
        await notify_user(u, title, body, link, email=email, kind=kind, base_url=base_url)


async def notify_role(role: str | list[str], title, body="", link: str | None = None, *,
                      email: bool = False, kind: str = "info", base_url: str = ""):
    roles = [role] if isinstance(role, str) else role
    async for u in db.users.find({"role": {"$in": roles + ["ops_admin"]}, "disabled": {"$ne": True}},
                                 {"_id": 0, "password_hash": 0}):
        await notify_user(u, title, body, link, email=email, kind=kind, base_url=base_url)


def eur(cents, lang: str) -> str:
    """Amount for a notification text: 1 234,50 € (fi) / €1,234.50 (en)."""
    s = f"{(cents or 0) / 100:,.2f}"
    return f"{s.replace(',', ' ').replace('.', ',')} €" if lang == "fi" else f"€{s}"


def day(iso: str | None, lang: str) -> str:
    """Date for a notification text: 24.10.2026 (fi) / 2026-10-24 (en)."""
    if not iso:
        return "—"
    y, m, d = str(iso)[:10].split("-")
    return f"{int(d)}.{int(m)}.{y}" if lang == "fi" else str(iso)[:10]
