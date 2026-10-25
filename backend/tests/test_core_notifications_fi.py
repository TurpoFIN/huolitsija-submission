"""Notifications in both UI languages (s1 review r3, N4): what notify_* stores, the e-mail language, and the one-time
backfill of notifications stored before. Uses a throwaway MongoDB database (dropped at the end); no backend needed.
The stored samples are the ones the reviewer read on live (evidence/s1-review/r3/checks/notifications-customer.txt)."""
import asyncio
import os
import uuid

from motor.motor_asyncio import AsyncIOMotorClient

from core import notify
from services import notification_backfill as nb

MONGO = os.environ.get("MONGO_URL", "mongodb://localhost:27017")


def _with_db(fn):
    async def run():
        client = AsyncIOMotorClient(MONGO)
        name = f"huo_test_n4_{uuid.uuid4().hex[:8]}"
        try:
            return await fn(client[name])
        finally:
            await client.drop_database(name)
            client.close()
    return asyncio.run(run())


def test_notify_stores_both_languages_and_mails_in_the_recipients_language(monkeypatch):
    sent = []

    async def capture(to, subject, html, text, **kw):
        sent.append((to, subject, text))
        return {"to": to, "status": "logged"}

    monkeypatch.setattr(notify, "send_email", capture)

    async def scenario(db):
        monkeypatch.setattr(notify, "db", db)
        fi_user = {"id": "u-fi", "email": "fi@example.test", "locale": "fi", "company_id": "c1"}
        en_user = {"id": "u-en", "email": "en@example.test", "locale": "en", "company_id": "c1"}
        for u in (fi_user, en_user):
            await notify.notify_user(u, notify.bi("Tulli-ilmoitus luonnosteltu", "Customs declaration drafted"),
                                     notify.bi("Tarkista lähetyksen HUO-1 tavaratiedot", "Check the goods data for HUO-1"),
                                     "/portal/customs/x", email=True, base_url="https://app.test")
        return [d async for d in db.notifications.find({}, {"_id": 0}).sort("user_id", 1)]

    docs = _with_db(scenario)
    en_doc, fi_doc = docs
    assert fi_doc["title"] == "Customs declaration drafted"  # plain field keeps the English for older clients
    assert fi_doc["title_i18n"] == {"fi": "Tulli-ilmoitus luonnosteltu", "en": "Customs declaration drafted"}
    assert fi_doc["body_i18n"]["fi"] == "Tarkista lähetyksen HUO-1 tavaratiedot"
    assert en_doc["title_i18n"] == fi_doc["title_i18n"]
    by_to = {to: (subject, text) for to, subject, text in sent}
    assert by_to["fi@example.test"][0] == "Tulli-ilmoitus luonnosteltu" and "Avaa Huolitsijassa" in by_to["fi@example.test"][1]
    assert by_to["en@example.test"][0] == "Customs declaration drafted" and "Open in Huolitsija" in by_to["en@example.test"][1]


def test_reviewer_samples_get_finnish():
    assert nb.title_variants("Customs declaration drafted")["fi"] == "Tulli-ilmoitus luonnosteltu"
    assert nb.body_variants("Check the goods data for HUO-2026-000061")["fi"] == "Tarkista lähetyksen HUO-2026-000061 tavaratiedot"
    assert nb.title_variants("Released by Customs: HUO-2026-000053")["fi"] == "Tulli luovutti: HUO-2026-000053"
    assert nb.title_variants("New booking HUO-2026-000058")["fi"] == "Uusi varaus HUO-2026-000058"
    assert nb.title_variants("26FI0000000000001 ready for review")["fi"] == "26FI0000000000001 valmis tarkistettavaksi"
    assert nb.body_variants("Review and issue the invoice.")["fi"] == "Tarkista ja lähetä lasku."
    assert nb.body_variants("E2E Testi Oy confirmed the goods data")["fi"] == "E2E Testi Oy vahvisti tavaratiedot"


