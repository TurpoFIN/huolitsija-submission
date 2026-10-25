"""Tax working papers and balanced vendor exports via HTTP, with explicit test source records."""
import csv,io,uuid,zipfile
from datetime import date,timedelta
from decimal import Decimal
from test_s13_support import db,make_company,make_shipment,ops,iso,now

def test_reverse_charge_customer_import_and_balanced_exports():
    period=date.today().isoformat()[:7]
    c=make_company(country='DE',vat_id='DE123456789',name='Finance EU customer test')
    s=make_shipment(c,delivered_days_ago=1,origin=('DEHAM','Hamburg','DE',53.5,10),destination=('FIHEL','Helsinki','FI',60,25),mode='road')
    # a real (non-test) customer: company-wide ledger builds and exports book only real records (s2: test tenants and
    # test-flagged records never reach company-wide figures or outside exports), so this fixture drops the test flags
    db.companies.update_one({'id':c['id']},{'$unset':{'test':''}})
    for coll in ('shipments','quotes','cost_accruals'):
        db[coll].update_many({'$or':[{'id':s['id']},{'shipment_id':s['id']}]},{'$unset':{'test':''}})
    inv=ops().post('/invoices/from-shipment/'+s['id'])
    assert inv.status_code==200,inv.text
    invoice=inv.json()
    assert invoice['status'] in ('open','overdue'),invoice.get('warnings')
    # A purchase of a domestic Finnish leg from an EU supplier: taxable reverse charge.
    cid=str(uuid.uuid4())
    db.carrier_invoices.insert_one({'id':cid,'company_id':c['id'],'number':'TEST-RC-'+cid[:8],'invoice_date':date.today().isoformat(),
        'supplier':{'name':'EU purchase test','country':'SE','vat_id':'SE123456789001'},'currency':'EUR',
        'net_cents':100000,'vat_cents':0,'total_cents':100000,'eur':{'net_cents':100000,'vat_cents':0,'total_cents':100000},
        'lines':[{'description':'Finnish local haulage','amount_cents':100000,'vat_cents':0,'vat_rate_pct':0}],
        'vat_treatment':'eu_reverse_charge','status':'approved','matches':[],'created_at':iso(now())})
    did=str(uuid.uuid4())
    db.customs_declarations.insert_one({'id':did,'company_id':c['id'],'vat_route':'tilitys','status':'released',
        'importer':{'name':c['name'],'business_id':c['business_id']},
        'customs_decision':{'decision_date':date.today().isoformat(),'mrn':'TEST-MRN-'+did[:8],
                            'vat_cents':255000,'source':'tulli_decision'}})
    rep=ops().get('/finance/reports/vat',params={'period':period,'company_id':c['id']})
    assert rep.status_code==200,rep.text
    rep=rep.json()
    source=lambda code:[r for r in rep['reconciliation'].get(code,[]) if r['id']==cid]
    assert sum(r['amount_cents'] for r in source('314'))==100000
    assert sum(r['amount_cents'] for r in source('306'))==25500
    assert sum(r['amount_cents'] for r in source('307'))==25500
    imp=next(r for r in rep['import_tilitys'] if r['declaration_id']==did)
    assert not imp['own_return'] and imp['vat_cents']==255000
    assert imp['vat_base_cents'] is None and 'vat_base_cents' in imp['missing_fields']
    assert not any(r['id']==did for r in rep['reconciliation'].get('304',[]))
    ec=ops().get('/finance/reports/ec-sales',params={'period':period,'company_id':c['id']}).json()
    assert any(x['vat_id']==c['vat_id'] and x['services_cents']>0 for x in ec['items']),ec
    bounds={'from':(date.today()-timedelta(days=60)).isoformat(),'to':date.today().isoformat()}
    result=ops().post('/finance/ledger/build',json=bounds)
    assert result.status_code==200,result.text
    assert db.ledger_entries.count_documents({'source_id':cid})>0
    listing=ops().get('/finance/ledger',params=bounds).json()
    assert listing['total']>0 and listing['unbalanced']==[]
    assert listing['trial_balance']['debit_cents']==listing['trial_balance']['credit_cents']
    for fmt in ['procountor','netvisor','csv']:
        response=ops().post('/finance/ledger/export',json={**bounds,'format':fmt})
        assert response.status_code==200,response.text
        row=response.json();assert row['status']=='ready' and row['count']>0,row
        assert row['debit_cents']==row['credit_cents']
        content=ops().get('/finance/ledger/exports/'+row['id']+'/file')
        assert content.status_code==200 and len(content.content)>20
        if fmt=='procountor':
            files=[content.content]
            if content.content[:2]==b'PK':
                with zipfile.ZipFile(io.BytesIO(content.content)) as z:files=[z.read(n) for n in z.namelist()]
            for data in files:
                rows=list(csv.reader(io.StringIO(data.decode('cp1252')),delimiter=';'))
                assert all(len(r)==7 for r in rows)
                assert sum(Decimal(r[2].replace(',','.')) for r in rows)==0


