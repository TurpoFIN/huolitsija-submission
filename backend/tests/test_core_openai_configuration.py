"""Configuration status and real HTTP serialization for the OpenAI-only boundary."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from core import config, llm
from core.email import email_configured
from routes import admin


@pytest.mark.parametrize('key,model,provider,available', [
    ('configured-test-key', 'gpt-6.1-sol', 'openai', True),
    ('', 'gpt-6.1-sol', 'openai', False),
    ('configured-test-key', '', 'openai', False),
    ('configured-test-key', 'gpt-6.1-sol', 'unsupported', False),
])
def test_settings_reports_same_configuration_as_calls(monkeypatch, key, model, provider, available):
    monkeypatch.setattr(config, 'OPENAI_API_KEY', key)
    monkeypatch.setattr(config, 'LLM_MODEL', model)
    monkeypatch.setattr(config, 'LLM_PROVIDER', provider)
    monkeypatch.setattr(admin, 'get_settings', AsyncMock(return_value={'seller_business_id': '', 'seller_vat_id': ''}))
    result = asyncio.run(admin.integrations_status(user={}))['ai']
    assert result == {'available': available, 'model': model, 'provider': 'openai' if available else 'none'}
    assert llm.llm_available() is available


def test_responses_request_uses_configured_model_and_openai_endpoint(monkeypatch):
    monkeypatch.setattr(config, 'OPENAI_API_KEY', 'configured-test-key')
    monkeypatch.setattr(config, 'LLM_MODEL', 'gpt-6.1-sol')
    monkeypatch.setattr(config, 'OPENAI_BASE_URL', 'https://api.openai.com/v1')
    monkeypatch.setattr(config, 'LLM_REASONING_EFFORT', '')
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={'model': config.LLM_MODEL, 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': 'parsed'}]}]})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client_type(transport=httpx.MockTransport(respond), **kw))
    result = asyncio.run(llm._call_openai('system', 'prompt', [('application/pdf', b'%PDF-1.4', 'invoice.pdf')], None, 100))
    assert result == ('parsed', 'openai/gpt-6.1-sol')
    assert str(seen[0].url) == 'https://api.openai.com/v1/responses'
    body = json.loads(seen[0].content)
    assert body['model'] == 'gpt-6.1-sol'
    assert body['input'][0]['content'][1]['type'] == 'input_file'


@pytest.mark.parametrize('provider,token,sender,ready', [
    ('postmark', 'test-token', 'marc+huolitsija@heliosone.fi', True),
    ('postmark', '', 'marc+huolitsija@heliosone.fi', False),
    ('postmark', 'test-token', '', False),
    ('log', 'test-token', 'marc+huolitsija@heliosone.fi', False),
])
def test_email_status_requires_actual_provider_credentials(monkeypatch, provider, token, sender, ready):
    monkeypatch.setattr(config, 'EMAIL_PROVIDER', provider)
    monkeypatch.setattr(config, 'POSTMARK_SERVER_TOKEN', token)
    monkeypatch.setattr(config, 'EMAIL_FROM', sender)
    assert email_configured() is ready
