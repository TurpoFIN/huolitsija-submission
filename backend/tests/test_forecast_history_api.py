"""Real local HTTP/Mongo history checks, using disposable explicitly labelled test rows."""
from datetime import datetime, timedelta, timezone
import uuid
import pytest
from test_s14_insights import db, ops, company


@pytest.fixture
def history_company():
    c = company('Demo forecast boundary check')
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    docs = []
    for i in range(8):
        d = monday - timedelta(weeks=8-i)
        docs.append({'id': str(uuid.uuid4()), 'reference': 'DEMO-FC-' + uuid.uuid4().hex, 'company_id': c['id'], 'test': True, 'demo': True,
                     'status': 'booked', 'created_at': d.isoformat() + 'T12:00:00Z',
                     'price': {'total_cents': 0 if i == 2 else 10000}})
    try:
        db.shipments.insert_many(docs)
        yield c, docs
    finally:
        db.shipments.delete_many({'company_id': c['id']})
        db.companies.delete_one({'id': c['id']})


def read(cid):
    r = ops().get('/insights/forecast', params={'company_id': cid, 'horizon': 4})
    assert r.status_code == 200
    return r.json()


def test_api_preserves_zero_and_discloses_demo_history(history_company):
    c, _ = history_company
    f = read(c['id'])
    assert f['history']['sell_cents'][2] == 0
    assert f['data_quality'] == {'records': 8, 'demo_test_records': 8, 'missing_price_records': 0,
                                 'operating_history_validated': False}
    assert f['volume']['status'] == f['revenue']['status'] == 'ok'
    assert f['volume']['validation'] == 'limited_history'


def test_api_missing_price_blocks_sales_forecast_without_hiding_volume(history_company):
    c, docs = history_company
    db.shipments.update_one({'id': docs[3]['id']}, {'$unset': {'price.total_cents': ''}})
    f = read(c['id'])
    assert f['history']['sell_cents'][3] is None
    assert f['data_quality']['missing_price_records'] == 1
    assert f['revenue']['status'] == 'incomplete_history'
    assert f['revenue']['forecast'] == []
    assert f['volume']['status'] == 'ok'


def test_history_counts_only_completed_periods(history_company):
    c, _ = history_company
    db.shipments.insert_one({'id': str(uuid.uuid4()), 'reference': 'DEMO-FC-' + uuid.uuid4().hex, 'company_id': c['id'], 'test': True,
        'status': 'booked', 'created_at': datetime.now(timezone.utc).isoformat(), 'price': {}})
    f = read(c['id'])
    assert f['data_quality']['records'] == 8
    assert f['data_quality']['missing_price_records'] == 0
