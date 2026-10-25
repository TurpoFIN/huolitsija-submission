"""World inputs use the application's normal inbound APIs and parsers."""
from email.message import EmailMessage
from email.utils import formatdate
import hashlib
from io import BytesIO
import re

import httpx
from lxml import etree as E

from core import config
from core.db import db
from core.util import now, now_iso
from services.demo_autoquote import conf


async def mail(run, actor, outgoing, text, *, key, attachments=()):
    from core.email import fit_references
    from services.email_channel.dispatch import dispatch_raw
    m = EmailMessage()
    mid = "<world-" + hashlib.sha256(key.encode()).hexdigest()[:30] + "@demo.huolitsija.invalid>"
    m["Message-ID"], m["Date"] = mid, formatdate(localtime=False)
    m["From"] = actor["address"]
    m["To"] = outgoing.get("reply_to") or conf.inbound_address()
    m["Subject"] = "Re: " + (outgoing.get("subject") or "")
    m["X-Huolitsija-Demo-Simulated"] = "1"
    # This is the receiving provider's boundary assertion, not mail text parsed by agents.
    m["Authentication-Results"] = "demo-inbound; dkim=pass; spf=pass; dmarc=pass"
    ref = outgoing.get("message_id")
    if ref:
        m["In-Reply-To"] = ref
        m["References"] = fit_references(ref)
    m.set_content(text)
    for name, ctype, blob in attachments:
        main, sub = ctype.split("/", 1)
        m.add_attachment(blob, maintype=main, subtype=sub, filename=name)
    if outgoing.get("channel") == "execution":
        from services.execution.mail import ingest
        result = await ingest(m.as_bytes())
    else:
        result = await dispatch_raw(m.as_bytes(), provider_id="demo-sim:" + key, provider_authenticated=True, source="demo-world")
    return {"message_id": mid, "result": result, "subject": m["Subject"], "text": text, "from": actor["address"], "to": m["To"]}


