"""Actual provider captures plus applicability, arithmetic and HTTP boundary checks.
These captures are published tariffs, not carrier offers or fabricated customers.
"""
import asyncio
import copy
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from services import public_surcharges as s
from routes.public_surcharges import router

CAPTURES = Path(__file__).parent / "fixtures/public_surcharges"
FILES = {"finnlines":"finnlines.json", "bluedart":"bluedart.html", "swissport":"swissport.pdf",
         "maersk":"maersk-cn.html", "arkas":"arkas.pdf"}


def row(provider, ident=None):
    rows=s.parse_source(provider,(CAPTURES/FILES[provider]).read_bytes())
    if ident: rows=[r for r in rows if r["id"]==ident]
    return max(rows,key=lambda r:r["effective_from"])


def context(provider, **kw):
    ctx={"provider":provider,"service":"FCL","origin":"TRALI","destination":"SAJED",
         "on_date":"2026-10-25","date_basis":"booking_date","equipment":"40HC","containers":2}
    ctx.update(kw);return ctx


def test_finnlines_current_json_comma_decimals_and_dated_records():
    r=row("finnlines","finnlines-baf-finnlink")
    assert r["rate"]=="6.38" and r["effective_from"]=="2026-10-01"
    c=context("finnlines",service="RORO",origin="FINLI",destination="SEKPS",lane_metres="13.6")
    assert s.evaluate(r,c)["amount"]=="86.77"
    assert s.evaluate(r,{**c,"origin":"SEKPS","destination":"FINLI"})["amount"]=="86.77"
    assert s.evaluate(row("finnlines","finnlines-baf-nordolink"),{**c,"origin":"SEMMA","destination":"DETRV"})["amount"]=="195.84"


@pytest.mark.parametrize("change,reason",[({"provider":"maersk"},"provider_or_service_mismatch"),
 ({"service":"ROAD_FTL"},"provider_or_service_mismatch"),({"destination":"FIHEL"},"lane_mismatch"),
 ({"on_date":"2026-09-30"},"not_yet_effective"),({"on_date":"2026-11-01"},"tariff_period_expired"),
 ({"lane_metres":None},"lane_metres_required"),({"lane_metres":"0"},"lane_metres_must_be_positive")])
def test_finnlines_refuses_wrong_scope_and_missing_values(change,reason):
    c=context("finnlines",service="RORO",origin="FINLI",destination="SEKPS",lane_metres="13.6")
    result=s.evaluate(row("finnlines","finnlines-baf-finnlink"),{**c,**change})
    assert result["amount"] is None and result["reason"]==reason and result["bookable"] is False


def test_caf_requires_explicit_base_and_currency():
    r=row("bluedart")
    c=context("bluedart",service="AIR",origin="INBOM",destination="INDEL",caf_subject_amount="10000",base_currency="INR")
    assert s.evaluate(r,c)["amount"]=="2250.00"
    assert s.evaluate(r,{**c,"caf_subject_amount":None})["state"]=="needs_input"
    assert s.evaluate(r,{**c,"base_currency":"EUR"})["amount"] is None
    assert s.evaluate(r,{**c,"destination":"FIHEL"})["amount"] is None


@pytest.mark.parametrize("weight,amount",[(1,"13.00"),(1000,"150.00"),(10000,"1139.00")])
def test_swissport_import_minimum_per_kg_and_cap(weight,amount):
    c=context("swissport",service="AIR",destination="FIHEL",chargeable_kg=weight)
    r=s.evaluate(row("swissport","swissport-hel-import-thc"),c)
    assert r["amount"]==amount and r["tax_included"] is False


@pytest.mark.parametrize("weight,amount",[(1,"14.00"),(1000,"75.00"),(10000,"119.00")])
def test_swissport_export_half_cent_and_cap(weight,amount):
    c=context("swissport",service="AIR",origin="FIHEL",destination="DEHAM",chargeable_kg=weight)
    assert s.evaluate(row("swissport","swissport-hel-export-thc"),c)["amount"]==amount


