"""Real s11 writer -> persisted decision -> finance report. Local labelled documents only."""
import io,uuid
from datetime import date
from reportlab.pdfgen import canvas
from test_s13_support import db,make_company,make_shipment,ops


def test_s11_writer_to_finance_for_own_missing_zero_and_customer_imports():
    client=ops();client.headers['X-Include-Test-Tenants']='true'
    old=client.get('/settings').json().get('seller_business_id') or ''
    own='1234567-1'
    assert client.patch('/settings',json={'seller_business_id':own}).status_code==200
    c=make_company(name='TEST VAT integration customer')
    ship=make_shipment(c,delivered_days_ago=1)
    def document(kind):
        buf=io.BytesIO();pdf=canvas.Canvas(buf);pdf.drawString(40,800,'LOCAL VAT CONTRACT TEST - NOT AN AUTHORITY DOCUMENT');pdf.save()
        resp=client.post('/documents',data={'shipment_id':ship['id'],'type':kind},
                         files={'file':('contract-test.pdf',buf.getvalue(),'application/pdf')})
        assert resp.status_code==200,resp.text
        return resp.json()['id']
    decision_doc=document('customs_decision');assessment_doc=document('other')
    outcomes=[]
    try:
        for label,owner,base,deduct in [('own',own,10000,1275),('missing',None,None,None),
                                      ('zero',own,0,0),('customer','7654321-0',10000,1275)]:
            did=str(uuid.uuid4())
            decl={'id':did,'shipment_id':ship['id'],'company_id':c['id'],'type':'import','vat_route':'tilitys',
                  'status':'released','mrn':'26FI123456789012R4','test':True,'items':[{'seq':1}],
                  'importer':{'business_id':own},'submission':{'channel':'manual'},'sandbox':False}
            db.customs_declarations.insert_one(decl)
            body={'mrn':decl['mrn'],'decision_date':date.today().isoformat(),'paid_by':'forwarder',
                  'items':[{'seq':1,'duty_cents':100,'vat_cents':2550}]}
            if owner:
                body.update(document_id=decision_doc,importer_business_id=owner,vat_base_cents=base,deductible_vat_cents=deduct,
                            vat_base_source={'document_id':decision_doc,'basis':'Explicit local test assessment'},
                            deductible_vat_source={'document_id':assessment_doc,'basis':'Separate explicit test deduction'})
            posted=client.post('/customs/'+did+'/decision',json=body);assert posted.status_code==200,posted.text
            stored=db.customs_declarations.find_one({'id':did})['customs_decision']
            assert stored['importer_business_id']==owner and stored['vat_base_cents']==base and stored['deductible_vat_cents']==deduct
            r=client.get('/finance/reports/vat',params={'period':date.today().isoformat()[:7]});assert r.status_code==200,r.text
            rep=r.json();row=next(x for x in rep['import_tilitys'] if x['declaration_id']==did)
            assert row['business_id']==owner and row['vat_base_cents']==base and row['deductible_vat_cents']==deduct
            expected=None if owner is None else owner==own
            assert row['own_return'] is expected
            if owner:
                assert row['vat_base_source']['recorded_at'] and row['deductible_vat_source']['recorded_by']
                assert row['missing_fields']==[]
            else:
                assert 'importer_business_id' in row['missing_fields']
                assert any(v['code']=='import_missing' for v in rep['verify'])
                blocked=client.get('/finance/reports/vat/export',params={'period':date.today().isoformat()[:7],'format':'vero'})
                assert 400<=blocked.status_code<500
            if label=='own':
                for code,amount in [('310',10000),('304',2550),('307',1275)]:
                    assert sum(x['amount_cents'] for x in rep['reconciliation'][code] if x['id']==did)==amount
            if label=='customer':assert not any(x['id']==did for x in rep['reconciliation'].get('304',[]))
            outcomes.append({'case':label,'declaration_id':did,'post_status':posted.status_code,'report':row})
        import json,os
        from pathlib import Path
        target=os.environ.get('VAT_CONTRACT_EVIDENCE')
        if target:Path(target).write_text(json.dumps({'source':'local test documents through actual s11 writer','results':outcomes},indent=2))
    finally:
        client.patch('/settings',json={'seller_business_id':old})