def source_documents(run, parties):
    """The world shipper supplies actual PDF files from its supplied cargo facts.

    Huolitsija subsequently extracts these files, researches classification and
    drafts its own trade/transport/customs documents. No extracted data is injected.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import simpleSplit
    p = run.get("profile") or {}
    required = ("cargo_description", "packages", "weight_kg", "net_weight_kg", "value_eur", "origin_country")
    if any(p.get(k) in (None, "") for k in required):
        return []
    shipper, consignee = parties.get("shipper") or {}, parties.get("consignee") or {}
    result = []
    for title, name in (("Commercial invoice", "commercial-invoice.pdf"), ("Packing list", "packing-list.pdf")):
        out = BytesIO()
        c = canvas.Canvas(out, pagesize=A4)
        lines = [title, "Number: CW-" + run["id"][:8], "Date: " + now().date().isoformat()]
        labels = ("Seller", "Buyer") if title == 'Commercial invoice' else ("Shipper", "Consignee")
        for label, party in zip(labels, (shipper, consignee)):
            lines.append(f"{label}: {party.get('name', '')}")
            if party.get('contact_name'):
                lines.append(f"{label} contact: {party['contact_name']}")
            address = party.get("address") or {}
            lines.append(f"{label} address: " + ", ".join(str(address[k]) for k in ("street", "postal_code", "city", "country") if address.get(k)))
        lines += [f"Goods: {p['cargo_description']}", "Quantity: 1 lot", f"Packages: {p['packages']}",
                  f"Total gross weight kg: {p['weight_kg']}", f"Total net weight kg: {p['net_weight_kg']}",
                  f"Country of origin: {p['origin_country']}", f"Total amount EUR: {p['value_eur']}",
                  f"Unit price EUR: {p['value_eur']}", "Currency: EUR"]
        for key, label in (("hs_code", "HS code"), ("incoterm", "Incoterm"), ("incoterm_place", "Incoterm place"), ("package_type", "Package type"), ("dimensions", "Dimensions")):
            if p.get(key):
                lines.append(f"{label}: {p[key]}")
        if not p.get('incoterm_place') and p.get('incoterm') in ('EXW','FCA','DAP','DPU','DDP'):
            named_party = shipper if p['incoterm'] in ('EXW','FCA') else consignee
            address = named_party.get('address') or {}
            lines.append('Incoterm place: ' + ', '.join(str(address[k]) for k in ('street','city','country') if address.get(k)))
        y = 790
        for line in lines:
            for wrapped in simpleSplit(line, "Helvetica", 11, 480):
                c.setFont("Helvetica-Bold" if line == title else "Helvetica", 15 if line == title else 11)
                c.drawString(50, y, wrapped)
                y -= 20
                if y < 50:
                    c.showPage()
                    y = 790
        c.save()
        result.append((name, "application/pdf", out.getvalue()))
    return result


async def customer_portal(run, actor, outgoing):
    """Follow the actual one-time link, authenticate, fill profile and sign POA."""
    from server import app
    identity = run.get("requester_registry") or {}
    match = re.search(r"/api/email-channel/go/([A-Za-z0-9._-]+)", (outgoing.get("text") or "") + " " + (outgoing.get("html") or ""))
    if not match:
        return None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.local", timeout=150) as client:
        response = await client.get("/api/email-channel/go/" + match[1])
        if response.status_code != 303:
            raise ValueError("Customer's one-time link was not accepted")
        payload = {"name": identity["company"], "business_id": identity["business_id"], "vat_id": identity["vat_id"],
                   "eori": identity["eori"], "address": identity["address"], "signatory": identity["signatory"], "ubos": identity["ubos"],
                   "billing_email": actor["address"]}
        response = await client.patch("/api/company", json=payload)
        if response.status_code != 200:
            raise ValueError(f"Customer profile API returned {response.status_code}")
        today = now().date()
        response = await client.post("/api/company/poa", json={"type": "direct", "scope": ["import", "export", "correspondence"],
            "valid_from": today.isoformat(), "valid_to": today.replace(year=today.year + 1).isoformat(),
            "typed_name": identity["signatory"]["name"], "title": identity["signatory"].get("title"), "accept": True})
        if response.status_code != 200:
            raise ValueError(f"Customer POA API returned {response.status_code}")
        response = await client.post("/api/company/kyc/run")
        if response.status_code != 200:
            raise ValueError(f"Customer KYC API returned {response.status_code}")
        outcome = response.json()
        # Cookies are credentials: private world state only, never map data or logs.
        await db.raw.world_actors.update_one({"_id": actor["_id"]}, {"$set": {"session_cookies": dict(client.cookies)}})
        return {"kyc_status": outcome.get("kyc_status"), "sanctions_status": outcome.get("sanctions_status")}


async def customer_instructions(shipment, actor, instructions):
    from server import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.local",
                                 cookies=actor['session_cookies'], timeout=60) as client:
        response = await client.patch(f"/api/customs/shipments/{shipment['id']}/instructions", json=instructions)
    if response.status_code != 200:
        raise ValueError(f"Customer customs-instructions API returned {response.status_code}")
    return {"shipment_id": shipment['id'], "transaction_nature": response.json()['transaction_nature']}


async def customer_session(run, actor):
    """The same fictional customer may retain its login between demonstration runs.

    Reuse is proven by normal authentication and exact company/persona identity,
    never by a shared mailbox alone. No token is minted by the world actor.
    """
    bid = (run.get('requester_registry') or {}).get('business_id')
    company_id = run.get('company_id')
    if not bid or not company_id:
        return False
    from server import app
    async for previous in db.raw.world_actors.find({'actor':'w:customer','address':actor['address'],
            'session_cookies':{'$exists':True}},sort=[('created_at',-1)]):
        owner = await db.raw.demo_runs.find_one({'id':previous['run_id'],'company_id':company_id,
            'requester_registry.business_id':bid},{'id':1})
        if not owner:
            continue
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://demo.local',
                                     cookies=previous['session_cookies'],timeout=20) as client:
            response = await client.get('/api/auth/me')
        if response.status_code != 200:
            continue
        user = response.json().get('user') or {}
        company = response.json().get('company') or {}
        if user.get('org_type') != 'customer' or str(user.get('email') or '').lower() != actor['address'].lower() \
                or user.get('company_id') != company_id or company.get('business_id') != bid:
            continue
        await db.raw.world_actors.update_one({'_id':actor['_id']},{'$set':{'session_cookies':previous['session_cookies']}})
        return True
    return False


def inspection_report(shipment, exception, actor, facts):
    """The outside customer's written inspection evidence, never a photo claim."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas
    out = BytesIO()
    page = canvas.Canvas(out, pagesize=A4)
    lines = ['Cargo inspection report', 'Reference: IR-' + exception['id'],
        'Shipment: ' + shipment['reference'], 'Reported at: ' + now_iso(),
        'Observed event: ' + exception['title'], facts['description'],
        'Affected gross kg: ' + str(facts['gross_kg_affected']),
        'Affected packages: ' + str(facts['packages_affected']),
        'Amount claimed EUR: ' + f"{facts['amount_claimed_cents']/100:.2f}",
        'Reported by: ' + actor['name']]
    y = 790
    for line in lines:
        for wrapped in simpleSplit(line,'Helvetica',11,480):
            page.setFont('Helvetica',11)
            page.drawString(50,y,wrapped)
            y -= 20
            if y < 50:
                page.showPage()
                y = 790
    page.save()
    return out.getvalue()


