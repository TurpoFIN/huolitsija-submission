"""Authenticated presentation facts are tenant-scoped and cannot file a declaration."""
import asyncio
import os
import uuid

os.environ['DB_NAME'] = 'huo_instructions_' + uuid.uuid4().hex[:10]
os.environ['JWT_SECRET'] = 'local-instructions-test-secret-sufficiently-long'

import httpx
from fastapi import FastAPI
from core.db import db, client
from core.security import create_session_token
from routes.customs import router


def test_actual_customer_route_and_tenant_boundary():
    app = FastAPI()
    app.include_router(router)

    async def exercise():
        await db.raw.users.insert_many([
            {'id': 'shipper', 'org_type': 'customer', 'role': 'customer_admin', 'company_id': 'own'},
            {'id': 'carrier', 'org_type': 'partner', 'role': 'partner', 'company_id': 'own'}])
        await db.raw.shipments.insert_many([
            {'id': 'own-shipment', 'company_id': 'own', 'status': 'booked', 'direction': 'export'},
            {'id': 'other-shipment', 'company_id': 'other', 'status': 'booked', 'direction': 'export'},
            {'id': 'closed-shipment', 'company_id': 'own', 'status': 'delivered', 'direction': 'export'}])
        body = {'goods_location': {'type': 'D', 'address': {'street': 'Naavapuronkuja 8', 'postal_code': '33720', 'city': 'Tampere', 'country': 'FI'}},
                'transaction_nature': '11'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://instructions') as h:
            url = '/api/customs/shipments/own-shipment/instructions'
            assert (await h.patch(url, json=body)).status_code == 401
            h.headers['Authorization'] = 'Bearer ' + create_session_token('shipper')
            assert (await h.patch(url.replace('own-shipment','other-shipment'), json=body)).status_code == 404
            assert (await h.patch(url.replace('own-shipment','closed-shipment'), json=body)).status_code == 409
            assert (await h.patch(url, json={**body, 'transaction_nature': 'invented'})).status_code == 422
            assert (await h.patch(url, json={**body, 'goods_location': {'type': 'D','address': {'country':'FI'}}})).status_code == 422
            statement = {'seq':1,'value_eur':2060,'basis':'Goods EUR2000 plus EUR60 domestic carriage; international flight excluded.'}
            assert (await h.patch(url, json={**body, 'export_statistical_values':[{**statement,'value_eur':-1}]})).status_code == 422
            assert (await h.patch(url, json={**body, 'export_statistical_values':[statement,statement]})).status_code == 422
            response = await h.patch(url, json={**body, 'export_statistical_values':[statement]})
            assert response.status_code == 200, response.text
            assert response.json()['goods_location']['source'] == 'customer_statement'
            h.headers['Authorization'] = 'Bearer ' + create_session_token('carrier')
            assert (await h.patch(url, json=body)).status_code == 403
        shipment = await db.raw.shipments.find_one({'id': 'own-shipment'})
        assert shipment['transaction_nature'] == '11' and shipment['status'] == 'booked'
        assert shipment['export_statistical_values'][0]['value_eur'] == 2060
        assert shipment['export_statistical_values'][0]['source'] == 'customer_statement'
        assert await db.raw.customs_declarations.count_documents({}) == 0
        assert await db.raw.export_declarations.count_documents({}) == 0
        assert await db.raw.audit_log.find_one({'action': 'shipment_customs_instructions'})
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(exercise())
    finally:
        loop.run_until_complete(client.drop_database(db.raw.name))