def test_import_owner_and_amount_provenance_are_required_without_inference():
    period='2032-02'
    settings=ops().get('/settings').json()
    original_bid=settings.get('seller_business_id') or ''
    own='1234567-1'
    changed=ops().patch('/settings',json={'seller_business_id':own});assert changed.status_code==200,changed.text
    did=str(uuid.uuid4())
    source={'document_id':'TEST-ASSESSMENT','basis':'Explicit reviewer-entered authority assessment',
            'recorded_by':'test-reviewer','recorded_at':iso(now())}
    decision={'decision_date':'2032-02-15','mrn':'TEST-VAT-'+did[:8], 'vat_cents':25500,
              'currency':'EUR','document_id':'TEST-DECISION','vat_base_cents':100000,'deductible_vat_cents':25500,
              'vat_base_source':source,'deductible_vat_source':source}
    # no 'test' flag: company-wide figures never include test records (s2, core.tenant_visibility.exclude_test)
    db.customs_declarations.insert_one({'id':did,'vat_route':'tilitys',
        'importer':{'business_id':own},'customs_decision':decision})
    def report():
        r=ops().get('/finance/reports/vat',params={'period':period});assert r.status_code==200,r.text
        data=r.json();return data,next(i for i in data['import_tilitys'] if i['declaration_id']==did)
    try:
        rep,imp=report()
        assert imp['own_return'] is None and imp['business_id'] is None
        assert imp['vat_base_cents'] is None and 'importer_business_id' in imp['missing_fields']
        assert any(v['code']=='import_missing' for v in rep['verify'])
        blocked=ops().get('/finance/reports/vat/export',params={'period':period,'format':'vero'})
        assert 400 <= blocked.status_code < 500,blocked.text
        db.customs_declarations.update_one({'id':did},{'$set':{'customs_decision.importer_business_id':own}})
        rep,imp=report()
        assert imp['own_return'] is True and imp['missing_fields']==[]
        assert imp['vat_base_cents']==100000 and imp['deductible_vat_cents']==25500
        for code,expected in [('310',100000),('304',25500),('307',25500)]:
            assert sum(x['amount_cents'] for x in rep['reconciliation'][code] if x['id']==did)==expected
        db.customs_declarations.update_one({'id':did},{'$set':{'customs_decision.deductible_vat_cents':0},
                                                                  '$unset':{'customs_decision.deductible_vat_source':''}})
        rep,imp=report();assert imp['deductible_vat_cents'] is None
        db.customs_declarations.update_one({'id':did},{'$set':{'customs_decision.deductible_vat_source':source}})
        rep,imp=report();assert imp['deductible_vat_cents']==0 and imp['missing_fields']==[]
        other='7654321-0' if own!='7654321-0' else '1234567-1'
        db.customs_declarations.update_one({'id':did},{'$set':{'customs_decision.importer_business_id':other}})
        rep,imp=report();assert imp['own_return'] is False
        assert not any(x['id']==did for x in rep['reconciliation'].get('304',[]))
    finally:
        db.customs_declarations.delete_one({'id':did})
        ops().patch('/settings',json={'seller_business_id':original_bid})
