"""Real HTTP/Mongo/GridFS lifecycle checks; local fixture cargo is not counterparty acceptance."""
import hashlib
import os
import uuid
from pathlib import Path

import pytest
import test_v3_support as S
from test_s8_tradedocs import ops, pdf_text, ship_id, _fresh_shipment

CASES = [
    ('booking_confirmation', 'HUO-2026-000005'), ('commercial_invoice', 'HUO-2026-000005'),
    ('packing_list', 'HUO-2026-000005'), ('house_bl', 'HUO-2026-000001'),
    ('master_bl', 'HUO-2026-000001'), ('hawb', 'HUO-2026-000008'), ('cmr', 'HUO-2026-000003'),
    ('certificate_of_origin', 'HUO-2026-000005'), ('eur1_application', None),
    ('dg_declaration', 'HUO-2026-000009'), ('arrival_notice', 'HUO-2026-000001'),
    ('delivery_order', 'HUO-2026-000005'),
]


def isolated_shipment(ops, reference):
    sid = _fresh_shipment(ops, reference)
    db = S.mongo()
    db.shipments.update_one({'id': sid}, {'$set': {'demo': True, 'customs_declaration_ids': []}})
    source_id = ship_id(reference)
    # Keep extraction/customs provenance with the copied local shipment; no live data is changed.
    for collection in ('documents', 'customs_declarations'):
        for source in db[collection].find({'shipment_id': source_id}, {'_id': 0}):
            source.update(id=str(uuid.uuid4()), shipment_id=sid)
            db[collection].insert_one(source)
    return sid


def eur1_shipment(ops):
    sid = _fresh_shipment(ops)
    # Explicitly fictional local fixture: actual EUR.1 application fields, no grant or preferential qualification claim.
    S.mongo().shipments.update_one({'id': sid}, {'$set': {
        'demo': True, 'direction': 'export',
        'origin': {'name': 'Helsinki', 'country': 'FI', 'unlocode': 'FIHEL'},
        'destination': {'name': 'Oslo', 'country': 'NO', 'unlocode': 'NOOSL'},
        'cargo.origin_country': 'FI', 'cargo.description': 'Finnish wooden furniture',
        'references.invoice_number': 'S8-EUR1-INV-001',
        'legs': [{'seq': 1, 'mode': 'road', 'role': 'main', 'carrier': 'S8 Fixture Transport',
                  'from': {'name': 'Helsinki', 'unlocode': 'FIHEL', 'country': 'FI'},
                  'to': {'name': 'Oslo', 'unlocode': 'NOOSL', 'country': 'NO'}}],
        'parties.shipper': {'name': 'S8 Export Fixture Oy', 'address': {'street': 'Testikatu 1', 'city': 'Helsinki', 'country': 'FI'}},
        'parties.consignee': {'name': 'S8 Import Fixture AS', 'address': {'street': 'Testgata 1', 'city': 'Oslo', 'country': 'NO'}},
    }})
    return sid


@pytest.mark.parametrize('kind,reference', CASES, ids=[c[0] for c in CASES])
def test_each_kind_amends_visible_party_and_preserves_history(ops, kind, reference):
    sid = isolated_shipment(ops, reference) if reference else eur1_shipment(ops)
    response = ops.post(f'/shipments/{sid}/trade-documents', json={'kind': kind, 'final': kind != 'delivery_order'})
    assert response.status_code == 200, response.text
    before = response.json()
    old_pdfs = {lang: ops.get(f"/trade-documents/{before['id']}/pdf?lang={lang}").content for lang in ('fi', 'en')}
    amended_name = 'S8 Consignee ' + uuid.uuid4().hex[:8]
    response = ops.post(f"/trade-documents/{before['doc_no']}/amend", json={
        'changes': {'consignee.name': amended_name}, 'reason': 'Consignee spelling confirmed for lifecycle verification'})
    assert response.status_code == 200, response.text
    after = response.json()
    assert after['version'] == before['version'] + 1 and after['doc_no'] == before['doc_no']
    assert after['status'] == ('draft' if kind in ('master_bl', 'delivery_order') else 'issued')
    history = ops.get(f"/trade-documents/{before['doc_no']}/versions").json()
    old_row = next(d for d in history['versions'] if d['id'] == before['id'])
    assert old_row['status'] == 'superseded' and not old_row['current']
    assert any(a['action'] == 'tradedoc.amended' and a['entity_id'] == after['id'] for a in history['audit'])
    for lang in ('fi', 'en'):
        pdf = ops.get(f"/trade-documents/{after['id']}/pdf?lang={lang}")
        assert pdf.status_code == 200 and amended_name in pdf_text(pdf.content)
        assert hashlib.sha256(pdf.content).hexdigest() == after['sha256'][lang]
        assert ops.get(f"/trade-documents/{before['id']}/pdf?lang={lang}").content == old_pdfs[lang]
        if os.environ.get('S8_EVIDENCE_DIR'):
            output = Path(os.environ['S8_EVIDENCE_DIR']) / 'documents'
            output.mkdir(parents=True, exist_ok=True)
            (output / f'{kind}-amended-{lang}.pdf').write_bytes(pdf.content)
    if kind == 'eur1_application':
        assert 'FI' in pdf_text(pdf.content) and 'NO' in pdf_text(pdf.content)
        assert 'S8-EUR1-INV-001' in pdf_text(pdf.content)
        assert 'PO-77140' not in pdf_text(pdf.content)


