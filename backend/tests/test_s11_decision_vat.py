"""Decision accounting preserves unavailable values and requires attributable sources."""
import copy
from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from services import customs_decisions as svc

DECL = {"id":"vat-test", "shipment_id":"vat-shipment", "company_id":"vat-company",
        "status":"released", "mrn":"26FI123456789012R4", "items":[{"seq":1}],
        "totals":{"vat_base_cents":999999, "vat_cents":888888}, "importer":{"business_id":"customer"}}
BODY = {"mrn":DECL["mrn"], "decision_date":date.today().isoformat(), "paid_by":"forwarder",
        "items":[{"seq":1, "duty_cents":100, "vat_cents":2550}]}


def source_body(amount=10000, deduction=2550):
    return {**copy.deepcopy(BODY), "document_id":"decision-doc", "importer_business_id":"1234567-1",
            "vat_base_cents":amount, "deductible_vat_cents":deduction,
            "vat_base_source":{"document_id":"decision-doc", "basis":"Assessment taxable base line 3"},
            "deductible_vat_source":{"document_id":"assessment-doc", "basis":"Documented deduction assessment line 4"}}


def test_absent_values_stay_unavailable_despite_estimates_and_payer():
    for additions in ({}, {"vat_base_cents":None,"deductible_vat_cents":None}):
        d=svc.decision_values(DECL,{**BODY,**additions})
        assert all(d[k] is None for k in ('vat_base_cents','deductible_vat_cents','importer_business_id',
                                          'vat_base_source','deductible_vat_source'))
        assert d['vat_cents']==2550


@pytest.mark.parametrize('value',[0,10000])
def test_explicit_source_backed_values_and_zero(value):
    d=svc.decision_values(DECL,source_body(value,0))
    assert d['vat_base_cents']==value and d['deductible_vat_cents']==0
    assert d['importer_business_id']=='1234567-1' and d['currency']=='EUR'
    assert d['deductible_vat_source']['document_id']=='assessment-doc'


@pytest.mark.parametrize('change',[
    {'vat_base_cents':True},{'vat_base_cents':1.5},{'vat_base_cents':-1},
    {'deductible_vat_cents':2551},{'vat_base_source':None},
    {'deductible_vat_source':{'document_id':'assessment-doc','basis':' '}},
    {'importer_business_id':None},{'document_id':None},{'currency':'USD'},
])
def test_invalid_or_unattributable_amounts_refused(change):
    with pytest.raises(HTTPException): svc.decision_values(DECL,{**source_body(),**change})


def test_simulation_cannot_be_accounting_evidence():
    with pytest.raises(HTTPException):
        svc.decision_values({**DECL,'submission':{'channel':'sandbox'}},source_body())


@pytest.mark.asyncio
async def test_record_persists_sources_and_assigns_actor_time(monkeypatch):
    class Collection:
        def __init__(self): self.saved=None
        async def update_one(self, query, update):
            self.saved=update['$set']['customs_decision']
            return type('Result',(),{'modified_count':1})()
        async def find_one(self,*a): return {**DECL,'customs_decision':self.saved}
    col=Collection()
    docs=AsyncMock(); docs.find_one.return_value={'id':'document'}
    monkeypatch.setattr(svc,'db',type('DB',(),{'documents':docs,'customs_declarations':col})())
    monkeypatch.setattr(svc,'audit',AsyncMock()); monkeypatch.setattr(svc,'emit',AsyncMock())
    body=source_body(); body['deductible_vat_source'].update(recorded_by='forged',recorded_at='1900-01-01')
    result=await svc.record_decision(DECL,body,{'id':'ops','name':'Reviewer','type':'user'})
    d=result['customs_decision']
    assert (d['vat_base_cents'],d['deductible_vat_cents'])==(10000,2550)
    assert d['deductible_vat_source']['recorded_by']==d['recorded_by']!='forged'
    assert d['deductible_vat_source']['recorded_at']==d['recorded_at']!='1900-01-01'
    assert len(docs.find_one.call_args_list)==3
    assert all(call.args[0]['shipment_id']=='vat-shipment' for call in docs.find_one.call_args_list)


@pytest.mark.asyncio
async def test_foreign_shipment_assessment_refused_before_write(monkeypatch):
    docs=AsyncMock(); docs.find_one.side_effect=[{'id':'decision-doc'},{'id':'decision-doc'},None]
    col=AsyncMock()
    monkeypatch.setattr(svc,'db',type('DB',(),{'documents':docs,'customs_declarations':col})())
    with pytest.raises(HTTPException): await svc.record_decision(DECL,source_body(),{'id':'ops'})
    col.update_one.assert_not_called()
