"""Public identity boundary: incomplete and populated; never expose secrets."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncio
import httpx
from fastapi import FastAPI
from routes import legal


def request():
    async def go():
        app = FastAPI()
        app.include_router(legal.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            return await c.get("/api/legal/config")
    return asyncio.run(go())


def test_public_identity_is_allowlisted_and_preserves_missing(monkeypatch):
    async def settings():
        return {"seller_business_id": "", "seller_vat_id": "", "seller_iban": "private-bank", "owner_alert_email": "private-owner"}
    monkeypatch.setattr(legal, "get_settings", settings)
    monkeypatch.setattr(legal.config, "OPENAI_API_KEY", "must-not-leak")
    monkeypatch.delenv("COMPANY_ADDRESS", raising=False)
    monkeypatch.delenv("COMPANY_REGISTERED", raising=False)
    r = request()
    assert r.status_code == 200
    assert r.headers['cache-control'] == 'no-store'
    c = r.json()['company']
    assert c['business_id'] is None and c['address'] is None and c['registered'] is False
    assert set(c) == {'name', 'registered', 'business_id', 'vat_id', 'eori', 'address', 'email', 'privacy_email', 'phone', 'registered_office'}
    assert all(secret not in r.text for secret in ['private-bank', 'private-owner', 'must-not-leak'])


def test_registered_identity_uses_document_settings_and_environment(monkeypatch):
    async def settings():
        return {"seller_business_id": "1234567-8", "seller_vat_id": "FI12345678"}
    monkeypatch.setattr(legal, "get_settings", settings)
    monkeypatch.setenv('COMPANY_ADDRESS', 'Testikatu 10, 00100 Helsinki, Finland')
    monkeypatch.setenv('COMPANY_REGISTERED_OFFICE', 'Espoo')
    monkeypatch.setenv('COMPANY_REGISTERED', 'true')
    monkeypatch.setenv('COMPANY_PRIVACY_EMAIL', 'privacy@example.test')
    c=request().json()['company']
    assert c['registered'] is True
    assert c['business_id']=='1234567-8'
    assert c['address']=='Testikatu 10, 00100 Helsinki, Finland'
    assert c['registered_office']=='Espoo'
    assert c['privacy_email']=='privacy@example.test'
