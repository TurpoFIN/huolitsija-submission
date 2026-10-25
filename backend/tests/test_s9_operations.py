"""Real HTTP/Mongo/SMTP execution checks. No external completion is simulated by application code.

Test counterparts explicitly act as haulier/warehouse and supply receipt references.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

import httpx
import pytest
from test_s9_support import API, ops, partner, customer, db, create_test_shipment, partner_carrier_id, set_autopilot, work
from test_s9_si_vgm import imap_fetch

created = []
def post(path, data=None, client=None, status=200):
    r=(client or ops()).post('/execution'+path,json=data or {})
    assert r.status_code==status,r.text
    return r.json()
def ship():
    s=create_test_shipment(cargo={'description':'TEST S9 terminal cartons','packages':4,'package_type':'cartons','weight_kg':1800,'volume_m3':8})
    created.append(s['id'])
    # Actual fixture assignment to the demo haulier; partner authorization checks this stored contract.
    db.shipments.update_one({'id':s['id']},{'$set':{'legs.0.carrier_id':partner_carrier_id()}})
    return s

def win(offset=24):
    a=datetime.now(timezone.utc)+timedelta(hours=offset)
    return {'start':a.isoformat(),'end':(a+timedelta(hours=4)).isoformat()}

@pytest.fixture(scope='module',autouse=True)
def cleanup():
    set_autopilot(False)
    yield
    set_autopilot(False)
    if not os.getenv('S9_KEEP_EVIDENCE'):
        db.shipments.delete_many({'id':{'$in':created}})
        for coll in ('slot_bookings','warehouse_orders','crossdock_tasks'):
            db[coll].delete_many({'shipment_id':{'$in':created}})

def test_slots_partner_scope_confirmation_and_replay():
    s=ship(); w=win()
    row=post(f"/shipments/{s['id']}/slots",{'leg_seq':s['legs'][0]['seq'],'kind':'pickup','window':w})
    assert row['status']=='requested'
    again=post(f"/shipments/{s['id']}/slots",{'leg_seq':s['legs'][0]['seq'],'kind':'pickup','window':w})
    assert again['id']==row['id']
    post(f"/partner/slots/{row['id']}/reply",{'action':'confirm'},customer(),403)
    rows=partner().get('/execution/partner/slots').json()['items']; assert row['id'] in [r['id'] for r in rows]
    result=post(f"/partner/slots/{row['id']}/reply",{'action':'confirm'},partner())
    assert result['status']=='confirmed'
    post(f"/partner/slots/{row['id']}/reply",{'action':'reject'},partner(),409)
    stranger=ship(); db.shipments.update_one({'id':stranger['id']},{'$set':{'legs.0.carrier_id':'other-haulier'}})
    other=post(f"/shipments/{stranger['id']}/slots",{'leg_seq':stranger['legs'][0]['seq'],'kind':'pickup','window':w})
    post(f"/partner/slots/{other['id']}/reply",{'action':'confirm'},partner(),404)

def test_proposed_window_auto_accept_and_kill_switch():
    s=ship();w=win(48)
    row=post(f"/shipments/{s['id']}/slots",{'leg_seq':s['legs'][0]['seq'],'kind':'delivery','window':w})
    proposed={**w,'start':(datetime.fromisoformat(w['start'])+timedelta(hours=1)).isoformat()}
    post(f"/partner/slots/{row['id']}/reply",{'action':'propose','window':proposed},partner())
    set_autopilot(True,True); assert work(['execution.slot_accept'])['ran']==0
    assert db.slot_bookings.find_one({'id':row['id']})['status']=='proposed'
    set_autopilot(True);work(['execution.slot_accept'])
    assert db.slot_bookings.find_one({'id':row['id']})['status']=='confirmed'
    assert db.agent_decisions.find_one({'action':'slot_accept','subject_id':row['id'],'outcome':'auto'})
    set_autopilot(False)

def test_consolidation_capacity_exclusivity_and_physical_evidence():
    a,b=ship(),ship()
    body={'origin_hub':'FIHEL','destination_hub':'FITMP','mode':'road','capacity_kg':3600,'capacity_m3':16,'cutoff':win(72)['start']}
    master=post('/consolidations',body)
    assert post(f"/consolidations/{master['id']}/houses",{'shipment_id':a['id']})['weight_kg']==1800
    assert post(f"/consolidations/{master['id']}/houses",{'shipment_id':b['id']})['volume_m3']==16
    third=ship()
    post(f"/consolidations/{master['id']}/houses",{'shipment_id':third['id']},status=409)
    assert not db.shipments.find_one({'id':third['id']}).get('consolidation_id')
    other=post('/consolidations',body)
    post(f"/consolidations/{other['id']}/houses",{'shipment_id':a['id']},status=409)
    assert post(f"/consolidations/{master['id']}/actions/close")['status']=='closed'
    post(f"/consolidations/{master['id']}/actions/deconsolidate",{'evidence':'scan-early'},status=409)
    post(f"/consolidations/{master['id']}/actions/arrive",status=422)
    assert post(f"/consolidations/{master['id']}/actions/arrive",{'evidence':'TEST-TERMINAL-ARRIVAL-001'})['status']=='arrived'
    assert post(f"/consolidations/{master['id']}/actions/deconsolidate",{'evidence':'TEST-UNLOAD-001'})['status']=='deconsolidated'
    csv=ops().get('/execution/consolidations/'+master['id']+'/load-plan.csv')
    assert csv.status_code==200 and a['reference'] in csv.text and '1800' in csv.text

def test_crossdock_sequence_discrepancy_and_complete_scan_chain():
    s=ship();row=post('/crossdock',{'shipment_id':s['id'],'hub':'FIHEL','packages':4})
    post(f"/crossdock/{row['id']}/steps",{'action':'depart','packages':4,'evidence':'TEST-EARLY'},status=409)
    for action in ('arrive','unload','sort','reload','depart'):
        r=post(f"/crossdock/{row['id']}/steps",{'action':action,'packages':4,'evidence':'TEST-SCAN-'+action})
    assert r['status']=='complete' and len(r['steps'])==5
    problem=post('/crossdock',{'shipment_id':s['id'],'hub':'FITMP','packages':4})
    r=post(f"/crossdock/{problem['id']}/steps",{'action':'arrive','packages':3,'evidence':'TEST-SHORT-001'})
    assert r['status']=='discrepancy'
    post(f"/crossdock/{problem['id']}/steps",{'action':'unload','packages':4,'evidence':'TEST-NO-RESOLUTION'},status=409)
    post(f"/crossdock/{problem['id']}/reconcile",{'packages':3,'evidence':'TEST-STILL-SHORT'},status=409)
    fixed=post(f"/crossdock/{problem['id']}/reconcile",{'packages':4,'evidence':'TEST-RECOUNT-004'})
    assert fixed['status']=='arrive' and fixed['steps'][0]['packages']==3 and fixed['reconciliations'][0]['packages']==4
    assert post(f"/crossdock/{problem['id']}/steps",{'action':'unload','packages':4,'evidence':'TEST-UNLOAD-004'})['status']=='unload'

def test_warehouse_email_actual_receipt_and_wrong_quantity():
    s=ship();wh=post('/warehouses',{'name':'TEST Helsinki terminal','email':'warehouse@terminal.example.com','provider':'email'})
    row=post('/warehouse-orders',{'shipment_id':s['id'],'kind':'inbound','warehouse_id':wh['id']})
    assert row['status']=='submitted' and row['email_status']=='sent'
    m=imap_fetch('warehouse.terminal-example-com@sandbox.test',s['reference'])
    assert m is not None
    content=m.get_body(('plain',)).get_content()
    url=re.search(r'http[^\s]+/warehouse-reply\?t=[^\s]+',content).group(0)
    token=parse_qs(urlparse(url).query)['t'][0]
    r=httpx.post(API+'/execution/warehouse-reply',params={'t':token},json={'packages':3,'evidence':'TEST-WH-SHORT-001'})
    assert r.status_code==200 and r.json()['status']=='discrepancy'
    r=httpx.post(API+'/execution/warehouse-reply',params={'t':token},json={'packages':4,'evidence':'TEST-WH-RECOUNT-002'})
    assert r.status_code==200 and r.json()['status']=='received'
    replay=httpx.post(API+'/execution/warehouse-reply',params={'t':token},json={'packages':4,'evidence':'REPLAY'})
    assert replay.status_code==409
    assert db.warehouse_orders.find_one({'id':row['id']})['actual_packages']==4

def test_warehouse_not_configured_does_not_claim_sent():
    s=ship();wh=post('/warehouses',{'name':'TEST Disconnected warehouse','provider':'none'})
    r=post('/warehouse-orders',{'shipment_id':s['id'],'kind':'outbound','warehouse_id':wh['id']})
    assert r['status']=='failed' and 'not configured' in r['error']
    post('/warehouse-orders/'+r['id']+'/milestone',{'packages':4,'evidence':'NO-SEND'},status=409)

def test_compliance_hold_blocks_execution_mutations():
    s=ship();db.shipments.update_one({'id':s['id']},{'$set':{'compliance_hold':True}})
    post(f"/shipments/{s['id']}/slots",{'leg_seq':s['legs'][0]['seq'],'kind':'pickup','window':win()},status=409)
    post('/crossdock',{'shipment_id':s['id'],'hub':'FIHEL','packages':4},status=409)

def test_warehouse_odoo_creates_real_stock_operation_and_polls_done():
    if not os.getenv('WMS_ODOO_URL'): pytest.skip('Odoo not configured')
    import xmlrpc.client
    url=os.environ['WMS_ODOO_URL'];database=os.environ['WMS_ODOO_DB'];user=os.environ['WMS_ODOO_USER'];key=os.environ['WMS_ODOO_API_KEY']
    uid=xmlrpc.client.ServerProxy(url+'/xmlrpc/2/common').authenticate(database,user,key,{})
    def call(model,method,args,kw=None):return xmlrpc.client.ServerProxy(url+'/xmlrpc/2/object').execute_kw(database,uid,key,model,method,args,kw or {})
    operation=call('stock.picking.type','search_read',[[['code','=','incoming']]],{'fields':['id'],'limit':1})[0]['id']
    product=call('product.product','create',[{'name':'TEST S9 handling units','type':'consu'}])
    uom=call('product.product','read',[[product]],{'fields':['uom_id']})[0]['uom_id'][0]
    wh=post('/warehouses',{'name':'TEST Odoo terminal','provider':'odoo','odoo_inbound_type_id':operation,'odoo_product_id':product,'odoo_uom_id':uom})
    s=ship();order=post('/warehouse-orders',{'shipment_id':s['id'],'kind':'inbound','warehouse_id':wh['id']})
    assert order['status']=='submitted' and order['external_id']>0,order
    actual=call('stock.picking','read',[[order['external_id']]],{'fields':['origin','state','move_ids']})[0]
    assert actual['origin']=='HUOLITSIJA:'+order['id'] and actual['state']!='done'
    # Counterparty warehouse records physically observed quantity in real Odoo, not in Huolitsija.
    call('stock.move','write',[actual['move_ids'],{'quantity':4,'picked':True}])
    call('stock.picking','button_validate',[[order['external_id']]])
    set_autopilot(True);work(['execution.wms_sync'],fire=True);set_autopilot(False)
    done=db.warehouse_orders.find_one({'id':order['id']})
    assert done['status']=='received' and done['actual_packages']==4,done


def test_slot_email_signed_link_and_proposal_boundary():
    s=ship();carrier=partner_carrier_id()
    old=db.carriers.find_one({'id':carrier})
    db.carriers.update_one({'id':carrier},{'$set':{'contact_email':'dispatch@haulier.example.com'}})
    try:
        row=post(f"/shipments/{s['id']}/slots",{'leg_seq':s['legs'][0]['seq'],'kind':'pickup','window':win()})
        assert row['email_status']=='sent',row
        m=imap_fetch('dispatch.haulier-example-com@sandbox.test',s['reference'])
        text=m.get_body(('plain',)).get_content()
        url=re.search(r'http[^\s]+/slot-reply\?t=[^\s]+',text).group(0)
        token=parse_qs(urlparse(url).query)['t'][0]
        view=httpx.get(API+'/execution/slot-reply',params={'t':token})
        assert view.status_code==200 and view.json()['status']=='requested'
        response=httpx.post(API+'/execution/slot-reply',params={'t':token},json={'action':'confirm'})
        assert response.status_code==200 and response.json()['status']=='confirmed'
        assert httpx.post(API+'/execution/slot-reply',params={'t':token},json={'action':'reject'}).status_code==403
    finally:
        db.carriers.update_one({'id':carrier},{'$set':{'contact_email':old.get('contact_email')}})
