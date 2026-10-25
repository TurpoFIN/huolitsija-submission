"""Actual HTTP/auth/Mongo/GridFS boundaries, with isolated labelled demo records only.
Run with MONGO_URL pointing at a local Mongo; the HTTP scenario uses an isolated database.
"""
import asyncio
import io
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI
from pypdf import PdfReader
import pytest

from core import config, security
from core.db import client, db, ensure_indexes
from core.storage import put_file
from routes import documents, trade_docs
from services.tradedocs import dispatch


def test_release_http_contract():
    assert 'localhost' in config.MONGO_URL or '127.0.0.1' in config.MONGO_URL, 'Local test DB only'
    result = subprocess.run([sys.executable, __file__, '--scenario'], timeout=60, capture_output=True, text=True,
                            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]) + os.pathsep + os.environ.get('PYTHONPATH', ''),
                                 'DB_NAME': 'document_release_test_' + uuid.uuid4().hex,
                                 'EMAIL_PROVIDER': 'log', 'JOBS_WORKER_ENABLED': 'false', 'TRADEDOCS_AUTO': 'false'})
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())


async def scenario():
    await ensure_indexes()
    await security.load_secret(db)
    app = FastAPI()
    app.include_router(trade_docs.router)
    app.include_router(documents.router)
    suffix = uuid.uuid4().hex
    sid, cid, uid = ['demo-' + x + suffix for x in ('ship-', 'company-', 'ops-')]
    await db.raw.users.insert_one({'id': uid, 'email': uid + '@example.invalid', 'role': 'ops_admin', 'org_type': 'internal', 'name': 'Demo ops'})
    await db.raw.companies.insert_one({'id': cid, 'business_id': 'DEMO-'+suffix, 'name': 'DEMO company no cargo', 'country': 'FI'})
    await db.raw.shipments.insert_one({'id': sid, 'company_id': cid, 'reference': 'DEMO-NO-CARGO', 'demo': True,
        'mode': 'road', 'direction': 'import', 'status': 'arrived', 'customs_clearance': False,
        'origin': {'name': 'Demo origin', 'country': 'SE'}, 'destination': {'name': 'Demo terminal', 'country': 'FI'},
        'cargo': {'description': 'DEMO goods no cargo', 'packages': 1, 'weight_kg': 10},
        'parties': {'shipper': {'name': 'Demo shipper'}, 'consignee': {'name': 'Demo consignee'}},
        'price': {}, 'eta': datetime.now(timezone.utc).isoformat()})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                headers={'Authorization': 'Bearer ' + security.create_session_token(uid)}) as c:
        async def issue(final=True):
            return await c.post(f'/api/shipments/{sid}/trade-documents', json={'kind': 'delivery_order', 'final': final})
        rejected = await issue()
        assert rejected.status_code == 409 and 'release evidence' in rejected.text, rejected.text
        draft = (await issue(False)).json()
        assert draft['status'] == 'draft'
        # Actual existing upload route, no fabricated real carrier document: visibly labelled test artifact.
        from test_document_arrival_amount import context
        from services.tradedocs import pdf
        source = pdf.render('arrival_notice', context(), lang='en', meta={'doc_no':'DEMO-SOURCE','version':1,'status':'draft'})
        uploaded = await c.post('/api/documents', data={'shipment_id': sid, 'type': 'other'},
                                files={'file': ('DEMO-NO-CARGO-release.pdf', source, 'application/pdf')})
        assert uploaded.status_code == 200, uploaded.text
        docid = uploaded.json()['id']
        body = {'document_id': docid, 'reference': 'DEMO-RELEASE-001', 'issuer': 'Demo terminal reviewer fixture',
                'terminal': 'Demo terminal', 'haulier': 'Demo haulier', 'transport_document': 'DEMO-CMR-001',
                'valid_until': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                'reason': 'DEMO test only; no cargo and no actual carrier authorization'}
        for key, value in [('document_id','absent'), ('valid_until','2020-01-01T00:00:00Z')]:
            response = await c.post(f'/api/shipments/{sid}/delivery-release', json={**body, key:value})
            assert response.status_code == 409, response.text
        naive = await c.post(f'/api/shipments/{sid}/delivery-release', json={**body,'valid_until':'2030-01-01T00:00:00'})
        assert naive.status_code == 422
        for org, role in [('customer','customer_admin'), ('partner','partner')]:
            user_id = org + suffix
            await db.raw.users.insert_one({'id':user_id, 'email':user_id+'@example.invalid', 'role':role,'org_type':org,'company_id':cid})
            response = await c.post(f'/api/shipments/{sid}/delivery-release',json=body,
                                   headers={'Authorization':'Bearer '+security.create_session_token(user_id)})
            assert response.status_code == 403
        saved = await c.post(f'/api/shipments/{sid}/delivery-release', json=body)
        assert saved.status_code == 200, saved.text
        release = saved.json()
        assert release['source'] == 'manual' and release['demo'] and not release['issuer_verified_by_provider']
        assert release['recorded_by']['id'] == uid
        issued = await issue()
        assert issued.status_code == 200, issued.text
        order = issued.json()
        assert order['status'] == 'issued' and order['version'] == 2
        for lang in ['fi','en']:
            response = await c.get(f"/api/trade-documents/{order['id']}/pdf?lang={lang}")
            text = ' '.join(p.extract_text() for p in PdfReader(io.BytesIO(response.content)).pages)
            assert 'DEMO-RELEASE-001' in text and 'Demo haulier' in text and 'DEMO-CMR-001' in text
            assert ('Käsin tarkastettu' if lang == 'fi' else 'Manually reviewed') in text
            if os.getenv('DOCUMENT_EVIDENCE_DIR'):
                Path(os.environ['DOCUMENT_EVIDENCE_DIR'], f'delivery-order-demo-{lang}.pdf').write_bytes(response.content)
        amend_url = f"/api/trade-documents/{order['doc_no']}/amend"
        forged = await c.post(amend_url,json={'changes':{'delivery_release.reference':'invented'},'reason':'forged source'})
        assert forged.status_code == 400
        # An uploaded source moved to a different shipment cannot authorize existing orders.
        await db.raw.documents.update_one({'id':docid},{'$set':{'shipment_id':'other-shipment'}})
        assert (await issue()).status_code == 409
        for gated in [True, False]:
            with pytest.raises(Exception) as exc:
                await dispatch.send(order['id'], gated=gated)
            assert getattr(exc.value,'status_code',None) == 409
        await db.raw.documents.update_one({'id':docid},{'$set':{'shipment_id':sid}})
        source_row = await db.raw.documents.find_one({'id':docid})
        await db.raw.documents.delete_one({'id':docid})
        assert (await issue()).status_code == 409
        for gated in [True, False]:
            with pytest.raises(Exception) as exc:
                await dispatch.send(order['id'], gated=gated)
            assert getattr(exc.value,'status_code',None) == 409
        await db.raw.documents.insert_one(source_row)
        # The raw source file is re-read, not trusted solely because metadata says unchanged.
        badfile = await put_file(b'changed source', 'changed.txt','text/plain')
        original = await db.documents.find_one({'id':docid})
        await db.raw.documents.update_one({'id':docid},{'$set':{'file_id':badfile}})
        assert (await issue()).status_code == 409
        await db.raw.documents.update_one({'id':docid},{'$set':{'file_id':original['file_id']}})
        # Holds and customs status must not be bypassed by a recorded manual release.
        await db.raw.shipments.update_one({'id':sid},{'$set':{'holds':[{'type':'compliance'}]}})
        assert (await issue()).status_code == 409
        await db.raw.shipments.update_one({'id':sid},{'$set':{'holds':[], 'customs_clearance':True}})
        assert (await issue()).status_code == 409
        await db.raw.shipments.update_one({'id':sid},{'$set':{'customs_clearance':False}})
        # Expired but otherwise identical current/snapshot evidence must also block queued/manual sends.
        expired = {**release, 'valid_until':'2020-01-01T00:00:00Z'}
        await db.raw.shipments.update_one({'id':sid},{'$set':{'delivery_release':expired}})
        await db.raw.trade_documents.update_one({'id':order['id']},{'$set':{'data.delivery_release':expired}})
        for gated in [True, False]:
            with pytest.raises(Exception) as exc:
                await dispatch.send(order['id'], gated=gated)
            assert getattr(exc.value,'status_code',None) == 409 and 'expired' in str(exc.value)
        # Replace/expire evidence: amendment and old dispatch must reject it.
        await db.raw.shipments.update_one({'id':sid},{'$set':{'delivery_release.valid_until':'2020-01-01T00:00:00Z'}})
        response = await c.post(amend_url,json={'changes':{'refs.customer':'Demo corrected'},'reason':'demo correction'})
        assert response.status_code == 409
        assert (await c.post(f"/api/trade-documents/{order['id']}/send")).status_code == 409
        # New current evidence can explicitly refresh the order; old PDF bytes stay stored.
        saved = await c.post(f'/api/shipments/{sid}/delivery-release',json={**body,'reference':'DEMO-RELEASE-002'})
        assert saved.status_code == 200
        response = await c.post(f"/api/trade-documents/{order['doc_no']}/refresh")
        assert response.status_code == 200, response.text
        assert response.json()['version'] == 3
        print('HTTP/auth/Mongo/GridFS: missing evidence 409; draft available; demo upload/manual source; final v2; FI/EN PDF source references; customer/partner 403; expiry/wrong-shipment/changed-file/holds/customs/amend/send guards; refresh v3.')


@pytest.mark.parametrize('source,expected', [('schedule', False), ('simulator', False), ('', False), ('carrier_api', True), ('partner', True), ('manual', True)])
def test_arrival_requires_observed_milestone(source, expected):
    from services.tradedocs.jobs import arrival_evidence
    stamp = datetime.now(timezone.utc).isoformat()
    shipment = {'mode':'sea','status':'delivered','legs':[{'mode':'sea','seq':1}],
                'milestones':[{'code':'arrived','at':stamp,'source':source,'leg_seq':1}]}
    assert arrival_evidence(shipment) is expected
    shipment['milestones'][0]['leg_seq'] = 0
    assert not arrival_evidence(shipment)

    shipment['milestones'][0].update(leg_seq=1, at='2099-01-01T00:00:00Z')
    assert not arrival_evidence(shipment)


if __name__ == '__main__':
    assert config.DB_NAME.startswith('document_release_test')
    client.get_io_loop().run_until_complete(scenario())
