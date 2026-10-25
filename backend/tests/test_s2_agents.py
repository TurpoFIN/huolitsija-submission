"""s2 Autopilot operations agents, in process against a real MongoDB (own database, dropped at the end).

Every job kind is driven through core.jobs.work_once and checked by its effect in the database: the agent decision
log (agent_decisions), the owner queue (approvals), and the record the agent works on (company, quote, shipment,
carrier booking, customs declaration, feed event, exception, invoice, claim, document, message thread).

External submissions: none. Sandbox test companies (Business ID in the reserved 399xxxx block, test_tenant)
answer from the sandboxes in core.sandbox / services.tulli_sandbox / services.carrier_booking.SandboxAdapter, and
no AI provider is configured (core.llm raises LLMUnavailable), so agents must hand work to a person or use their
rule-based path. The sanctions lists are copied from a local database that downloaded the official EU, UN and OFAC
files (services.screening.refresh_lists); with no such copy the lists are downloaded the same way. The SDR rate
row is a fixture (IMF not fetched).
TARIC and COL use hash-checked official captures. Commercial documents and supplier
statements describe synthetic sandbox goods; no official licence is fabricated.
"""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_agents_{uuid.uuid4().hex[:8]}"
os.environ.setdefault("EMAIL_PROVIDER", "log")
os.environ["SIMULATOR_ENABLED"] = "true"
os.environ["SIMULATOR_STEP_SECONDS"] = "1"
os.environ["TULLI_SANDBOX_RELEASE_SECONDS"] = "0"
os.environ["TULLI_SANDBOX_DECISION_SECONDS"] = "0"
os.environ["DIGITRAFFIC_ENABLED"] = "false"
os.environ["TRACKING_WEBHOOK_SECRET"] = "test-secret"
os.environ["COMPANY_EORI"] = "FI3990002-2"
os.environ["COMPANY_BUSINESS_ID"] = "3990002-2"
os.environ["DELIVERY_POD_WAIT_HOURS"] = "2"
os.environ.pop("CARRIER_BOOKING_MODE", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("EMERGENT_LLM_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
import asyncio  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
from datetime import timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402

OWNER = "pytest-s2-agents"
OPS = {"id": "u-ops", "name": "Olli Ops", "email": "olli@huolitsija.test", "org_type": "internal", "role": "ops_admin",
       "locale": "fi"}
CUSTOMER = {"id": "u-sbx", "name": "Aino Asiakas", "email": "aino@sandbox-tuonti.test", "org_type": "customer",
            "role": "customer_admin", "company_id": "co-sbx", "locale": "fi"}
FIHEL = {"unlocode": "FIHEL", "code": "FIHEL", "name": "Helsinki", "country": "FI", "lat": 60.21, "lon": 25.19}
DEHAM = {"unlocode": "DEHAM", "code": "DEHAM", "name": "Hamburg", "country": "DE", "lat": 53.53, "lon": 9.93}
USNYC = {"unlocode": "USNYC", "code": "USNYC", "name": "New York", "country": "US", "lat": 40.68, "lon": -74.04}
CHAIRS = "Wooden office chairs with upholstered seats"
SDR_EUR = 1.2  # EUR per SDR in the fixture row
W: dict = {}
FIX = Path(__file__).parent / "fixtures"
EVIDENCE = Path(os.environ.get("S2_CUSTOMS_EVIDENCE_DIR", "/tmp/s2compat/evidence"))


# --------------------------------------------------------------------------------------------- helpers
async def drain(*kinds: str, rounds: int = 40) -> int:
    """Run every due job of these kinds until none is left (deferred jobs wait for their next_run_at)."""
    from core.jobs import work_once
    total = 0
    for _ in range(rounds):
        n = await work_once(OWNER, 50, kinds=list(kinds))
        total += n
        if not n:
            return total
    raise AssertionError(f"jobs {kinds} did not settle")


async def autopilot(on: bool):
    from core.autonomy import switch
    from core.settings import get_settings
    await switch("autopilot_enabled", on, "enable" if on else "", OPS)
    assert (await get_settings())["autopilot_enabled"] is on


async def put_settings(**kw):
    from core.settings import update_settings
    out = await update_settings(kw)
    for k, v in kw.items():
        assert out[k] == v, k


def codes(reasons) -> list:
    return [r["code"] for r in reasons or []]


async def find(coll: str, q: dict, sort=None) -> list:
    from core.db import db
    cur = db.raw[coll].find(q, {"_id": 0})
    if sort:
        cur = cur.sort(sort)
    return await cur.to_list(500)


async def one(coll: str, q: dict) -> dict | None:
    from core.db import db
    return await db.raw[coll].find_one(q, {"_id": 0})


async def decisions(agent: str, subject_id: str) -> list:
    return await find("agent_decisions", {"agent": agent, "subject_id": subject_id}, sort=[("at", 1)])


def fi_dt(iso_s: str) -> str:
    """Finnish date-time as the customer messages write it (Helsinki time), computed here independently."""
    from datetime import datetime
    d = datetime.fromisoformat(iso_s.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Helsinki"))
    return f"{d.day}.{d.month}.{d.year} klo {d.hour}.{d.minute:02d}"


def fi_date(iso_s: str) -> str:
    from datetime import datetime
    d = datetime.fromisoformat(iso_s.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Helsinki"))
    return f"{d.day}.{d.month}.{d.year}"


def iso6346_mrn_check(mrn17: str) -> str:
    """UCC MRN check digit (ISO 6346 over the first 17 characters), written out here from the standard."""
    def val(ch):
        if ch.isdigit():
            return int(ch)
        v = 10 + ord(ch) - ord("A")
        return v + (v - 1) // 10  # 11, 22, 33 are skipped
    return str(sum(val(c) * 2 ** i for i, c in enumerate(mrn17)) % 11 % 10)


def viite_ok(ref: str) -> bool:
    digits = ref.replace(" ", "")
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (7, 3, 1)[i % 3] for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def rf_ok(rf: str) -> bool:
    s = rf.replace(" ", "")
    moved = s[4:] + s[:4]
    return int("".join(str(int(ch, 36)) for ch in moved)) % 97 == 1


def invoice_csv(number: str, *, complete: bool = True) -> bytes:
    rows = [("Document type", "Commercial invoice"), ("Invoice number", number), ("Invoice date", "2026-10-20"),
            ("Seller", "Hudson Valley Furniture Works Inc"), ("Seller address", "120 Water Street"),
            ("Seller city", "New York"), ("Seller postal code", "10005"), ("Nature of transaction", "11"),
            ("Seller country", "US"), ("Buyer", "Sandbox Tuonti Oy"), ("Buyer country", "FI")]
    if complete:
        rows += [("Incoterm", "FOB"), ("Incoterm place", "New York"), ("Currency", "EUR"), ("Total", "2500.00")]
    rows += [("Country of origin", "US"), ("Total gross weight kg", "520"), ("Total net weight kg", "480")]
    lines = [";".join(r) for r in rows] + [
        "", "Description;Quantity;Unit price;Amount;HS code;Origin;Net kg;Gross kg",
        f"{CHAIRS};20;125.00;2500.00;94016100;US;480;520"]
    return ("\n".join(lines) + "\n").encode()


def evidence_pdf(text: str) -> bytes:
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for i, line in enumerate(text.splitlines()):
        c.drawString(56, 800 - i * 16, line)
    c.showPage()
    c.save()
    return buf.getvalue()


async def make_shipment(company_id: str, tag: str, *, origin=FIHEL, dest=DEHAM, direction="export", customs=False,
                        dg=False, freight_cents=177000, freight_cost=150000, regime="HAGUE_VISBY", incoterm="FCA",
                        incoterm_place=None, weight=18000, packages=20, description="Birch plywood sheets, 18 mm",
                        carrier="ALPHA") -> dict:
    """A booked shipment from an accepted quote option (services.shipments.create_shipment, emits shipment.created)."""
    from core.db import db
    from core.util import now
    from services import shipments as svc

    etd = (now() + timedelta(days=5)).date().isoformat()
    eta = (now() + timedelta(days=8)).date().isoformat()
    cid = W[carrier]
    lines = [{"code": "OCEAN_FREIGHT", "stage": "main", "leg_seq": 1, "qty": 1, "unit_cost_cents": freight_cost,
              "cost_cents": freight_cost, "unit_price_cents": freight_cents, "amount_cents": freight_cents,
              "is_pass_through": False}]
    if dg:
        lines.append({"code": "DG", "stage": "main", "leg_seq": 1, "qty": 1, "unit_cost_cents": 15000, "cost_cents": 15000,
                      "unit_price_cents": 17700, "amount_cents": 17700, "is_pass_through": False})
    lines.append({"code": "DOC_FEE", "stage": "fees", "leg_seq": None, "qty": 1, "unit_cost_cents": 0, "cost_cents": 0,
                  "unit_price_cents": 3500, "amount_cents": 3500, "is_pass_through": False})
    option = {"id": f"opt_{tag}", "mode": "sea", "service": "FCL", "carrier_id": cid, "carrier_name": W[carrier + "_name"],
              "rate_card_id": f"card-{carrier.lower()}-{origin['unlocode'].lower()}", "etd": etd, "eta": eta,
              "total_cents": sum(ln["amount_cents"] for ln in lines), "cost_cents": sum(ln["cost_cents"] for ln in lines),
              "liability_regime": regime, "forwarder_role": "principal",
              "legs": [{"seq": 1, "kind": "main", "role": "main", "mode": "sea", "from": dict(origin), "to": dict(dest),
                        "carrier_id": cid, "carrier_name": W[carrier + "_name"], "carrier": W[carrier + "_name"],
                        "transit_days": 3, "service": "FCL"}],
              "lines": lines}
    cargo = {"load_type": "FCL", "containers": [{"type": "40HC", "count": 1}], "weight_kg": weight, "packages": packages,
             "package_type": "cartons" if direction == "import" else "pallet_eur",
             "description": description, "dangerous_goods": dg}
    if dg:
        cargo["dg"] = {"un_number": "UN1263", "class": "3", "packing_group": "II", "proper_shipping_name": "PAINT",
                       "emergency_phone": "+358 9 123 4567"}
    quote = {"id": f"q_{tag}", "number": f"Q-{tag}", "company_id": company_id, "status": "booked", "source": "ops",
             "request": {"mode": "sea", "origin": dict(origin), "destination": dict(dest), "cargo": cargo,
                         "direction": direction, "incoterm": incoterm, "incoterm_place": incoterm_place,
                         "customs_clearance": customs, "ready_date": etd},
             "options": [option], "accepted_option_id": option["id"]}
    await db.raw.quotes.insert_one(dict(quote))
    booking = {"parties": {"shipper": {"name": "Hudson Valley Furniture Works Inc" if direction == "import" else "Metsä Export Oy",
                                       "address": {"street": "Satamatie 1", "city": origin["name"], "country": origin["country"]}},
                           "consignee": {"name": "Sandbox Tuonti Oy" if direction == "import" else "Hafen Handel GmbH",
                                         "contact_name": "Ville Vastaanottaja",
                                         "address": {"street": "Kaiweg 2", "city": dest["name"], "country": dest["country"]}}},
               "references": {"customer_ref": f"PO-{tag}", "po_numbers": [tag]}}
    return await svc.create_shipment(company_id=company_id, quote=quote, option=option, booking=booking, actor=OPS)


async def import_shipment(tag: str, company_id: str = "co-sbx"):
    from core.db import db
    from services import customs_offices
    s = await make_shipment(company_id, tag, origin=USNYC, dest=FIHEL, direction="import", customs=True, incoterm="FOB",
                            incoterm_place="New York", weight=520, packages=20, description=CHAIRS, freight_cents=180000)
    # The terminal instruction supplies the actual warehouse and presentation office.
    # FIHEL has several ENT offices: do not make the agent guess which terminal was booked.
    office = await customs_offices.verify("FI009801", "ENT")
    assert customs_offices.eligible(office, "ENT", mode="sea")
    await db.raw.shipments.update_one({"id": s["id"]}, {"$set": {
        "temporary_storage": {"eori": "FI3990002-2", "warehouse_code": "001", "location_code": "01"},
        "office_of_presentation": {k: office[k] for k in ("code", "name", "source_url", "sha256", "fetched_at")}
        | {"source": "EU COL", "role": "ENT", "verified": True}}})
    return await shipment(s["id"])


async def shipment(sid: str) -> dict:
    return await one("shipments", {"id": sid})


async def book_legs(sid: str) -> list:
    """The carrier agent's own booking path for every leg (rank, decide, book). The carrier.select_and_book job
    wrapper cannot store its result (see test_carrier_select_and_book_job_finishes), so other tests book this way."""
    from services.agents import carrier_agent
    s = await shipment(sid)
    return [await carrier_agent.book_one_leg(s, leg, reason="initial booking") for leg in s["legs"]]


async def confirm_and_book(sid: str) -> dict:
    """booking.confirm then the carrier agent books the legs (Autopilot on, a sandbox company)."""
    await drain("booking.confirm")
    await book_legs(sid)
    s = await shipment(sid)
    assert s["booking_confirmation"]["status"] == "confirmed", s.get("booking_confirmation")
    assert all((lg.get("booking") or {}).get("status") == "confirmed" for lg in s["legs"]), [lg.get("booking") for lg in s["legs"]]
    return s


async def upload_invoice(sid: str | None, number: str, *, complete: bool = True, company_id: str = "co-sbx",
                         omit_exemption: str | None = None) -> dict:
    from services import documents, extraction
    s = await shipment(sid) if sid else None
    if s and s.get("direction") == "import":
        await upload_import_support(s, number, omit_exemption=omit_exemption)
    doc = await documents.create_document(company_id=company_id, shipment_id=sid, doc_type="commercial_invoice",
                                          filename=f"{number}.csv", data=invoice_csv(number, complete=complete),
                                          actor=CUSTOMER, extract=False)
    await extraction.extract_document(doc["id"])
    return await one("documents", {"id": doc["id"]})


async def upload_import_support(s: dict, number: str, *, omit_exemption: str | None = None):
    """Actual files for sandbox cargo; structured CSV extraction and human-entered B/L fields.

    The supplier explicitly attests the exemptions. The agent must never infer them
    from a generic chair description or forge a CITES/other official authorisation.
    """
    from services import documents, extraction
    from routes.documents import patch_document, DocPatch
    sid, cid = s["id"], s["company_id"]
    for code, statement in {
        "Y904": "Ordinary office seats, without restraints; not goods in the torture-goods annex.",
        "Y900": "Solid Scots pine (Pinus sylvestris) and polyester; no CITES-listed materials.",
        "Y922": "Polyester upholstery, no animal fur, including no cat or dog fur.",
    }.items():
        if code == omit_exemption:
            continue
        await documents.create_document(company_id=cid, shipment_id=sid, doc_type="other",
            filename=f"{number}-{code}.pdf", actor=CUSTOMER, extract=False,
            data=evidence_pdf(f"SANDBOX supplier statement {number}-{code}\nHudson Valley Furniture Works Inc\n"
                              f"Invoice {number}: 20 wooden upholstered office chairs\n{statement}"),
            extra={"certificate_code": code, "certificate_number": f"{number}-{code}"})
    packing = (f"Document type;Packing list\nPacking list number;PL-{number}\n"
               "Consignee;Sandbox Tuonti Oy\nTotal packages;20\nPackage type;cartons\n"
               "Total gross weight kg;520\nTotal net weight kg;480\n"
               f"Marks and numbers;{number} CTN 1-20\nContainer numbers;MSCU6639870\n")
    doc = await documents.create_document(company_id=cid, shipment_id=sid, doc_type="packing_list",
        filename=f"PL-{number}.csv", data=packing.encode(), actor=CUSTOMER, extract=False)
    await extraction.extract_document(doc["id"])
    leg = s["legs"][0]
    fields = {"bl_number": leg["transport_doc_no"], "shipper_name": "Hudson Valley Furniture Works Inc",
              "consignee_name": "Sandbox Tuonti Oy", "port_of_loading": "New York",
              "port_of_discharge": "Helsinki", "vessel_name": leg["vessel"]["name"], "vessel_flag": "FI",
              "total_gross_weight_kg": 520, "container_numbers": ["MSCU6639870"]}
    doc = await documents.create_document(company_id=cid, shipment_id=sid, doc_type="bill_of_lading",
        filename=f"BL-{number}.pdf", actor=CUSTOMER, extract=False,
        data=evidence_pdf("SANDBOX Bill of lading\n" + "\n".join(f"{k}: {v}" for k, v in fields.items())))
    await patch_document(doc["id"], DocPatch(fields=fields, confirm=True), OPS)


async def feed(provider: str, ev: dict):
    from services.agents.tracking_agent import store_feed_event
    return await store_feed_event(provider, ev, verified=True)


async def approve_card(approval_id: str, note: str = "Tarkistettu ja hyväksytty") -> dict:
    """The owner decides a card in /ops/approvals (routes.admin.decide, the endpoint's own function)."""
    from core.permissions import permissions_for
    from routes import admin
    user = {**OPS, "permissions": permissions_for(OPS)}
    return await admin.decide(approval_id, admin.DecisionIn(decision="approved", note=note), user=user)


async def deliver_manually(sid: str):
    from services import shipments as svc
    await svc.set_status(sid, "in_transit", actor=OPS, milestone="departed")
    await svc.set_status(sid, "delivered", actor=OPS)


async def file_and_release(decl_id: str, *, phase: str = "initial"):
    """customs.review_and_file, then two customs.poll_tulli rounds (release, then the decision)."""
    from core.jobs import enqueue
    await enqueue("customs.review_and_file", {"declaration_id": decl_id}, key=f"customs:{decl_id}:pytest:{phase}",
                  subject=("customs_declaration", decl_id))
    await drain("customs.review_and_file")
    for i in range(2):
        await enqueue("customs.poll_tulli", {}, key=f"poll:{decl_id}:{phase}:{i}")
        await drain("customs.poll_tulli")
    from services import customs
    from services.customs_messages.tulli_common import validate
    d = await one("customs_declarations", {"id": decl_id})
    print("CUSTOMS BLOCKING AFTER REVIEW", json.dumps(customs.blocking(d), indent=2))
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    name = d["invoice"]["number"]
    (EVIDENCE / f"{name}-lifecycle.json").write_text(json.dumps({
        "declaration": d, "blocking": customs.blocking(d),
        "decisions": await decisions("customs_agent", decl_id),
        "approval": await one("approvals", {"id": d.get("approval_id")}),
    }, indent=2, default=str))
    message = await one("customs_messages", {"declaration_id": decl_id, "direction": "out"})
    if message:
        (EVIDENCE / f"{name}-FI415B.xml").write_text(message["xml"])
        result = validate(message["xml"], "import")
        assert result["valid"], result
        (EVIDENCE / f"{name}-XSD.json").write_text(json.dumps(result, indent=2))


# --------------------------------------------------------------------------------------------- world
def _copy_sanctions_lists() -> bool:
    """Copy the sanctions lists (EU/UN/OFAC/UK/US CSL) a local database already downloaded from the official sources."""
    import pymongo
    from core import config
    c = pymongo.MongoClient(config.MONGO_URL, serverSelectionTimeoutMS=5000)
    try:
        for name in c.list_database_names():
            if name == os.environ["DB_NAME"] or name in ("admin", "config", "local"):
                continue
            src = c[name]
            metas = list(src.sanctions_lists.find({"version": {"$ne": None}}, {"_id": 0}))
            from services.screening import LIST_SOURCES  # every configured list (s12 added uk + csl)
            if {m["list"] for m in metas} != set(LIST_SOURCES):
                continue
            if any(src.sanctions_entries.count_documents({"list": m["list"], "version": m["version"]}) != m["entries"]
                   for m in metas):
                continue
            dst = c[os.environ["DB_NAME"]]
            dst.sanctions_lists.insert_many(metas)
            for m in metas:
                batch = []
                for e in src.sanctions_entries.find({"list": m["list"], "version": m["version"]}, {"_id": 0}):
                    batch.append(e)
                    if len(batch) == 2000:
                        dst.sanctions_entries.insert_many(batch)
                        batch = []
                if batch:
                    dst.sanctions_entries.insert_many(batch)
            W["sanctions_source"] = name
            return True
        return False
    finally:
        c.close()


async def _seed():
    from core.db import db
    from core.util import now, now_iso

    today = now().date()
    W["today"] = today
    carriers = {"ALPHA": ("Alpha Lines", "dcsa", ["3"]), "BRAVO": ("Bravo Shipping", "manual", [])}
    for code, (name, integ, dg) in carriers.items():
        cid = f"car-{code.lower()}"
        W[code], W[code + "_name"] = cid, name
        await db.raw.carriers.insert_one({"id": cid, "code": code, "name": name, "modes": ["sea"], "country": "FI",
                                          "integration": {"type": integ}, "active": True, "on_time_pct": 90,
                                          "dg_classes_accepted": dg, "dg_un_blocked": [],
                                          "contact_email": f"{code.lower()}.booking@carrier.test"})
    lanes = [("ALPHA", "FIHEL", "DEHAM", 150000), ("BRAVO", "FIHEL", "DEHAM", 170000), ("ALPHA", "USNYC", "FIHEL", 150000)]
    for code, o, d, cost in lanes:
        await db.raw.rate_cards.insert_one({
            "id": f"card-{code.lower()}-{o.lower()}", "carrier_id": W[code], "mode": "sea", "service": "FCL",
            "basis": "PER_CONTAINER", "origin": {"unlocode": o, "radius_km": 40}, "destination": {"unlocode": d, "radius_km": 40},
            "container_costs": {"40HC": cost}, "surcharges": [], "transit_days": 3,
            "valid_from": (today - timedelta(days=10)).isoformat(), "valid_to": (today + timedelta(days=60)).isoformat(),
            "source": "contract", "currency": "EUR", "archived": False})
    poa = {"type": "direct", "scope": ["import", "export"], "valid_from": (today - timedelta(days=30)).isoformat(),
           "valid_to": (today + timedelta(days=365)).isoformat(), "signed_at": now_iso()}
    base = {"kyc_status": "approved", "sanctions_status": "clear", "credit_limit_cents": 100_000_000,
            "address": {"street": "Satamatie 5", "postal_code": "00980", "city": "Helsinki", "country": "FI"}}
    companies = [
        {**base, "id": "co-sbx", "name": "Sandbox Tuonti Oy", "business_id": "3990002-2", "test_tenant": True,
         "eori": "FI3990002-2", "vat_id": "FI39900022", "vat_registered": True, "poa": poa},
        {**base, "id": "co-real", "name": "Metsä Export Oy", "business_id": "2840123-6"},
        {**base, "id": "co-lowcredit", "name": "Sandbox Pienasiakas Oy", "business_id": "3990005-7", "test_tenant": True,
         "credit_limit_cents": 100000},
    ]
    await db.raw.companies.insert_many([dict(c) for c in companies])
    await db.raw.users.insert_many([dict(OPS), dict(CUSTOMER)])
    await db.raw.sdr_rates.insert_one({"date": today.isoformat(), "eur_per_sdr": SDR_EUR, "fetched_at": now_iso(),
                                       "source": "test fixture (IMF not fetched)", "source_url": None})
    # Money guardrails high by default; each test that checks a limit sets its own value.
    await put_settings(guard_spend_per_day_cents=100_000_000, quote_approval_threshold_eur=5000.0,
                       customs_confirm_timeout_hours=0)


async def _setup():
    # first import of core.db inside the module's loop: the Motor client binds to the loop that is running
    import server  # noqa: F401  (every route module, so every production event handler, is registered)
    import services.agents  # noqa: F401  (every agent job, trigger and schedule)
    from core.db import ensure_indexes
    from core.llm import llm_available
    from services import screening, taric, customs_offices
    from core.db import db
    from core.sandbox import sandbox_eori

    assert not llm_available(), "tests run without an AI provider"
    await ensure_indexes()
    if not await asyncio.to_thread(_copy_sanctions_lists):
        W["sanctions_source"] = "downloaded"
        await screening.refresh_lists()
    assert all(s["loaded"] for s in await screening.lists_status())
    await _seed()
    pages = {}
    for url, meta in json.loads((FIX / "s2_customs/manifest.json").read_text())["pages"].items():
        data = (FIX / "s2_customs" / meta["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == meta["sha256"]
        pages[url] = data.decode()
    taric._REPLAY = pages
    source = json.loads((FIX / "offices/SOURCE.json").read_text())
    await customs_offices.install_snapshot((FIX / "offices/COL-Generic.zip").read_bytes(), source)
    await db.raw.aeo_eori_checks.insert_one(sandbox_eori("3990002-2"))
    # Establish a recurring customer through a real sandbox filing. The first
    # declaration must receive the independent review required by s11 AEO.
    await autopilot(True)
    await put_settings(customs_filing_delegated=True)
    s = await import_shipment("first-import")
    await confirm_and_book(s["id"])
    await upload_invoice(s["id"], "INV-FIRST")
    d = await one("customs_declarations", {"shipment_id": s["id"]})
    await file_and_release(d["id"])
    d = await one("customs_declarations", {"id": d["id"]})
    card = await one("approvals", {"id": d.get("approval_id")})
    assert d["status"] == "ready_for_review" and card["status"] == "pending", d
    assert "aeo_four_eyes_first_declaration" in codes(card["reasons"]), card
    from routes.admin import decide, DecisionIn
    from core.permissions import permissions_for
    approved = await decide(card["id"], DecisionIn(decision="approved", note="Reviewed sandbox first import"),
                            {**OPS, "permissions": permissions_for(OPS)})
    assert approved["status"] == "approved", approved
    await file_and_release(d["id"], phase="independently-approved")
    d = await one("customs_declarations", {"id": d["id"]})
    assert d["status"] == "released" and d["submission"]["sandbox"] and d["customs_decision"]["sandbox"], d
    await put_settings(customs_filing_delegated=False)


async def _teardown():
    from core.db import client
    from services import taric

    taric._REPLAY = None
    await client.drop_database(os.environ["DB_NAME"])


@pytest.fixture(scope="module", autouse=True)
def world():
    try:
        run(_setup())
        yield W
    finally:
        run(_teardown())


# --------------------------------------------------------------------------------------------- kyc.decide
def _kyc_company(cid: str, business_id: str, name: str) -> dict:
    return {"id": cid, "name": name, "business_id": business_id, "test_tenant": True, "kyc_status": "not_started",
            "address": {"street": "Testikatu 1", "postal_code": "00100", "city": "Helsinki", "country": "FI"},
            "signatory": {"name": "Matti Meikäläinen", "title": "toimitusjohtaja"},
            "ubos": [{"name": "Maija Meikäläinen", "country": "FI", "share_pct": 100, "pep": False}],
            "created_at": "2026-10-24T08:00:00Z", "updated_at": "2026-10-24T08:00:00Z"}


def test_kyc_decide_approves_a_green_sandbox_company_with_autopilot_on():
    from core.db import db
    from core.events import emit

    run(autopilot(True))
    c = _kyc_company("co-kyc-on", "3990003-0", "Sandbox Kauppa Oy")
    run(db.raw.companies.insert_one(dict(c)))
    run(emit("company.profile_completed", {"company": c}))
    assert run(drain("kyc.decide")) == 1
    got = run(one("companies", {"id": c["id"]}))
    assert (got["kyc_status"], got["sanctions_status"]) == ("approved", "clear")
    assert got["kyc"]["decided_by"]["id"] == "agent:kyc_agent" and got["kyc"]["green"] is True
    assert got["kyc"]["risk"] == {"ytunnus": "pass", "ytj": "pass", "vies": "pass", "sanctions": "pass", "ubo": "pass",
                                  "pep": "pass"}  # s12: PEP check of owners and signatory
    assert got["kyc"]["sandbox"] is True and got["kyc"]["ytj"]["data"]["sandbox"] is True
    assert got["kyc"]["sanctions"]["screened_names"] == ["Sandbox Kauppa Oy", "Maija Meikäläinen", "Matti Meikäläinen"]
    d = run(decisions("kyc_agent", c["id"]))
    assert [(x["action"], x["outcome"], x["reasons"]) for x in d] == [("approve_customer", "auto", [])]
    assert got["kyc"]["agent_decision_id"] == d[0]["id"]
    assert run(find("approvals", {"kind": "kyc_review", "ref_id": c["id"], "status": "pending"})) == []


def test_kyc_decide_parks_the_same_company_in_the_owner_queue_with_autopilot_off():
    from core.db import db
    from core.events import emit

    run(autopilot(False))
    c = _kyc_company("co-kyc-off", "3990004-9", "Sandbox Logistiikka Oy")
    run(db.raw.companies.insert_one(dict(c)))
    run(emit("company.profile_completed", {"company": c}))
    assert run(drain("kyc.decide")) == 1
    got = run(one("companies", {"id": c["id"]}))
    assert got["kyc_status"] == "in_review" and got["kyc"]["green"] is True and got["kyc"].get("decided_by") is None
    cards = run(find("approvals", {"kind": "kyc_review", "ref_id": c["id"]}))
    assert len(cards) == 1 and cards[0]["status"] == "pending"
    assert codes(cards[0]["reasons"]) == ["autopilot_off"] and cards[0]["agent"] == "kyc_agent"
    d = run(decisions("kyc_agent", c["id"]))
    assert [(x["outcome"], codes(x["reasons"])) for x in d] == [("owner_queue", ["autopilot_off"])]
    assert cards[0]["agent_decision_id"] == d[0]["id"]


# --------------------------------------------------------------------------------------------- quote.autosend
async def _pending_quote(tag: str, total: int, cost: int) -> dict:
    from core.db import db
    from core.settings import get_settings
    from routes import quotes as rq
    q = {"id": f"q-{tag}", "number": f"Q-{tag}", "company_id": "co-sbx", "customer_name": "Sandbox Tuonti Oy",
         "status": "draft", "source": "ops",
         "request": {"mode": "sea", "origin": dict(FIHEL), "destination": dict(DEHAM), "cargo_summary": "1 × 40' HC",
                     "cargo": {"load_type": "FCL", "containers": [{"type": "40HC", "count": 1}], "weight_kg": 9000}},
         "options": [{"id": f"opt-{tag}", "mode": "sea", "carrier_name": "Alpha Lines", "total_cents": total, "cost_cents": cost,
                      "lines": [{"code": "OCEAN_FREIGHT", "amount_cents": total - 3500},
                                {"code": "DOC_FEE", "amount_cents": 3500}]}]}
    await db.raw.quotes.insert_one(dict(q))
    q = await rq.apply_approval_policy(q, await get_settings(), OPS, send=True)
    await db.raw.quotes.update_one({"id": q["id"]}, {"$set": {"status": q["status"], "approval": q["approval"]}})
    assert q["status"] == "pending_approval" and q["approval"]["reasons"] == ["threshold"]
    return q


def test_quote_autosend_sends_inside_policy_and_keeps_a_quote_above_the_cap_with_the_owner():
    from core.jobs import enqueue

    run(autopilot(False))
    inside = run(_pending_quote("in", 600000, 480000))      # 6 000 EUR, margin 20 %
    above = run(_pending_quote("above", 1200000, 900000))   # 12 000 EUR > autopilot_quote_max_cents 10 000 EUR
    run(autopilot(True))
    for q in (inside, above):
        aid = q["approval"]["approval_id"]
        run(enqueue("quote.autosend", {"approval_id": aid}, key=f"quote:{aid}:on", subject=("quote", q["id"])))
    assert run(drain("quote.autosend")) == 2

    q1 = run(one("quotes", {"id": inside["id"]}))
    a1 = run(one("approvals", {"id": inside["approval"]["approval_id"]}))
    assert q1["status"] == "sent" and q1["published"] is True
    assert a1["status"] == "approved" and a1["decided_by"]["id"] == "agent:quote_agent"
    assert [(x["action"], x["outcome"]) for x in run(decisions("quote_agent", inside["id"]))] == [("send_quote", "auto")]

    q2 = run(one("quotes", {"id": above["id"]}))
    a2 = run(one("approvals", {"id": above["approval"]["approval_id"]}))
    assert q2["status"] == "pending_approval" and a2["status"] == "pending"
    assert codes(a2["reasons"]) == ["quote_policy"]
    d2 = run(decisions("quote_agent", above["id"]))
    assert [(x["outcome"], codes(x["reasons"])) for x in d2] == [("owner_queue", ["quote_policy"])]
    assert d2[0]["approval_id"] == a2["id"]
    job = run(one("jobs", {"key": f"quote:{a2['id']}:on"}))
    assert job["status"] == "done" and job["result"] == {"owner_queue": True, "reasons": ["quote_policy"]}


# --------------------------------------------------------------------------------------------- booking.confirm
def test_booking_confirm_confirms_inside_limits_and_hands_over_to_the_carrier_agent():
    run(autopilot(True))
    s = run(make_shipment("co-sbx", "bk1"))
    assert run(one("jobs", {"key": f"booking:{s['id']}:on"}))["status"] == "queued"
    run(drain("booking.confirm"))
    bc = run(shipment(s["id"]))["booking_confirmation"]
    assert bc["status"] == "confirmed" and bc["by"]["id"] == "agent:booking_agent"
    d = run(decisions("booking_agent", s["id"]))
    assert [(x["action"], x["outcome"]) for x in d] == [("confirm_booking", "auto")] and bc["decision_id"] == d[0]["id"]
    assert d[0]["guardrails"]["credit"]["this_cents"] == 180500
    nxt = run(one("jobs", {"key": f"carrier:{s['id']}:initial"}))
    assert nxt["kind"] == "carrier.select_and_book" and nxt["status"] == "queued"
    assert run(find("approvals", {"kind": "booking_review", "ref_id": s["id"]})) == []


def test_booking_confirm_sends_credit_limit_and_dangerous_goods_to_the_owner():
    run(autopilot(True))
    over = run(make_shipment("co-lowcredit", "bk-credit"))        # 1 805 EUR against a 1 000 EUR credit limit
    dg = run(make_shipment("co-sbx", "bk-dg", dg=True))
    run(drain("booking.confirm"))
    for s, code in ((over, "credit_limit"), (dg, "legal")):
        got = run(shipment(s["id"]))
        assert got["booking_confirmation"]["status"] == "pending_owner"
        cards = run(find("approvals", {"kind": "booking_review", "ref_id": s["id"]}))
        assert len(cards) == 1 and cards[0]["status"] == "pending" and codes(cards[0]["reasons"]) == [code]
        assert got["booking_confirmation"]["approval_id"] == cards[0]["id"]
        assert run(one("jobs", {"key": f"carrier:{s['id']}:initial"})) is None
    dg_card = run(one("approvals", {"kind": "booking_review", "ref_id": dg["id"]}))
    assert dg_card["legal_point"] == "dangerous_goods" and dg_card["legal_basis"].startswith("IMDG Code")
    credit = run(decisions("booking_agent", over["id"]))[0]["guardrails"]["credit"]
    assert (credit["limit_cents"], credit["limit_source"], credit["this_cents"]) == (100000, "company", 180500)


def test_booking_confirm_with_autopilot_off_waits_for_the_owner_and_the_approval_confirms_it():
    run(autopilot(False))
    s = run(make_shipment("co-sbx", "bk-off"))
    run(drain("booking.confirm"))
    card = run(one("approvals", {"kind": "booking_review", "ref_id": s["id"]}))
    assert card["status"] == "pending" and codes(card["reasons"]) == ["autopilot_off"]
    assert run(shipment(s["id"]))["booking_confirmation"]["status"] == "pending_owner"
    run(approve_card(card["id"]))
    bc = run(shipment(s["id"]))["booking_confirmation"]
    assert bc["status"] == "confirmed" and bc["by"]["id"] == "u-ops"
    assert run(one("jobs", {"key": f"carrier:{s['id']}:initial"}))["status"] == "queued"


# --------------------------------------------------------------------------------------------- carrier.select_and_book
def test_carrier_agent_books_the_sandbox_leg_and_respects_the_daily_spend_limit():
    run(autopilot(True))
    s = run(make_shipment("co-sbx", "cb1"))
    run(drain("booking.confirm"))
    out = run(book_legs(s["id"]))
    rows = run(find("carrier_bookings", {"shipment_id": s["id"]}))
    assert len(rows) == 1
    b = rows[0]
    assert out == [{"status": "confirmed", "booking_id": b["id"], "carrier": "Alpha Lines"}]
    assert (b["status"], b["carrier_id"], b["leg_seq"], b["sandbox"], b["mode"]) == ("confirmed", W["ALPHA"], 1, True, "sandbox")
    leg = run(shipment(s["id"]))["legs"][0]
    assert leg["booking"]["status"] == "confirmed" and leg["booking"]["id"] == b["id"]
    assert re.fullmatch(r"SBXALPHA\d{8}", leg["transport_doc_no"]) and leg["transport_doc_no"] == b["transport_doc_no"]
    d = [x for x in run(decisions("carrier_agent", s["id"])) if x["action"] == "book_leg"]
    assert [(x["outcome"], x["spend_cents"], x["decision"]["carrier_id"]) for x in d] == [("auto", 150000, W["ALPHA"])]

    run(put_settings(guard_spend_per_day_cents=100000))
    try:
        s2 = run(make_shipment("co-sbx", "cb-spend"))
        run(drain("booking.confirm"))
        out = run(book_legs(s2["id"]))
        assert run(find("carrier_bookings", {"shipment_id": s2["id"]})) == []
        card = run(one("approvals", {"kind": "carrier_booking", "ref_id": s2["id"]}))
        assert out == [{"status": "owner_queue", "approval_id": card["id"]}]
        assert card["status"] == "pending" and codes(card["reasons"]) == ["spend_limit"]
        assert (card["data"]["leg_seq"], card["data"]["carrier_id"], card["amount_cents"]) == (1, W["ALPHA"], 150000)
        assert run(shipment(s2["id"]))["legs"][0].get("booking") is None
    finally:
        run(put_settings(guard_spend_per_day_cents=100_000_000))


def test_carrier_select_and_book_job_finishes():
    run(autopilot(True))
    s = run(make_shipment("co-sbx", "cb-job"))
    run(drain("booking.confirm"))
    run(drain("carrier.select_and_book"))
    job = run(one("jobs", {"key": f"carrier:{s['id']}:initial"}))
    assert job["status"] == "done" and list(job["result"].values()) == ["confirmed"]
    assert [b["status"] for b in run(find("carrier_bookings", {"shipment_id": s["id"]}))] == ["confirmed"]


# --------------------------------------------------------------------------------------------- customs
def test_customs_agent_files_to_sandbox_tulli_and_the_poll_releases_with_a_sandbox_decision():
    from services import customs

    run(autopilot(True))
    run(put_settings(customs_filing_delegated=True))
    try:
        s = run(import_shipment("cu1"))
        s = run(confirm_and_book(s["id"]))
        tdoc = s["legs"][0]["transport_doc_no"]
        doc = run(upload_invoice(s["id"], "INV-CU1"))
        assert doc["extraction"]["status"] == "done"
        d = run(one("customs_declarations", {"shipment_id": s["id"]}))   # drafted by customs._auto_draft
        print("CUSTOMS BLOCKING BEFORE REVIEW", json.dumps(customs.blocking(d), indent=2))
        assert d["status"] == "draft" and d["invoice"]["number"] == "INV-CU1" and d["screening"]["status"] == "clear"
        it = d["items"][0]
        assert (it["hs_code"], it["cn_code"], it["cn_confirmed"], it["origin_country"]) == ("940161", "94016100", False, "US")

        run(file_and_release(d["id"]))
        d = run(one("customs_declarations", {"id": d["id"]}))
        from services import customs_offices, taric
        office = run(customs_offices.verify(d["office"]["code"], "ENT"))
        assert (office["code"], office["name"], office["unlocode"]) == (
            "FI009801", "Meritulli Vuosaari satama V6", "FIHEL")
        assert d["office"]["verified"] and d["office"]["sha256"] == office["sha256"]
        assert d["goods_location"] == {"type": "B", "qualifier": "EO", "identification": "FI3990002-200101",
                                        "source": "shipment.temporary_storage"}
        assert d["transport"]["transport_document"]["number"] == tdoc
        assert d["transport"]["transport_document"]["type"] == "bill_of_lading"
        assert d["transport"]["container_numbers"] == ["MSCU6639870"]
        assert d["exporter"]["address"] == {"street": "120 Water Street", "city": "New York",
                                             "postal_code": "10005", "country": "US"}
        assert d["transaction_nature"] == "11"
        it = d["items"][0]
        official = run(taric.lookup("9401610000", country="US", on=it["taric"]["taric_date"]))
        # Official captured third-country measure: 0%, not the old AI estimate.
        assert official["third_country_duty"]["duty"]["pct"] == 0.0
        assert it["duty_source"] == "taric" and it["duty_rate_pct"] == 0.0
        assert it["taric"]["source_url"] == official["source_url"]
        assert it["preference"] == "100" and it["marks"] == "INV-CU1 CTN 1-20"
        assert {r["satisfied_by"]["code"] for r in it["licence_requirements"]} == {"Y904", "Y900", "Y922"}
        assert d["screening"]["complete"] and all(p["status"] == "clear" for p in d["screening"]["parties"])
        assert it["cn_confirmed"] is True and it["cn_confirmed_by_agent"] is True and it["cn_confidence"] == 0.9
        sub = d["submission"]
        assert (sub["channel"], sub["state"], sub["sandbox"]) == ("sandbox", "sent", True)
        mrn = d["mrn"]
        assert re.fullmatch(r"\d{2}FISBX\d{9}R\d", mrn) and mrn == sub["mrn"]
        assert mrn[17] == iso6346_mrn_check(mrn[:17]) == customs.mrn_check_digit(mrn[:17]) and customs.mrn_ok(mrn)
        assert d["status"] == "released" and d["released_at"]
        card = run(one("approvals", {"kind": "customs_filing", "ref_id": d["id"]}))
        assert card["status"] == "approved" and card["decided_by"]["id"] == "agent:customs_agent"
        gate = [x for x in run(decisions("customs_agent", d["id"])) if x["action"] == "file_declaration"]
        assert [(x["outcome"], x["reasons"]) for x in gate] == [("auto", [])]
        dec = d["customs_decision"]
        value = it["customs_value_cents"]
        assert value == 250000 + 180000  # invoice 2 500 EUR + ocean freight to the EU border under FOB
        assert (dec["source"], dec["sandbox"], dec["paid_by"], dec["mrn"]) == ("tulli_decision", True, "forwarder", mrn)
        assert dec["items"] == [{"seq": 1, "duty_cents": 0, "vat_cents": round(value * 25.5 / 100)}]
        assert (dec["duty_cents"], dec["vat_cents"]) == (0, round(value * 25.5 / 100))
    finally:
        run(put_settings(customs_filing_delegated=False))


def test_customs_filing_without_the_directors_delegation_is_a_legal_point_for_the_owner():
    run(autopilot(True))
    run(put_settings(customs_filing_delegated=False))
    s = run(import_shipment("cu2"))
    s = run(confirm_and_book(s["id"]))
    run(upload_invoice(s["id"], "INV-CU2"))
    d = run(one("customs_declarations", {"shipment_id": s["id"]}))
    run(file_and_release(d["id"]))
    d = run(one("customs_declarations", {"id": d["id"]}))
    assert d["status"] == "ready_for_review" and d["mrn"] is None and not d.get("submission")
    card = run(one("approvals", {"kind": "customs_filing", "ref_id": d["id"]}))
    assert card["status"] == "pending" and card["legal_point"] == "customs_filing_not_delegated"
    assert codes(card["reasons"]) == ["legal"] and d["approval_id"] == card["id"]
    gate = [x for x in run(decisions("customs_agent", d["id"])) if x["action"] == "file_declaration"]
    assert [(x["outcome"], x["legal_point"]) for x in gate] == [("owner_queue", "customs_filing_not_delegated")]


@pytest.mark.parametrize("missing", ["Y904", "Y900", "Y922"])
def test_customs_missing_supplier_exemption_blocks_even_with_delegation(missing):
    from services import customs
    run(autopilot(True))
    run(put_settings(customs_filing_delegated=True))
    try:
        s = run(import_shipment("missing-" + missing))
        run(confirm_and_book(s["id"]))
        run(upload_invoice(s["id"], "INV-MISSING-" + missing, omit_exemption=missing))
        d = run(one("customs_declarations", {"shipment_id": s["id"]}))
        run(file_and_release(d["id"]))
        d = run(one("customs_declarations", {"id": d["id"]}))
        blocks = customs.blocking(d)
        assert [b["rule"] for b in blocks] == ["LIC-1"], blocks
        assert missing in blocks[0]["message"]
        assert not d.get("submission") and not d.get("mrn")
        assert not run(one("approvals", {"kind": "customs_filing", "ref_id": d["id"]}))
    finally:
        run(put_settings(customs_filing_delegated=False))


# --------------------------------------------------------------------------------------------- tracking.ingest (webhook)
def test_tracking_webhook_checks_the_signature_stores_once_and_applies_the_milestone():
    import httpx
    from fastapi import FastAPI
    from routes import webhooks_feeds

    app = FastAPI()
    app.include_router(webhooks_feeds.router)
    s = run(make_shipment("co-real", "tr1"))
    body = json.dumps({"event_id": "acme-0001", "shipment_ref": s["reference"], "code": "departed", "leg_seq": 1,
                       "at": "2026-10-24T06:00:00Z", "vessel": {"name": "ACME STAR", "imo": "9300001"}}).encode()
    good = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    async def post(sig: str):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://huolitsija.test") as c:
            return await c.post("/api/webhooks/tracking/acme", content=body,
                                headers={"Content-Type": "application/json", "X-Huolitsija-Signature": sig})

    bad = run(post(hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()))
    assert bad.status_code == 401 and run(find("feed_events", {"provider": "acme"})) == []
    r = run(post(good))
    assert r.status_code == 202 and r.json() == {"ok": True, "accepted": 1, "duplicates": 0}
    rec = run(one("feed_events", {"key": "acme:acme-0001"}))
    assert (rec["status"], rec["verified"], rec["event"]["shipment_ref"]) == ("queued", True, s["reference"])
    assert run(drain("tracking.ingest")) == 1
    assert run(one("feed_events", {"key": "acme:acme-0001"}))["status"] == "applied"
    got = run(shipment(s["id"]))
    dep = [m for m in got["milestones"] if m["code"] == "departed"]
    assert got["status"] == "in_transit" and len(dep) == 1
    assert (dep[0]["at"], dep[0]["source"]) == ("2026-10-24T06:00:00Z", "carrier_api")
    assert got["legs"][0]["atd"] == "2026-10-24T06:00:00Z" and got["legs"][0]["vessel"]["name"] == "ACME STAR"

    again = run(post("sha256=" + good))
    assert again.status_code == 202 and again.json() == {"ok": True, "accepted": 0, "duplicates": 1}
    run(drain("tracking.ingest"))
    assert len(run(find("feed_events", {"provider": "acme"}))) == 1
    assert len(run(find("jobs", {"kind": "tracking.ingest", "key": "feed:acme:acme-0001"}))) == 1
    assert len(run(find("tracking_events", {"shipment_id": s["id"], "code": "departed"}))) == 1


# --------------------------------------------------------------------------------------------- simulator.tick
async def _tick(n: int, sid: str):
    from core.db import db
    from core.jobs import enqueue
    await db.raw.shipments.update_one({"id": sid}, {"$unset": {"simulator.next_at": ""}})
    j = await enqueue("simulator.tick", {}, key=f"tick:{sid}:{n}")
    await drain("simulator.tick")
    await drain("tracking.ingest")
    return ((await one("jobs", {"id": j["id"]}))["result"] or {})


def test_simulator_moves_a_sandbox_shipment_milestone_by_milestone_and_never_a_real_one():
    from services import carrier_booking as cb

    run(autopilot(True))
    run(put_settings(customs_filing_delegated=True))
    try:
        s = run(import_shipment("sim1"))
        s = run(confirm_and_book(s["id"]))
        ref = s["reference"]
        run(upload_invoice(s["id"], "INV-SIM1"))
        decl = run(one("customs_declarations", {"shipment_id": s["id"]}))
        assert decl["status"] == "draft"
        real = run(make_shipment("co-real", "sim-real"))
        b = run(cb.book_leg(real["id"], 1, W["ALPHA"], OPS))
        run(cb.confirm_booking(b["id"], carrier_ref="ALPHA-REAL-1", actor=OPS))
        real_before = run(shipment(real["id"]))
        assert real_before["legs"][0]["booking"]["status"] == "confirmed"

        def sim_events():
            return run(find("feed_events", {"provider": "simulator", "event.shipment_id": s["id"]}, sort=[("received_at", 1)]))

        assert run(_tick(1, s["id"])).get(ref) == "departed"
        got = run(shipment(s["id"]))
        assert got["status"] == "in_transit" and [e["event"]["code"] for e in sim_events()] == ["departed"]
        assert next(m for m in got["milestones"] if m["code"] == "departed")["source"] == "simulator"
        eta_before = got["legs"][0]["eta"]

        assert run(_tick(2, s["id"])).get(ref) == "exception"
        ex = run(find("exceptions", {"shipment_id": s["id"]}))
        assert len(ex) == 1 and (ex[0]["type"], ex[0]["severity"], ex[0]["detected_by"]) == ("delay", "medium", "carrier")
        from core.util import parse_iso
        assert parse_iso(run(shipment(s["id"]))["legs"][0]["eta"]) - parse_iso(eta_before) == timedelta(hours=18)

        assert run(_tick(3, s["id"])).get(ref) == "arrived"
        assert run(shipment(s["id"]))["status"] == "at_hub"
        assert run(_tick(4, s["id"])).get(ref) == "customs_hold"
        got = run(shipment(s["id"]))
        assert got["status"] == "customs_hold" and [h["type"] for h in got["holds"]] == ["customs"]
        assert run(_tick(5, s["id"])).get(ref) is None    # held until Tulli releases
        assert run(shipment(s["id"]))["status"] == "customs_hold" and len(sim_events()) == 3

        run(file_and_release(decl["id"]))
        assert run(one("customs_declarations", {"id": decl["id"]}))["status"] == "released"
        got = run(shipment(s["id"]))
        assert got["status"] == "at_hub" and got["holds"] == []
        assert next(m for m in got["milestones"] if m["code"] == "customs_released")["at"]

        assert run(_tick(6, s["id"])).get(ref) == "delivered"
        got = run(shipment(s["id"]))
        assert got["status"] == "delivered" and got["delivered_at"]
        pods = run(find("documents", {"shipment_id": s["id"], "type": "pod"}))
        assert len(pods) == 1 and pods[0]["filename"] == f"POD-{ref}.pdf" and pods[0]["source"] == "carrier"
        assert run(_tick(7, s["id"])).get(ref) is None
        evs = sim_events()
        assert [e["event"]["code"] for e in evs] == ["departed", "exception", "arrived", "delivered"]
        assert all(e["status"] == "applied" for e in evs)
        assert evs[-1]["event"]["pod"]["signed_by"] == "Ville Vastaanottaja"

        real_after = run(shipment(real["id"]))
        assert real_after["milestones"] == real_before["milestones"] and real_after["status"] == "booked"
        assert "simulator" not in real_after
        assert run(find("feed_events", {"provider": "simulator", "event.shipment_id": real["id"]})) == []
    finally:
        run(put_settings(customs_filing_delegated=False))


# --------------------------------------------------------------------------------------------- exceptions.handle
def _delay_event(s: dict, tag: str, severity: str = "medium") -> dict:
    from core.util import iso, parse_iso
    new_eta = iso(parse_iso(s["legs"][0]["eta"]) + timedelta(hours=18))
    return {"event_id": f"exc-{tag}", "shipment_ref": s["reference"], "code": "exception", "leg_seq": 1,
            "at": "2026-10-24T07:00:00Z",
            "exception": {"type": "delay", "severity": severity, "new_eta": new_eta,
                          "title": "Vessel delayed by port congestion", "title_fi": "Alus myöhässä satamaruuhkan vuoksi",
                          "description": f"New ETA {new_eta}", "description_fi": f"Uusi ETA {new_eta}"}}


def test_exception_agent_tells_the_customer_the_new_eta_and_resolves_a_delay():
    run(autopilot(True))
    s = run(make_shipment("co-sbx", "ex1"))
    ev = _delay_event(s, "ex1")
    run(feed("acme", ev))
    run(drain("tracking.ingest"))
    ex = run(one("exceptions", {"shipment_id": s["id"]}))
    assert ex["status"] == "open" and ex["detected_by"] == "carrier"
    run(drain("exceptions.handle"))
    ex = run(one("exceptions", {"id": ex["id"]}))
    new_eta = ev["exception"]["new_eta"]
    assert ex["status"] == "resolved" and ex["resolved_by"]["name"] == "Autopilot · Exceptions"
    assert ex["resolution"] == f"Customer informed of the new ETA {new_eta[:16]} UTC (Autopilot)."
    assert ex["agent"]["done"] == ["customer_notified"] and ex["agent"]["handled_at"]
    msgs = run(find("messages", {"shipment_id": s["id"], "author.type": "system", "author.id": "autopilot"}))
    assert len(msgs) == 1 and msgs[0]["status"] == "sent" and msgs[0]["visibility"] == "customer"
    assert msgs[0]["body"] == (f"Lähetyksessä {s['reference']} on poikkeama: Alus myöhässä satamaruuhkan vuoksi. "
                               f"Uusi arvioitu saapuminen on {fi_dt(new_eta)}. Seuraamme tilannetta ja ilmoitamme, "
                               f"jos aikataulu muuttuu.")
    assert run(shipment(s["id"]))["legs"][0]["eta"] == new_eta
    d = run(decisions("exception_agent", ex["id"]))
    assert [(x["outcome"], x["decision"]["actions"]) for x in d] == [("auto", ["notify_customer", "resolve"])]


def test_exception_agent_hands_the_plan_to_the_owner_when_autopilot_is_off_or_the_exception_is_critical():
    run(autopilot(False))
    off = run(make_shipment("co-sbx", "ex-off"))
    run(feed("acme", _delay_event(off, "ex-off")))
    run(drain("tracking.ingest"))
    run(drain("exceptions.handle"))
    run(autopilot(True))
    crit = run(make_shipment("co-sbx", "ex-crit"))
    run(feed("acme", _delay_event(crit, "ex-crit", severity="critical")))
    run(drain("tracking.ingest"))
    run(drain("exceptions.handle"))
    for s, code in ((off, "autopilot_off"), (crit, "critical")):
        ex = run(one("exceptions", {"shipment_id": s["id"]}))
        card = run(one("approvals", {"kind": "exception_action", "ref_id": ex["id"]}))
        assert card["status"] == "pending" and codes(card["reasons"]) == [code]
        assert card["data"]["plan"]["actions"] == ["notify_customer", "resolve"]
        assert ex["status"] == "open" and ex["agent"]["owner_queue"] == card["id"] and not ex["agent"].get("handled_at")
        assert run(find("messages", {"shipment_id": s["id"], "author.id": "autopilot"})) == []


# --------------------------------------------------------------------------------------------- delivery.confirm
def test_delivery_agent_confirms_with_a_pod_and_waits_then_asks_the_owner_without_one():
    from core.db import db
    from core.util import iso, now

    run(autopilot(True))
    with_pod = run(make_shipment("co-real", "dl-pod"))
    no_pod = run(make_shipment("co-real", "dl-nopod"))
    for s, tag in ((with_pod, "pod"), (no_pod, "nopod")):
        run(feed("acme", {"event_id": f"dep-{tag}", "shipment_ref": s["reference"], "code": "departed", "leg_seq": 1}))
        run(drain("tracking.ingest"))
        ev = {"event_id": f"del-{tag}", "shipment_ref": s["reference"], "code": "delivered", "leg_seq": 1}
        if tag == "pod":
            ev["pod"] = {"signed_by": "H. Hafen", "at": "2026-10-24T09:00:00Z"}
        run(feed("acme", ev))
        run(drain("tracking.ingest"))
        assert run(shipment(s["id"]))["status"] == "delivered"
    run(drain("delivery.confirm"))

    pod = run(one("documents", {"shipment_id": with_pod["id"], "type": "pod"}))
    dc = run(shipment(with_pod["id"]))["delivery_confirmation"]
    assert (dc["status"], dc["basis"], dc["pod_document_id"]) == ("confirmed", "pod", pod["id"])
    assert [x["outcome"] for x in run(decisions("delivery_agent", with_pod["id"]))] == ["auto"]

    job = run(one("jobs", {"key": f"delivery:{no_pod['id']}:on"}))
    assert (job["status"], job["waiting_for"], job["defer_count"]) == ("queued", "waiting for the proof of delivery", 1)
    assert "delivery_confirmation" not in run(shipment(no_pod["id"]))
    run(db.raw.shipments.update_one({"id": no_pod["id"]}, {"$set": {"delivered_at": iso(now() - timedelta(hours=3))}}))
    run(db.raw.jobs.update_one({"id": job["id"]}, {"$set": {"next_run_at": iso(now() - timedelta(seconds=1))}}))
    run(drain("delivery.confirm"))
    card = run(one("approvals", {"kind": "exception_action", "ref_id": no_pod["id"]}))
    assert card["status"] == "pending" and codes(card["reasons"]) == ["no_pod"] and card["data"]["action"] == "confirm_delivery"
    assert "delivery_confirmation" not in run(shipment(no_pod["id"]))
    run(approve_card(card["id"]))
    dc = run(shipment(no_pod["id"]))["delivery_confirmation"]
    assert (dc["status"], dc["basis"], dc["by"]["id"]) == ("confirmed", "owner", "u-ops")


# --------------------------------------------------------------------------------------------- invoices
def _assert_issued(inv: dict, by: str):
    assert inv["status"] == "open" and re.fullmatch(r"HUO-INV-\d{4}-\d{6}", inv["number"])
    assert inv["issued_by"]["id"] == by and inv["issue_date"] and inv["due_date"] > inv["issue_date"]
    assert viite_ok(inv["reference"]) and inv["rf_reference"].replace(" ", "").startswith("RF") and rf_ok(inv["rf_reference"])
    assert inv["subtotal_cents"] == sum(ln["amount_cents"] for ln in inv["lines"]) > 0
    assert inv["total_cents"] == inv["subtotal_cents"] + inv["vat_cents"]


def test_invoice_agent_issues_on_delivery_and_waits_while_customs_is_pending():
    from core.jobs import enqueue
    from services import billing, customs

    run(autopilot(True))
    s = run(make_shipment("co-real", "inv1"))
    run(deliver_manually(s["id"]))
    run(drain("invoice.issue_on_delivery", "invoice.issue"))
    inv = run(one("invoices", {"shipment_id": s["id"], "kind": "invoice"}))
    _assert_issued(inv, "agent:invoice_agent")
    assert sorted(ln["amount_cents"] for ln in inv["lines"]) == [3500, 177000] and inv["warnings"] == []
    # the customer's INVOICED milestone carries the issued invoice number, never "draft"
    ms = [m for m in run(one("shipments", {"id": s["id"]}))["milestones"] if (m.get("code") or "").lower() == "invoiced"]
    assert [m.get("note") for m in ms] == [inv["number"]], ms
    assert [x["outcome"] for x in run(decisions("invoice_agent", inv["id"]))] == ["auto"]

    pending = run(import_shipment("inv-customs"))
    run(customs.draft_declaration_for_shipment(pending["id"], OPS, classify=False))
    run(deliver_manually(pending["id"]))
    run(drain("invoice.issue_on_delivery", "invoice.issue"))
    job = run(one("jobs", {"key": f"invoice:{pending['id']}:delivered"}))
    assert job["status"] == "queued" and job["waiting_for"] == "waiting for Tulli's decision before the customs lines can be invoiced"
    draft = run(one("invoices", {"shipment_id": pending["id"], "kind": "invoice"}))
    assert draft["status"] == "draft" and draft["number"] is None
    assert draft["warnings"] == ["The import declaration is not released yet; duty and import VAT are not on this invoice."]

    # invoice.issue on a draft made without an agent (billing with no actor leaves it a draft)
    s3 = run(make_shipment("co-real", "inv3"))
    run(deliver_manually(s3["id"]))
    d3 = run(billing.create_invoice_for_shipment(s3["id"], None))
    assert d3["status"] == "draft"
    assert not any((m.get("code") or "").lower() == "invoiced" for m in run(one("shipments", {"id": s3["id"]}))["milestones"])
    run(enqueue("invoice.issue", {"invoice_id": d3["id"]}, key=f"invoice-issue:{d3['id']}"))
    run(drain("invoice.issue"))
    d3 = run(one("invoices", {"id": d3["id"]}))
    _assert_issued(d3, "agent:invoice_agent")
    ms = [m for m in run(one("shipments", {"id": s3["id"]}))["milestones"] if (m.get("code") or "").lower() == "invoiced"]
    assert [m.get("note") for m in ms] == [d3["number"]], ms


def test_invoice_with_autopilot_off_is_a_card_and_approving_it_issues_the_invoice():
    run(autopilot(False))
    s = run(make_shipment("co-real", "inv-off"))
    run(deliver_manually(s["id"]))
    run(drain("invoice.issue_on_delivery", "invoice.issue"))
    inv = run(one("invoices", {"shipment_id": s["id"], "kind": "invoice"}))
    assert inv["status"] == "draft" and inv["number"] is None
    card = run(one("approvals", {"kind": "invoice_issue", "ref_id": inv["id"]}))
    assert card["status"] == "pending" and codes(card["reasons"]) == ["autopilot_off"]
    assert card["data"]["invoice_id"] == inv["id"] and card["amount_cents"] == inv["total_cents"]
    run(approve_card(card["id"]))
    _assert_issued(run(one("invoices", {"id": inv["id"]})), "u-ops")


# --------------------------------------------------------------------------------------------- claims.decide
async def _claim(s: dict, ctype: str, claimed: int, kg: float | None = None, packages: int | None = None) -> dict:
    from services import claims, documents
    docs = []
    if ctype != "delay":
        d = await documents.create_document(company_id=s["company_id"], shipment_id=s["id"], doc_type="other",
                                            filename=f"damage-{s['reference']}.pdf",
                                            data=evidence_pdf(f"Damage report {s['reference']}"), actor=CUSTOMER,
                                            extract=False)
        docs = [d["id"]]
    body = {"shipment_id": s["id"], "type": ctype, "description": f"{ctype} reported on delivery of {s['reference']}",
            "amount_claimed_cents": claimed, "gross_kg_affected": kg, "packages_affected": packages,
            "document_ids": docs, "submit": True}
    return await claims.create_claim(body, CUSTOMER, company_id=s["company_id"])


def test_claims_agent_pays_inside_the_limits_and_sends_larger_claims_to_the_owner():
    from core.db import db
    from core.util import iso, parse_iso

    run(autopilot(True))
    run(put_settings(guard_claim_limit_cents=100000, insurance_claim_limit_cents=300000))
    ships = {}
    for tag, regime in (("cl-dmg", "HAGUE_VISBY"), ("cl-delay", "NSAB"), ("cl-big", "HAGUE_VISBY"), ("cl-ins", "HAGUE_VISBY")):
        ships[tag] = run(make_shipment("co-sbx", tag, regime=regime))
        run(deliver_manually(ships[tag]["id"]))
    late = run(shipment(ships["cl-delay"]["id"]))  # the plan promised delivery 30 hours before it happened
    planned = iso(parse_iso(late["delivered_at"]) - timedelta(hours=30))
    run(db.raw.shipments.update_one({"id": late["id"], "milestones.code": "delivered"},
                                    {"$set": {"milestones.$.planned": planned}}))

    dmg = run(_claim(ships["cl-dmg"], "damage", 150000, kg=400, packages=1))      # cap max(667, 2 × 400) SDR = 800 SDR
    delay = run(_claim(ships["cl-delay"], "delay", 50000))                         # NSAB: delay capped at the freight
    big = run(_claim(ships["cl-big"], "damage", 150000, kg=2000, packages=1))     # cap 4 000 SDR, above the claim limit
    ins = run(_claim(ships["cl-ins"], "damage", 350000, kg=2000, packages=1))     # above the insurer's authority
    cap_dmg, cap_big = round(800 * SDR_EUR * 100), round(4000 * SDR_EUR * 100)   # 96 000 and 480 000 cents
    assert (dmg["cap_eur_cents"], delay["cap_eur_cents"], big["cap_eur_cents"]) == (cap_dmg, 180500, cap_big)
    assert all(c["status"] == "submitted" and c["notice_late"] is False for c in (dmg, delay, big, ins))
    run(drain("claims.decide"))

    for cl, amount in ((dmg, cap_dmg), (delay, 50000)):
        got = run(one("claims", {"id": cl["id"]}))
        assert got["status"] == "approved" and got["decision"]["amount_cents"] == amount, got["number"]
        assert got["proposal"]["decision"] == "approve" and got["proposal"]["amount_cents"] == amount
        card = run(one("approvals", {"id": got["approval_id"]}))
        assert card["kind"] == "claim" and card["status"] == "approved" and card["decided_by"]["id"] == "agent:claims_agent"
        d = run(decisions("claims_agent", cl["id"]))
        assert [(x["outcome"], x["decision"]["amount_cents"]) for x in d] == [("auto", amount)]

    for cl, code, legal in ((big, "claim_limit", None), (ins, "legal", "claim_above_insurance_limit")):
        got = run(one("claims", {"id": cl["id"]}))
        assert got["status"] == "in_review" and got["proposal"]["decision"] == "approve"  # never auto-rejected
        assert got["proposal"]["amount_cents"] == cl["amount_claimed_cents"]
        card = run(one("approvals", {"id": got["approval_id"]}))
        assert card["kind"] == "claim" and card["status"] == "pending" and codes(card["reasons"]) == [code]
        assert card.get("legal_point") == legal and card["data"]["decision"] == "approve"
        d = run(decisions("claims_agent", cl["id"]))
        assert [(x["outcome"], x["legal_point"]) for x in d] == [("owner_queue", legal)]
        assert d[0]["approval_id"] == card["id"]


# --------------------------------------------------------------------------------------------- documents.review
def test_structured_csv_invoice_is_read_exactly_and_the_document_agent_accepts_it():
    run(autopilot(True))
    doc = run(upload_invoice(None, "INV-DOC-1"))
    ex = doc["extraction"]
    assert (ex["status"], ex["source"], ex["model"], ex["confidence"]) == ("done", "structured", "rules/structured-sheet", 0.99)
    f = ex["fields"]
    assert {k: f[k]["value"] for k in ("seller_city", "seller_postal_code", "transaction_nature")} == {
        "seller_city": "New York", "seller_postal_code": "10005", "transaction_nature": "11"}
    assert f["invoice_number"] == {"value": "INV-DOC-1", "confidence": 0.99, "source": "document"}
    assert {k: f[k]["value"] for k in ("invoice_date", "seller_name", "buyer_name", "incoterm", "incoterm_place", "currency",
                                       "total_amount", "country_of_origin", "total_gross_weight_kg")} == {
        "invoice_date": "2026-10-20", "seller_name": "Hudson Valley Furniture Works Inc", "buyer_name": "Sandbox Tuonti Oy",
        "incoterm": "FOB", "incoterm_place": "New York", "currency": "EUR", "total_amount": 2500.0,
        "country_of_origin": "US", "total_gross_weight_kg": 520.0}
    assert all(v["source"] == "document" for v in f.values())
    it = ex["line_items"]
    assert len(it) == 1
    assert {k: it[0][k] for k in ("description", "quantity", "unit_price", "amount", "hs_code", "country_of_origin",
                                  "net_weight_kg", "gross_weight_kg")} == {
        "description": CHAIRS, "quantity": 20.0, "unit_price": 125.0, "amount": 2500.0, "hs_code": "94016100",
        "country_of_origin": "US", "net_weight_kg": 480.0, "gross_weight_kg": 520.0}
    assert doc["number"] == "INV-DOC-1" and [w for w in ex["warnings"] if w["severity"] == "error"] == []
    run(drain("documents.review"))
    got = run(one("documents", {"id": doc["id"]}))
    d = run(decisions("document_agent", doc["id"]))
    assert [(x["outcome"], x["confidence"]) for x in d] == [("auto", 0.99)]
    assert got["agent_review"]["outcome"] == "auto" and got["agent_review"]["approval_id"] is None
    assert run(find("approvals", {"kind": "document_review", "ref_id": doc["id"]})) == []


def test_csv_invoice_missing_required_fields_needs_review_and_a_document_review_card():
    run(autopilot(True))
    doc = run(upload_invoice(None, "INV-DOC-2", complete=False))
    ex = doc["extraction"]
    assert ex["status"] == "needs_review" and ex["source"] == "structured" and ex["confidence"] == 0
    missing = sorted(w["field"] for w in ex["warnings"] if w["code"] == "missing" and w["severity"] == "error")
    assert missing == ["currency", "incoterm", "incoterm_place", "total_amount"]
    run(drain("documents.review"))
    card = run(one("approvals", {"kind": "document_review", "ref_id": doc["id"]}))
    assert card["status"] == "pending"
    assert codes(card["reasons"]) == ["low_confidence", "needs_review", "check_missing", "check_missing", "check_missing"]
    got = run(one("documents", {"id": doc["id"]}))
    assert got["agent_review"]["outcome"] == "owner_queue" and got["agent_review"]["approval_id"] == card["id"]


# --------------------------------------------------------------------------------------------- messages.answer
async def _customer_question(s: dict, text: str) -> dict:
    """A customer message that the event path missed (restart): the state messages.answer picks up."""
    from core.db import db
    from core.util import new_id, now_iso
    from services import messaging
    author = {"type": "customer", "id": CUSTOMER["id"], "name": CUSTOMER["name"], "role": CUSTOMER["role"]}
    t = await messaging.get_or_create_thread(company_id=s["company_id"], shipment_id=s["id"], subject=s["reference"],
                                             created_by=author)
    at = now_iso()
    m = {"id": new_id(), "thread_id": t["id"], "company_id": s["company_id"], "shipment_id": s["id"], "author": author,
         "body": text, "attachments": [], "visibility": "customer", "kind": "message", "status": "sent", "approval_id": None,
         "ai": None, "routine_key": None, "created_at": at, "sent_at": at, "demo": False}
    await db.raw.messages.insert_one(dict(m))
    await db.raw.threads.update_one({"id": t["id"]}, {"$set": {"last_message_at": at, "last_author_type": "customer",
                                                               "needs_reply": True, "last_message_preview": text},
                                                      "$inc": {"unread_ops": 1}})
    return {"thread": t, "message": m}


async def _answer(q: dict):
    from core.jobs import enqueue
    t = q["thread"]
    await enqueue("messages.answer", {"thread_id": t["id"], "message_id": q["message"]["id"]},
                  key=f"answer:{t['id']}:{q['message']['created_at']}", subject=("thread", t["id"]))
    await drain("messages.answer")
    replies = await find("messages", {"thread_id": t["id"], "author.type": {"$in": ["system", "ai"]}})
    return await one("threads", {"id": t["id"]}), replies


def test_status_question_without_ai_gets_the_rule_based_finnish_answer_or_waits_when_autopilot_is_off():
    run(autopilot(True))
    s = run(make_shipment("co-sbx", "msg-on"))
    thread, replies = run(_answer(run(_customer_question(s, "Missä lähetys on nyt?"))))
    assert len(replies) == 1 and thread["ai_status"] == "sent"
    r = replies[0]
    assert (r["status"], r["author"]["type"], r["author"]["id"], r["ai"]["rule"]) == ("sent", "system", "comms_agent", "status_question")
    assert r["body"].startswith(f"Lähetys {s['reference']} on nyt tilassa: varattu. Viimeisin tapahtuma: ")
    assert f" Arvioitu saapuminen {fi_date(s['eta'])}. Ajantasainen sijainti näkyy lähetyksen sivulla." in r["body"]
    d = run(decisions("comms_agent", thread["id"]))
    assert [(x["action"], x["outcome"], x["model"]) for x in d] == [("answer_status_question", "auto", "rules/status-template")]

    run(autopilot(False))
    s2 = run(make_shipment("co-sbx", "msg-off"))
    thread, replies = run(_answer(run(_customer_question(s2, "Milloin lähetys saapuu?"))))
    assert len(replies) == 1 and replies[0]["status"] == "pending_approval" and thread["ai_status"] == "draft_ready"
    card = run(one("approvals", {"kind": "ai_message", "ref_id": replies[0]["id"]}))
    assert card["status"] == "pending" and card["reason"] == "Autopilot is off"


@pytest.mark.parametrize("text", ["Missä lähetys on? Milloin se saapuu?", "Mikä on hinta ja milloin lähetys saapuu?"])
def test_two_questions_or_a_price_question_need_a_person_and_nothing_is_sent(text):
    run(autopilot(True))
    s = run(make_shipment("co-sbx", f"msg-p{len(text)}"))
    thread, replies = run(_answer(run(_customer_question(s, text))))
    assert replies == [] and thread["ai_status"] == "needs_person"
    assert thread["ai_note"].startswith("AI is not configured: ")
    assert run(decisions("comms_agent", thread["id"])) == []


def test_finnish_status_answer_has_no_english_milestone_label():
    run(autopilot(True))
    s = run(make_shipment("co-sbx", "msg-fi"))
    _thread, replies = run(_answer(run(_customer_question(s, "Missä lähetys on nyt?"))))
    assert len(replies) == 1
    assert "Booking confirmed" not in replies[0]["body"], replies[0]["body"]


# --------------------------------------------------------------------------------------------- rates.update
def test_rates_agent_asks_for_one_renewal_per_carrier_and_none_when_a_successor_exists():
    from core.db import db
    from core.jobs import enqueue

    today = W["today"]
    for code in ("RATE1", "RATE2", "RATE3"):
        run(db.raw.carriers.insert_one({"id": f"car-{code.lower()}", "code": code, "name": f"{code.title()} Lines",
                                        "modes": ["sea"], "active": True}))

    def card(cid, tag, o, d, valid_to_days):
        return {"id": f"card-{tag}", "carrier_id": cid, "mode": "sea", "service": "FCL", "basis": "PER_CONTAINER",
                "origin": {"unlocode": o, "radius_km": 40}, "destination": {"unlocode": d, "radius_km": 40},
                "container_costs": {"40HC": 100000}, "valid_from": (today - timedelta(days=30)).isoformat(),
                "valid_to": (today + timedelta(days=valid_to_days)).isoformat(), "currency": "EUR", "archived": False}
    run(db.raw.rate_cards.insert_many([
        card("car-rate1", "r1a", "FIHEL", "DEHAM", 5), card("car-rate1", "r1b", "FIHEL", "NLRTM", 7),
        card("car-rate2", "r2a", "FIKTK", "DEHAM", 3),
        card("car-rate3", "r3a", "FIHEL", "DEHAM", 4), card("car-rate3", "r3b", "FIHEL", "DEHAM", 120)]))
    run(enqueue("rates.update", {}, key="rates:pytest"))
    run(drain("rates.update"))
    job = run(one("jobs", {"key": "rates:pytest"}))
    assert job["status"] == "done", job.get("last_error")
    cards = {a["ref_id"]: a for a in run(find("approvals", {"kind": "rate_update"}))}
    assert sorted(cards) == ["car-rate1", "car-rate2"]
    assert (cards["car-rate1"]["data"]["count"], cards["car-rate2"]["data"]["count"]) == (2, 1)
    assert codes(cards["car-rate1"]["reasons"])[-1] == "rates_ending"


def test_exception_plan_reads_the_visibility_rule():
    from services.agents.exception_agent import plan_for

    s = {"eta": "2026-11-20T10:00:00+00:00", "legs": [{"seq": 1, "atd": "2026-11-01T08:00:00+00:00", "eta": "2026-11-10T10:00:00+00:00"}]}
    base = {"type": "delay", "detected_by": "carrier", "evidence": {"leg_seq": 1, "event": {"exception": {"new_eta": "2026-11-20T10:00:00+00:00"}}}}
    # rolled over / missed cut-off: rebook on the next sailing although the booked vessel has departed
    for rule in ("R-ROLLOVER", "R-CUTOFF"):
        p = plan_for({**base, "rule": rule}, s)
        assert p["rule"] == rule and p["actions"] == ["rebook", "notify_customer", "resolve"], p
    assert plan_for({**base, "rule": "R-ROLLOVER", "detected_by": "rule"}, s)["actions"] == ["rebook", "notify_customer"]
    # congestion: notify and re-plan only, never a rebook after departure (even when s10 maps it to capacity)
    for t in ("delay", "capacity"):
        p = plan_for({**base, "type": t, "rule": "R-CONGESTION"}, s)
        assert "rebook" not in p["actions"] and "notify_customer" in p["actions"] and p["new_eta"] == s["eta"], p
    # no rule: unchanged (a departed leg is not rebooked for a delay)
    assert "rebook" not in plan_for(base, s)["actions"]


# --------------------------------------------------------------------------------------------- request from s7
def test_quote_autosend_holds_a_quote_whose_cost_is_unknown_for_the_margin_floor():
    from core.db import db
    from core.jobs import enqueue
    run(autopilot(False))
    q = run(_pending_quote("floor", 600000, 480000))    # inside the Autopilot cap and margin, but s7's floor check fails
    run(db.raw.quotes.update_one({"id": q["id"]}, {"$set": {"options.$[].cost_basis": "unknown"}}))
    run(autopilot(True))
    aid = q["approval"]["approval_id"]
    run(enqueue("quote.autosend", {"approval_id": aid}, key=f"quote:{aid}:floor", subject=("quote", q["id"])))
    assert run(drain("quote.autosend")) == 1
    a = run(one("approvals", {"id": aid}))
    assert a["status"] == "pending" and "cost_unknown" in codes(a["reasons"])
    assert run(one("quotes", {"id": q["id"]}))["status"] == "pending_approval"
    r = next(x for x in a["reasons"] if x["code"] == "cost_unknown")
    assert "ostohinta ei ole tiedossa" in r["fi"]