def test_pss_latest_postponement_and_price_date():
    r=row("maersk")
    assert r["effective_from"]=="2026-12-01"
    c=context("maersk",origin="ESBCN",destination="USNYC",date_basis="carrier_price_calculation_date")
    assert s.evaluate(r,c)["reason"]=="not_yet_effective"
    assert s.evaluate(r,{**c,"on_date":"2026-12-01"})["amount"]=="500.00"
    assert s.evaluate(r,{**c,"on_date":"2026-12-01","date_basis":"service_date"})["state"]=="needs_input"
    assert s.evaluate(r,{**c,"on_date":"2026-12-01","origin":"FIHEL"})["amount"] is None


@pytest.mark.parametrize("port,equipment,amount",[("SAJED","20GP","4000.00"),("JOAQJ","40HC","6000.00"),
 ("DJJIB","20GP","5000.00"),("SDPZU","40GP","10000.00")])
def test_arkas_exact_port_equipment_amounts(port,equipment,amount):
    ident="arkas-wrs-sajed" if port in ["SAJED","JOAQJ"] else "arkas-wrs-djjib"
    assert s.evaluate(row("arkas",ident),context("arkas",destination=port,equipment=equipment))["amount"]==amount


@pytest.mark.parametrize("change,reason",[({"origin":"USNYC"},"excluded_trade"),({"equipment":"40RF"},"equipment_not_published"),
 ({"destination":"SADMM"},"lane_mismatch"),({"containers":1.5},"whole_container_count_required"),
 ({"date_basis":"service_date"},"booking_date_required")])
def test_arkas_exclusions(change,reason):
    out=s.evaluate(row("arkas","arkas-wrs-sajed"),context("arkas",**change))
    assert out["amount"] is None and out["reason"]==reason


@pytest.mark.asyncio
async def test_download_failure_never_returns_stale_price(monkeypatch):
    s._CACHE.clear();s._LOCKS.clear()
    async def failed(url):raise httpx.ConnectError("test disconnection")
    monkeypatch.setattr(s,"_download",failed)
    out=await s.calculate("finnlines-baf-finnlink",{})
    assert out["state"]=="unavailable" and out["amount"] is None and not out["items"]
    s._CACHE.clear();s._LOCKS.clear()


@pytest.mark.asyncio
async def test_actual_http_contract_and_provenance(monkeypatch):
    s._CACHE.clear();s._LOCKS.clear()
    async def captured(url):
        provider=next(k for k,v in s.SOURCES.items() if v==url)
        return (CAPTURES/FILES[provider]).read_bytes()
    monkeypatch.setattr(s,"_download",captured)
    app=FastAPI();app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        ctx=context("finnlines",service="RORO",origin="FINLI",destination="SEKPS",lane_metres="13.6")
        ctx["tariff_id"]="finnlines-baf-finnlink"
        resp=await client.post("/api/public/surcharges/calculate",json=ctx)
        assert resp.status_code==200,resp.text
        out=resp.json();assert out["amount"]=="86.77"
        assert out["tariff"]["source"]["url"]==s.SOURCES["finnlines"]
        assert len(out["tariff"]["source"]["sha256"])==64
        for value in ["NaN","Infinity",-1]:
            bad=await client.post("/api/public/surcharges/calculate",json={**ctx,"lane_metres":value})
            assert bad.status_code==422
        bad=await client.post("/api/public/surcharges/calculate",json={**ctx,"on_date":"2026-99-99"})
        assert bad.status_code==422
    s._CACHE.clear();s._LOCKS.clear()


def test_changed_publication_fails_closed():
    with pytest.raises(s.SourceUnavailable):s.parse_source("maersk",b'<h1>Access denied</h1>')
    with pytest.raises(s.SourceUnavailable):s.parse_source("bluedart",b'<h1>No current CAF</h1>')


