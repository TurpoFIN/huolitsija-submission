"""s5 contracts through the real HTTP API and Mongo. No carrier response is faked.
Use S5_API/S5_DB (ci-run.sh supplies them). External procurement is checked separately in the sandbox proof.
"""
import os
from uuid import uuid4

import httpx
import pytest

API = os.environ.get('S5_API', 'http://127.0.0.1:8025/api')
CARGO = {'weight_kg': 800, 'volume_m3': 3, 'packages': 2, 'package_type': 'pallet_eur'}
FCL = {'load_type': 'FCL', 'containers': [{'type': '40HC', 'count': 1}], 'weight_kg': 12000}


def quote(origin, destination, cargo=None, **extra):
    response = httpx.post(f'{API}/public/quote', json={'origin': origin, 'destination': destination,
                         'cargo': cargo or CARGO, **extra}, timeout=120,
                         headers={'X-Forwarded-For': f'10.55.{uuid4().int % 255}.{uuid4().int % 255}'})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('origin,destination,cargo', [
    ('Ouagadougou', 'Kuopio', CARGO), ('Lagos, Nigeria', 'Lima', FCL),
    ('Belo Horizonte', 'Tampere', CARGO), ('Chengdu', 'Hamburg', FCL),
    ('Nairobi', 'Dubai', CARGO), ('Denver', 'Monterrey', CARGO),
    ('Hobart', 'Christchurch', CARGO), ('Ulaanbaatar', 'Almaty', FCL),
    ('Kuopio', 'Cochabamba', {'weight_kg': 12, 'volume_m3': .05, 'packages': 1, 'package_type': 'carton'}),
    ('Winnipeg', 'Oulu', CARGO), ('Tukholma', 'Lontoo', CARGO), ('Shanghai', 'Helsinki', FCL),
])
def test_any_city_returns_priced_options(origin, destination, cargo):
    result = quote(origin, destination, cargo)
    q = result['quote']
    assert q['options'], (origin, destination, q.get('warnings'))
    assert sum(bool(o['recommended']) for o in q['options']) == 1
    for side in ('origin', 'destination'):
        assert q['request'][side]['country']
        assert isinstance(q['request'][side]['lat'], (int, float))
    for option in q['options']:
        assert option['total_cents'] > 0
        assert option['total_cents'] == sum(l['amount_cents'] for l in option['lines'])
        assert option['door_to_door_days'] > 0
        assert option['liability_regime']
        assert option['firmness'] in ('firm', 'partial', 'estimate')
        assert option['bookable'] == (option['firmness'] == 'firm')
        assert all(leg['from'] and leg['to'] and leg['pricing'] for leg in option['legs'])
        if not option['bookable']:
            assert any(line.get('source', {}).get('as_of') for line in option['lines'])
    # Public retrieval must preserve the same provenance and sourcing fields, without buy costs.
    saved = httpx.get(f"{API}/public/quote/{q['id']}", params={'token': result['claim_token']}, timeout=30).json()['quote']
    assert saved['sourcing']['status'] == q['sourcing']['status']
    assert 'needs' not in saved['sourcing']
    assert all('cost_cents' not in o for o in saved['options'])


@pytest.mark.parametrize('mode,regime', [('sea', 'HAGUE_VISBY'), ('rail', 'CIM')])
def test_explicit_mode_and_liability(mode, regime):
    q = quote('Shanghai', 'Helsinki', FCL, mode=mode)['quote']
    assert q['options']
    assert {o['mode'] for o in q['options']} == {mode}
    assert {o['liability_regime'] for o in q['options']} == {regime}


def test_domestic_options_all_use_domestic_terms():
    q = quote('FIHEL', 'FIOUL')['quote']
    assert q['options']
    assert {o['liability_regime'] for o in q['options']} == {'NSAB'}
    assert all(line['vat_code'] == 'FI_25_5' for o in q['options'] for line in o['lines'])


def test_city_autocomplete_and_finnish_exonym():
    for text, country in [('Ouagadougou', 'BF'), ('Tukholma', 'SE')]:
        r = httpx.get(f'{API}/public/places', params={'q': text}, timeout=30)
        assert r.status_code == 200
        assert any(p['country'] == country for p in r.json()['items'])


def test_invalid_coordinates_do_not_get_a_price():
    r = httpx.post(f'{API}/public/quote', json={'origin': {'name': 'Invalid', 'country': 'FI', 'lat': 181, 'lon': 300},
                  'destination': 'Helsinki', 'cargo': CARGO}, timeout=60)
    assert r.status_code in (400, 422), r.text


def test_five_letter_city_is_not_misread_as_a_locode_alias():
    q = quote('Nairobi', 'Dubai')['quote']
    assert q['request']['destination']['name'] == 'Dubai'
    assert q['request']['destination']['country'] == 'AE'