def test_promoting_corrected_draft_preserves_correction(ops):
    sid = _fresh_shipment(ops)
    before = ops.post(f'/shipments/{sid}/trade-documents', json={'kind': 'cmr', 'final': False}).json()
    name = 'Corrected consignee on reviewed draft'
    amendment = ops.post(f"/trade-documents/{before['doc_no']}/amend", json={
        'changes': {'consignee.name': name}, 'reason': 'Customer corrected draft consignee'})
    assert amendment.status_code == 200, amendment.text
    issued = ops.post(f'/shipments/{sid}/trade-documents', json={'kind': 'cmr', 'final': True})
    assert issued.status_code == 200, issued.text
    doc = issued.json()
    assert doc['data']['consignee']['name'] == name
    assert doc['version'] == 3 and doc['status'] == 'issued'
    assert name in pdf_text(ops.get(f"/trade-documents/{doc['id']}/pdf?lang=en").content)


def test_issued_certificate_cannot_be_amended_to_empty_goods(ops):
    sid = eur1_shipment(ops)
    doc = ops.post(f'/shipments/{sid}/trade-documents', json={'kind': 'eur1_application'}).json()
    result = ops.post(f"/trade-documents/{doc['doc_no']}/amend", json={
        'changes': {'goods': []}, 'reason': 'Remove all goods'})
    assert result.status_code == 409, result.text


def test_amendment_rechecks_document_applicability(ops):
    sid = eur1_shipment(ops)
    doc = ops.post(f'/shipments/{sid}/trade-documents', json={'kind': 'eur1_application'}).json()
    result = ops.post(f"/trade-documents/{doc['doc_no']}/amend", json={
        'changes': {'destination.country': 'US'}, 'reason': 'Destination changed to USA'})
    assert result.status_code == 409, result.text


def test_eur1_never_invents_eu_goods_origin(ops):
    sid = eur1_shipment(ops)
    S.mongo().shipments.update_one({'id': sid}, {'$set': {'cargo.origin_country': 'CN'}})
    result = ops.post(f'/shipments/{sid}/trade-documents', json={'kind': 'eur1_application', 'final': False})
    assert result.status_code == 200, result.text
    doc = result.json()
    text = pdf_text(ops.get(f"/trade-documents/{doc['id']}/pdf?lang=en").content)
    assert 'CN' in text
    assert 'products of the EU' not in text


def test_incomplete_dg_declaration_cannot_issue(ops):
    sid = _fresh_shipment(ops, 'HUO-2026-000009')
    S.mongo().shipments.update_one({'id': sid}, {'$set': {
        'cargo.dg_items': [{'un': 'UN9999', 'packages': 2, 'gross_kg': 10}], 'cargo.dg': None}})
    result = ops.post(f'/shipments/{sid}/trade-documents', json={'kind': 'dg_declaration'})
    assert result.status_code == 409, result.text


def test_demo_amendments_do_not_enqueue_external_delivery(ops):
    sid = eur1_shipment(ops)
    doc = ops.post(f'/shipments/{sid}/trade-documents', json={'kind': 'booking_confirmation'}).json()
    response = ops.post(f"/trade-documents/{doc['doc_no']}/amend", json={
        'changes': {'consignee.name': 'Manual demo correction'}, 'reason': 'Demonstration remains manual'})
    assert response.status_code == 200, response.text
    assert not S.mongo().jobs.find_one({'kind': 'tradedocs.send', 'payload.doc_id': response.json()['id']})


def test_non_demo_amendments_still_queue_delivery(ops):
    sid = _fresh_shipment(ops)
    doc = ops.post(f'/shipments/{sid}/trade-documents', json={'kind': 'booking_confirmation'}).json()
    response = ops.post(f"/trade-documents/{doc['doc_no']}/amend", json={
        'changes': {'consignee.name': 'Corrected active shipment consignee'}, 'reason': 'Correct party details'})
    assert response.status_code == 200, response.text
    assert S.mongo().jobs.find_one({'kind': 'tradedocs.send', 'payload.doc_id': response.json()['id']})


def test_queued_demo_delivery_is_skipped_without_email(ops):
    from core.db import client
    from services.tradedocs.jobs import send_job
    sid = eur1_shipment(ops)
    doc = ops.post(f'/shipments/{sid}/trade-documents', json={'kind': 'booking_confirmation'}).json()
    before = S.mongo().email_outbox.count_documents({'ref.id': doc['id']})
    result = client.get_io_loop().run_until_complete(send_job({'payload': {'doc_id': doc['id'], 'milestone': 'amended'}}))
    assert 'manual' in result['skipped']
    assert S.mongo().email_outbox.count_documents({'ref.id': doc['id']}) == before


def test_manual_delivery_attaches_current_pdf_after_amendment(ops):
    sid = isolated_shipment(ops, 'HUO-2026-000005')
    doc = ops.post(f'/shipments/{sid}/trade-documents', json={'kind': 'commercial_invoice'}).json()
    response = ops.post(f"/trade-documents/{doc['doc_no']}/amend", json={
        'changes': {'refs.customer': 'Current attachment check'}, 'reason': 'Attach the corrected version'})
    assert response.status_code == 200, response.text
    doc = response.json()
    response = ops.post(f"/trade-documents/{doc['id']}/send")
    assert response.status_code == 200, response.text
    receipts = response.json()['logged']
    assert receipts, 'This local integration test requires EMAIL_PROVIDER=log'
    for receipt in receipts:
        lang = receipt['lang']
        pdf = ops.get(f"/trade-documents/{doc['id']}/pdf?lang={lang}")
        row = S.mongo().email_outbox.find_one({'id': receipt['email_id']})
        assert {'filename': f"{doc['doc_no']}-v{doc['version']}-{lang}.pdf",
                'content_type': 'application/pdf', 'size': len(pdf.content)} in row['attachments']
        assert 'Current attachment check' in pdf_text(pdf.content)
