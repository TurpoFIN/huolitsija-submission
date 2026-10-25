"""Independent EC table expectations and real production location-resolution boundaries."""
import asyncio
from unittest.mock import AsyncMock, patch
import pytest
from services import customs_border as border, geo, pricing, shipments

# EC territorial-scope table checked 2026-10-25. Booleans describe common EU goods areas;
# GI has EU customs rules administered during transition, but remains a third territory for formalities.
EU_ROWS = 'AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE'.split()
SPECIAL_ROWS = [
    ('FO','FO',False,False,False), ('GL','GL',False,False,False),
    ('AX','AX',True,False,False),
    *[(c,c,True,False,False) for c in ('GP','GF','MQ','RE','YT','MF')],
    *[(c,c,False,False,False) for c in ('PF','NC','PM','WF','TF','BL')],
    ('DE','DE-HGL',False,False,False), ('DE','DE-BUS',False,False,False),
    ('GR','GR-69',True,False,True), ('IT','IT-LIVIGNO',False,False,False),
    ('IT','IT-CAMPIONE',True,False,True), ('IT','IT-LUGANO-WATERS',True,False,True),
    *[(c,c,False,False,False) for c in ('AN','AW','BQ','CW','SX')],
    ('PT','PT-20',True,True,True), ('PT','PT-30',True,True,True),
    ('ES','ES-CE',False,False,False), ('ES','ES-ML',False,False,False),
    ('ES','IC',True,False,False), ('MC','MC',True,True,True),
    ('SM','SM',True,False,True), ('GB','CY-SBA',True,True,True),
    ('GI','GI',False,False,False), # Transitional third territory: formalities remain (AEAT source).
    ('AD','AD',False,False,False), ('VA','VA',False,False,False), ('GB','XI',True,True,True),
]


def test_every_ec_table_row():
    ordinary={'FI':'FIHEL','DE':'DEHAM','IT':'ITROM','FR':'FRPAR','GR':'GRPIR','DK':'DKCPH','NL':'NLRTM','ES':'BCN'}
    for country in EU_ROWS:
        t = border.territory({'country':country,**({'unlocode':ordinary[country]} if country in ordinary else {})})
        assert [t[k]=='EU' for k in ('customs_area','vat_area','excise_area')] == [True]*3, country
    for country,code,customs,vat,excise in SPECIAL_ROWS:
        t = border.territory({'country':country,'territory_code':code})
        assert [t[k]=='EU' for k in ('customs_area','vat_area','excise_area')] == [customs,vat,excise], code
        assert t['eu_customs_rules'] is (customs or code=='GI'), code
        assert t['verified'] is True, code


def test_reviewers_actual_unlocode_path():
    async def run():
        origin=geo.public_location(geo.lookup_code('FIHEL'))
        with patch('services.gazetteer.ready',AsyncMock()):
            for code,required,territory in [('ESLPA',True,'IC'),('ESTCI',True,'IC'),('GBBEL',False,'XI'),('DEHGL',True,'DE-HGL')]:
                source=geo.public_location(geo.lookup_code(code))
                resolved=await shipments._resolve(code)
                assert resolved['subdivision']==source['subdivision']
                normalized=await pricing.normalize_request({'origin':origin,'destination':source,'mode':'air','cargo':{'packages':2,'weight_kg':600}})
                result=border.assess(origin,resolved)
                assert result['customs_required'] is required and normalized['customs_required'] is required,code
                assert result['destination_territory']['code']==territory,code
    asyncio.run(run())


@pytest.mark.parametrize('country',['FI','DE','IT','ES','FR','GR','DK','NL','GB'])
@pytest.mark.parametrize('subdivision',[None,'UNKNOWN'])
def test_unverified_territory_never_silently_false(country,subdivision):
    request={'origin':{'country':'FI','unlocode':'FIHEL'},
             'destination':{'country':country,'subdivision':subdivision,'name':'Text claiming ordinary mainland'},
             'customs_handling':'customer_broker'}
    border.apply(request)
    assert request['customs_required'] is None
    assert request['customs_territory_verified'] is False
    assert border.quote_needs_review({'request':request}) is True
    assert border.review_reason({'request':request})=='territory_unverified'
    row=border.postponed_row(request)
    assert row['status']=='territory_unverified' and row['reason_code']=='customs.territory_unverified'


def test_most_specific_locode_overrides_broader_subdivision():
    # The island and the ordinary mainland share SH; a claimed mainland subdivision cannot erase DEHGL.
    t=border.territory({'country':'DE','unlocode':'DEHGL','subdivision':'HH'})
    assert t['code']=='DE-HGL' and t['customs_area']!='EU'
    assert border.territory({'country':'ES','unlocode':'ESLPA','subdivision':'M'})['code']=='IC'
    assert border.territory({'country':'GB','unlocode':'GBBEL','subdivision':'LND'})['code']=='XI'


def test_projection_preserves_territory_designation():
    d={'country':'IT','territory_code':'IT-LUGANO-WATERS','subdivision':'CO','name':'Waters','lat':46,'lon':9}
    assert geo.public_location(d)['territory_code']=='IT-LUGANO-WATERS'
    assert geo.location_ref(d)['territory_code']=='IT-LUGANO-WATERS'
    assert geo.location_ref(d)['subdivision']=='CO'

    assert pricing._norm_loc(d)['territory_code']=='IT-LUGANO-WATERS'
    async def run():
        given=await geo.find_location(d)
        resolved=await shipments._resolve(d)
        for value in (given,resolved):
            assert value['territory_code']=='IT-LUGANO-WATERS' and value['subdivision']=='CO'
    asyncio.run(run())