@pytest.fixture
def replay(monkeypatch):
    s._CACHE.clear();s._LOCKS.clear()
    async def captured(url):
        provider=next(k for k,v in s.SOURCES.items() if v==url)
        return (CAPTURES/FILES[provider]).read_bytes()
    monkeypatch.setattr(s,"_download",captured)
    yield
    s._CACHE.clear();s._LOCKS.clear()


def quote_input():
    from services import pricing
    metrics=pricing.cargo_metrics({"load_type":"FTL","weight_kg":20000,"volume_m3":40,"packages":1})
    req={"ready_date":"2026-10-25"}
    opt={"legs":[{"seq":1,"kind":"main","from":{"unlocode":"FINLI"},"to":{"unlocode":"SEKPS"}}],
         "firmness":"estimate","bookable":False}
    card={"service":"RORO","public_surcharge_refs":[{"tariff_id":"finnlines-baf-finnlink"}]}
    carrier={"code":"FINNLINES"}
    return req,opt,card,carrier,metrics


@pytest.mark.asyncio
async def test_quote_hook_adds_real_cost_and_preserves_estimate(replay):
    req,opt,card,carrier,metrics=quote_input()
    before=copy.deepcopy(opt)
    lines,errors=await s.quote_lines(req,opt,card,carrier,metrics,[])
    assert not errors and len(lines)==1
    assert lines[0]["unit_cost_cents"]==8677
    assert lines[0]["original_currency"]=="EUR"
    assert lines[0]["public_tariff_calculation"]["quantity"]=="13.6"
    assert opt==before and opt["bookable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind",["included","itemized"])
async def test_quote_hook_never_double_charges(replay,kind):
    req,opt,card,carrier,metrics=quote_input()
    existing=[]
    if kind=="included":card["included_charge_codes"]=["BAF"]
    else:existing=[{"code":"BAF","leg_seq":1,"unit_cost_cents":1000}]
    lines,errors=await s.quote_lines(req,opt,card,carrier,metrics,existing)
    assert not lines and not errors


@pytest.mark.asyncio
async def test_quote_hook_requires_correct_provider_and_no_duplicate_refs(replay):
    req,opt,card,carrier,metrics=quote_input()
    lines,errors=await s.quote_lines(req,opt,card,{"code":"MAEU"},metrics,[])
    assert not lines and errors==["finnlines-baf-finnlink:provider_binding_required"]
    card["public_surcharge_refs"]*=2
    lines,errors=await s.quote_lines(req,opt,card,carrier,metrics,[])
    assert len(lines)==1 and errors==["finnlines-baf-finnlink:duplicate_reference"]


@pytest.mark.asyncio
async def test_unconfigured_quote_hook_has_no_network_or_price_changes(monkeypatch):
    async def forbidden(*args):raise AssertionError("No network for baseline cards")
    monkeypatch.setattr(s,"calculate",forbidden)
    assert await s.quote_lines({}, {}, {}, {}, {}, []) == ([],[])


@pytest.mark.asyncio
async def test_inclusive_rate_does_not_depend_on_public_feed(monkeypatch):
    req,opt,card,carrier,metrics=quote_input();card['included_charge_codes']=['BAF']
    async def forbidden(*args):raise AssertionError('Inclusive rates need no public lookup')
    monkeypatch.setattr(s,'calculate',forbidden)
    assert await s.quote_lines(req,opt,card,carrier,metrics,[]) == ([],[])


@pytest.mark.asyncio
async def test_expired_or_missing_required_tariff_marks_option_incomplete(replay,monkeypatch):
    req,opt,card,carrier,metrics=quote_input()
    req['ready_date']='2026-11-01'
    lines,errors=await s.quote_lines(req,opt,card,carrier,metrics,[])
    assert not lines and errors==['finnlines-baf-finnlink:tariff_period_expired']
    async def missing(*args):return {'state':'unavailable','amount':None,'reason':'source_unavailable'}
    monkeypatch.setattr(s,'calculate',missing)
    lines,errors=await s.quote_lines(req,opt,card,carrier,metrics,[])
    assert not lines and errors==['finnlines-baf-finnlink:source_unavailable']