def test_finnish_only_notifications_get_english():
    assert nb.title_variants("Lasku HUO-INV-2026-000018 – Huolitsija Oy")["en"] == "Invoice HUO-INV-2026-000018 from Huolitsija Oy"
    assert nb.body_variants("Lasku HUO-INV-2026-000018 lähetykselle HUO-2026-000053.")["en"] == \
        "Invoice HUO-INV-2026-000018 for shipment HUO-2026-000053."
    assert nb.title_variants("Reklamaatio HUO-CLM-2026-000001: hyväksytty")["en"] == "Claim HUO-CLM-2026-000001: approved"
    assert nb.body_variants("Kiitos, vastaanotimme maksun 1 703,22 € laskulle HUO-INV-2026-000022.")["en"] == \
        "Thank you, we received €1,703.22 for invoice HUO-INV-2026-000022."
    assert nb.title_variants("HUO-2026-000053: tilannepäivitys")["en"] == "HUO-2026-000053: status update"


def test_backfill_matches_what_the_call_sites_now_write():
    """The backfilled Finnish for an old English notification equals what the converted call site stores today."""
    pairs = [
        (("Claim HUO-CLM-1 submitted", "Pohjolan Kaluste Oy · damage · 1860.00 EUR"),
         ("Reklamaatio HUO-CLM-1 jätetty", "Pohjolan Kaluste Oy · vaurio · 1 860,00 €")),
        (("Overdue: HUO-INV-1", "Pohjolan Kaluste Oy · €1,703.22 was due 2026-10-20"),
         ("Erääntynyt: HUO-INV-1", "Pohjolan Kaluste Oy · 1 703,22 €, eräpäivä 20.10.2026")),
        (("Customs draft LRN-1 (HUO-1)", "3 goods items drafted for Pohjolan Kaluste Oy"),
         ("Tulli-ilmoitusluonnos LRN-1 (HUO-1)", "3 tavaranimikettä luonnosteltu: Pohjolan Kaluste Oy")),
        (("Quote to review: Q-1", "Kaunas → Lahti — total above the approval threshold, dangerous goods"),
         ("Tarkistettava tarjous: Q-1", "Kaunas → Lahti — summa ylittää hyväksyntärajan, vaarallisia aineita")),
        (("Delivered: HUO-1", "HUO-1 was delivered to Lahti."), ("Toimitettu: HUO-1", "HUO-1 on toimitettu kohteeseen Lahti.")),
    ]
    for (title, body), (fi_title, fi_body) in pairs:
        assert (nb.title_variants(title)["fi"], nb.body_variants(body)["fi"]) == (fi_title, fi_body)
    assert notify.eur(186000, "fi") == "1 860,00 €" and notify.day("2026-10-20", "fi") == "20.10.2026"


def test_backfill_updates_stored_notifications_once_and_leaves_user_text():
    async def scenario(db):
        from services import notification_backfill as mod

        orig = mod.db
        mod.db = db
        try:
            await db.exceptions.insert_one({"id": "ex1", "title": "No position update for 50 h",
                                            "title_i18n": {"en": "No position update for 50 h", "fi": "Ei sijaintipäivitystä 50 tuntiin"}})
            await db.notifications.insert_many([
                {"id": "n1", "title": "Customs declaration drafted", "body": "Check the goods data for HUO-2026-000061"},
                {"id": "n2", "title": "Medium exception: HUO-2026-000061", "body": "No position update for 50 h",
                 "link": "/ops/exceptions?id=ex1"},
                {"id": "n3", "title": "Pohjolan Kaluste Oy: Kysymys toimituksesta", "body": "Milloin kontti saapuu?"},
            ])
            await mod.backfill_notification_variants()
            await mod.backfill_notification_variants()  # marker: second run does nothing
            docs = {d["id"]: d async for d in db.notifications.find({}, {"_id": 0})}
            marker = await db.system.find_one({"key": "r3-notification-variants:1"}, {"_id": 0})
            return docs, marker, await db.system.count_documents({})
        finally:
            mod.db = orig

    docs, marker, markers = _with_db(scenario)
    assert docs["n1"]["title_i18n"]["fi"] == "Tulli-ilmoitus luonnosteltu"
    assert docs["n1"]["body_i18n"]["fi"] == "Tarkista lähetyksen HUO-2026-000061 tavaratiedot"
    assert docs["n2"]["title_i18n"]["fi"] == "Poikkeama (keskitaso): HUO-2026-000061"
    assert docs["n2"]["body_i18n"]["fi"] == "Ei sijaintipäivitystä 50 tuntiin"
    assert "title_i18n" not in docs["n3"] and "body_i18n" not in docs["n3"]  # customer's own words stay as written
    assert docs["n1"]["title"] == "Customs declaration drafted"  # plain fields unchanged
    assert (marker["updated"], marker["unknown_titles"], markers) == (2, 1, 1)
