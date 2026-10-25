"""PDF regression for LRN-2026-000055's missing LED bicycle-light duty.

Only goods fields from the reported shape are used; no customer or authority
identity is fabricated. Tests exercise the actual PDF renderer and extracted text.
"""
import copy
import io
import asyncio
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfReader

from services.tulli import render_summary_pdf


def declaration(item):
    return {'lrn': 'PDF REGRESSION FIXTURE', 'items': [dict({
        'seq': 3, 'description': 'LED bicycle front light, USB rechargeable, 800 lm',
        'cn_code': '85121000', 'cn_confirmed': True, 'origin_country': 'CN',
        'customs_value_cents': 10000, 'duty_rate_pct': None, 'duty_cents': None,
        'vat_rate_pct': 25.5, 'vat_cents': 2550}, **item)],
        'totals': {'customs_value_cents': 10000, 'duty_cents': 0, 'vat_cents': 2550}}


def text(decl):
    before = copy.deepcopy(decl)
    pdf = render_summary_pdf(decl)
    assert decl == before, 'Rendering must never mutate a released declaration'
    return '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


def test_missing_legacy_duty_has_reason_and_incomplete_totals():
    result = text(declaration({}))
    assert 'Not calculated' in result
    assert 'duty missing' in result
    assert 'No TARIC lookup result is recorded.' in result
    assert 'Missing duty is not zero.' in result
    assert result.count('Incomplete') == 2
    assert '25.50 EUR' not in result  # legacy VAT silently excluded unknown duty
    assert '0.00 EUR' not in result.replace('100.00 EUR', '')


def test_failed_taric_lookup_explains_actual_reason():
    result = text(declaration({'taric': {'status': 'unavailable', 'error': 'HTTP 503 from EU TARIC'}}))
    assert 'TARIC lookup failed: HTTP 503 from EU TARIC' in result
    assert 'No TARIC lookup result' not in result


def test_missing_taric_subcode_explains_required_input():
    result = text(declaration({'taric': {'status': 'not_declarable', 'reasons': [
        {'en': 'Choose a declarable TARIC subcode.'}]}}))
    assert 'Choose a declarable TARIC subcode.' in result
    assert 'lookup failed' not in result


@pytest.mark.parametrize('rate,amount', [(0, 0), (2.7, 270), (None, 3200)])
def test_real_zero_percentage_and_specific_duties_remain_values(rate, amount):
    d = declaration({'duty_rate_pct': rate, 'duty_cents': amount, 'duty_source': 'taric',
                     'taric': {'status': 'ok', 'duty_cents': amount}})
    d['totals'].update(duty_cents=amount, duty_complete=True)
    result = text(d)
    assert f'{amount / 100:.2f} EUR' in result
    assert 'Not calculated' not in result
    assert 'Incomplete' not in result
    assert '25.50 EUR' in result


@pytest.mark.parametrize('missing', [True, False])
def test_missing_duty_survives_recompute_json_and_pdf(monkeypatch, missing):
    import json
    from services import customs, customs_origin, customs_licences, taric
    monkeypatch.setattr(customs_origin, 'apply_to_declaration', AsyncMock())
    monkeypatch.setattr(customs_licences, 'attribute', AsyncMock())
    monkeypatch.setattr(customs, 'validate', AsyncMock(return_value=[]))
    # Boundary double models the real provider's unavailable/zero-duty outcomes;
    # apply_to_item, arithmetic, serialization and PDF rendering remain real.
    if missing:
        provider = AsyncMock(side_effect=taric.TaricUnavailable('HTTP 503 from EU TARIC'))
    else:
        provider = AsyncMock(return_value={'status': 'ok', 'rate_pct': 0, 'duty_cents': 0})
    monkeypatch.setattr(taric, 'duty_for_item', provider)
    d = declaration({'invoice_amount': 100, 'hs_code': '851210', 'gross_kg': 1, 'net_kg': 1})
    d['id'] = 'pdf-regression-fixture'
    d['valuation'] = {'exchange_rate': {'rate': 1}, 'invoice_total': 100, 'freight_known': True}
    asyncio.run(customs.recompute(d, company={'vat_registered': True}, shipment={'direction': 'import'},
                                 settings={'test': True}, on='2026-10-24'))
    roundtrip = json.loads(json.dumps(d))
    item, totals = roundtrip['items'][0], roundtrip['totals']
    summary = customs.summarize(roundtrip)
    assert summary['duty_complete'] is (not missing)
    assert summary['vat_complete'] is (not missing)
    if missing:
        assert item['duty_cents'] is None
        assert item['vat_base_cents'] is None and item['vat_cents'] is None
        assert totals['duty_cents'] is None and totals['vat_cents'] is None
        assert totals['vat_base_cents'] is None and totals['payable_to_customs_cents'] is None
        assert not totals['duty_complete'] and not totals['vat_complete']
        assert any(not rule['ok'] for rule in taric.rules(roundtrip))
        assert 'TARIC lookup failed: TaricUnavailable: HTTP 503' in text(roundtrip)
    else:
        assert item['duty_cents'] == totals['duty_cents'] == 0
        assert item['vat_base_cents'] == totals['vat_base_cents'] == 10000
        assert item['vat_cents'] == totals['vat_cents'] == 2550
        assert totals['duty_complete'] and totals['vat_complete']
        assert 'Not calculated' not in text(roundtrip)
