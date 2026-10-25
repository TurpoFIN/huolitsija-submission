"""UI-facing integration boundaries: concurrent action and explicit ENS party identities."""
import asyncio
import pytest
from fastapi import HTTPException
from core.jobs import JobDeferred
from routes.customs_flows import checked
from services.customs_flow import screening_payload


def test_running_filing_returns_retryable_bilingual_conflict():
    @checked
    async def operation():
        raise JobDeferred(20, 'filing already held')
    with pytest.raises(HTTPException) as exc:
        asyncio.run(operation())
    assert exc.value.status_code == 409
    assert exc.value.detail['fi'] and exc.value.detail['en']


def test_ens_identifier_only_carrier_uses_explicit_matching_party_name():
    source = {'ens_data': {'carrier': {'identificationNumber': 'NO999999999'}},
              'parties': {'carrier': {'eori': 'NO999999999', 'name': 'Sandbox Carrier'}}}
    out = screening_payload(source)
    assert out['ens_data']['carrier']['name'] == 'Sandbox Carrier'
    assert 'name' not in source['ens_data']['carrier']  # XML schema input stays untouched.
    source['parties']['carrier']['eori'] = 'NO888888888'
    assert 'name' not in screening_payload(source)['ens_data']['carrier']


def test_conflicting_party_names_never_fill_missing_identity():
    source = {'ens_data': {'carrier': {'identificationNumber': 'NO999999999'}},
              'parties': [{'eori': 'NO999999999', 'name': 'One'}, {'eori': 'NO999999999', 'name': 'Two'}]}
    assert 'name' not in screening_payload(source)['ens_data']['carrier']