async def customer_claim(shipment, exception, actor, facts):
    """Upload actual written evidence and submit using the customer's real session."""
    from server import app
    reference = 'IR-' + exception['id']
    description = reference + '\n' + facts['description']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://demo.local',
                                 cookies=actor['session_cookies'], timeout=150) as client:
        response = await client.get('/api/claims',params={'shipment_id':shipment['id']})
        if response.status_code != 200:
            raise ValueError(f'Customer claims API returned {response.status_code}')
        for claim in response.json()['items']:
            if claim.get('description') == description:
                if claim['status'] == 'draft':
                    submitted = await client.post('/api/claims/' + claim['id'] + '/submit')
                    if submitted.status_code != 200:
                        raise ValueError(f'Customer claim retry returned {submitted.status_code}')
                    claim = submitted.json()
                return {'id':claim['id'],'number':claim['number'],'status':claim['status']}
        response = await client.get('/api/documents',params={'shipment_id':shipment['id']})
        if response.status_code != 200:
            raise ValueError(f'Customer evidence API returned {response.status_code}')
        documents = response.json()['items']
        filename = reference + '.pdf'
        report = next((d for d in documents if d.get('filename') == filename),None)
        if report is None:
            response = await client.post('/api/documents',data={'shipment_id':shipment['id'],'type':'other'},
                files={'file':(filename,inspection_report(shipment,exception,actor,facts),'application/pdf')})
            if response.status_code != 200:
                raise ValueError(f'Customer inspection upload returned {response.status_code}')
            report = response.json()
        evidence = [report['id'],*[d['id'] for d in documents if d.get('type') in ('commercial_invoice','packing_list','pod','cmr')]]
        response = await client.post('/api/claims',json={'shipment_id':shipment['id'],'type':'damage',
            'description':description,'amount_claimed_cents':facts['amount_claimed_cents'],
            'gross_kg_affected':facts['gross_kg_affected'],'packages_affected':facts['packages_affected'],
            'date_of_loss':str(exception['created_at'])[:10],'notice_given_on':now().date().isoformat(),
            'document_ids':evidence,'submit':True})
        if response.status_code != 200:
            raise ValueError(f'Customer claim submission returned {response.status_code}')
        claim = response.json()
        return {'id':claim['id'],'number':claim['number'],'status':claim['status']}


async def tracking(run, provider, event):
    from services.agents.tracking_agent import store_feed_event
    return await store_feed_event(provider, event, verified=True)


def camt054(invoice, key):
    ns = "urn:iso:std:iso:20022:tech:xsd:camt.054.001.02"
    root = E.Element("{" + ns + "}Document", nsmap={None: ns})
    body = E.SubElement(root, "{" + ns + "}BkToCstmrDbtCdtNtfctn")
    def add(parent, path, text=None, **attrs):
        for name in path.split("/"):
            parent = E.SubElement(parent, "{" + ns + "}" + name)
        if text is not None:
            parent.text = str(text)
        parent.attrib.update(attrs)
        return parent
    header = add(body, "GrpHdr")
    add(header, "MsgId", key)
    add(header, "CreDtTm", now_iso())
    notification = add(body, "Ntfctn")
    add(notification, "Id", key)
    add(notification, "CreDtTm", now_iso())
    iban = (invoice.get('seller') or {}).get('iban') or (invoice.get("bank") or {}).get("iban") or config.COMPANY_IBAN
    if iban:
        add(notification, "Acct/Id/IBAN", iban)
    entry = add(notification, "Ntry")
    amount = int(invoice.get("total_cents") or 0) - int(invoice.get("paid_cents") or 0)
    if amount <= 0:
        raise ValueError("Invoice has no positive outstanding amount")
    add(entry, "Amt", f"{amount / 100:.2f}", Ccy=invoice.get("currency") or "EUR")
    add(entry, "CdtDbtInd", "CRDT")
    add(entry, "Sts", "BOOK")
    add(entry, "BookgDt/Dt", now().date().isoformat())
    add(entry, "ValDt/Dt", now().date().isoformat())
    add(entry, "AcctSvcrRef", key)
    tx = add(entry, "NtryDtls/TxDtls")
    add(tx, "Refs/EndToEndId", key)
    ref = invoice.get("rf_reference") or invoice.get("reference")
    if not ref:
        raise ValueError("Issued invoice has no bank reference")
    remittance = add(tx, "RmtInf/Strd/CdtrRefInf")
    add(remittance, "Tp/CdOrPrtry/Cd", "SCOR")
    add(remittance, "Ref", ref)
    return E.tostring(root, xml_declaration=True, encoding="UTF-8")


async def bank(invoice, key):
    from services.reconciliation import store_statement
    return await store_statement(camt054(invoice, key), key + ".xml", "webhook", None)
