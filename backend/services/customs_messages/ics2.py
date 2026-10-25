"""ICS2 3.2.0.0 HTI house filings and response decoding, official schemas.

F50/F51 are COMPLETE road/rail filings: a house filer must obtain master data
from its carrier. The withdrawn partial road/rail messages are not emitted.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from lxml import etree as E
from core import config
from .schema_codec import DATA, encode, validate, parse_xml, as_dict

BASE=DATA/'ics2'/'3.2.0.0'
FILINGS={'sea':('IE3F14','IE3F15','IE3F16'), 'air':('IE3F22','IE3F23','IE3F24','IE3F26'),
         'road':('IE3F50',), 'rail':('IE3F51',)}
DEFAULT={'sea':'IE3F15','air':'IE3F26','road':'IE3F50','rail':'IE3F51'}
STATES={'IE3R01':'registered','IE3N03':'assessment_complete','IE3Q02':'referral',
        'IE3Q03':'referral','IE3Q01':'do_not_load','IE3N01':'rejected','IE3N99':'rejected',
        'IE3R07':'invalidated','IE3N06':'arrived'}
NAMES={'IE3R01':'ENS registration response','IE3N03':'ENS assessment complete notification',
       'IE3Q02':'Request for additional information','IE3Q03':'Request for screening',
       'IE3Q01':'Do not load request','IE3N01':'ENS filing rejection notification',
       'IE3N99':'Error notification','IE3N06':'Arrival notification',
       'IE3R07':'ENS invalidation acceptance response'}


def path(message):
    if message not in set(sum((list(v) for v in FILINGS.values()),[])) | set(STATES) | {'IE3Q04','IE3R02'}:
        raise ValueError('Unsupported ICS2 message')
    return BASE/('V3' if message in {'IE3F50','IE3F51'} else 'V2')/(message+'.xsd')


def timestamp(): return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def build(message, data):
    return encode(path(message),message,data)


def build_ens(filing):
    mode=filing['mode']; message=filing.get('filing_type') or DEFAULT[mode]
    if message not in FILINGS[mode]: raise ValueError('Filing type does not match mode')
    data=deepcopy(filing['ens_data'])
    data.update(LRN=filing['lrn'],documentIssueDate={'DateTime':timestamp()},
                SpecificCircumstanceIndicator=message[3:])
    data.setdefault('declarant',{})['identificationNumber']=config.env('ICS2_EORI')
    if mode in {'sea','air'} and message not in {'IE3F23','IE3F24'}:
        if data.get('activeBorderTransportMeans',{}).get('ModeOfTransport') != {'sea':'1','air':'4'}[mode]:
            raise ValueError('Transport mode does not match filing')
    return build(message,data)


def parse_response(xml):
    root=parse_xml(xml);message=E.QName(root).localname
    if message not in STATES: raise ValueError('Unsupported ICS2 response')
    validate(xml,path(message));data=as_dict(root)
    mrn=data.get('MRN')
    related=data.get('relatedMRN',[])
    if isinstance(related,dict):related=[related]
    return {'type':message,'name':NAMES[message],'status':STATES[message],
        'mrn':mrn,'related_mrns':[v.get('identification') for v in related],
        'lrn':data.get('LRN'),'functional_reference':data.get('functionalReference'),'data':data}


def _dt(value):
    d=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    if not isinstance(d,datetime) or d.tzinfo is None:raise ValueError('Timezone-aware time required')
    return d.astimezone(timezone.utc)


def filing_deadlines(mode, *, arrival, loading=None, departure=None, duration_hours=None,
                     sea_category=None, air_minimum=False):
    """Delegated Regulation 2015/2446 Arts 105–109, NOT 2015/2447.

    sea_category must explicitly identify containerised, bulk, short_sea, or
    overseas_under_24h; caller classifies the voyage under Article 105(1)(c/d).
    Rail duration is from the LAST train formation station outside the EU.
    """
    arrival=_dt(arrival); result={}
    if mode=='sea':
        if sea_category=='containerised':
            result['filing']=_dt(loading)-timedelta(hours=24)
        elif sea_category=='bulk':result['filing']=arrival-timedelta(hours=4)
        elif sea_category in {'short_sea','overseas_under_24h'}:result['filing']=arrival-timedelta(hours=2)
        else:raise ValueError('Explicit Article 105 sea category required')
        article=105
    elif mode=='air':
        result['pre_loading']=_dt(loading)
        if duration_hours is None or duration_hours<0:raise ValueError('Flight duration required')
        result['pre_arrival']=_dt(departure) if duration_hours<4 else arrival-timedelta(hours=4)
        result['filing']=result['pre_loading'] if air_minimum else min(result.values())
        article=106
    elif mode=='rail':
        if duration_hours is None or duration_hours<0:raise ValueError('Rail journey duration required')
        result['filing']=arrival-timedelta(hours=1 if duration_hours<2 else 2);article=107
    elif mode=='road':result['filing']=arrival-timedelta(hours=1);article=108
    elif mode=='inland_waterway':result['filing']=arrival-timedelta(hours=2);article=109
    else:raise ValueError('Unsupported mode')
    return {**{k:v.isoformat() for k,v in result.items()},'legal_basis':f'Regulation (EU) 2015/2446 Article {article}',
            'source_url':'https://eur-lex.europa.eu/eli/reg_del/2015/2446/oj/eng'}


def deadlines_for(filing):
    result=filing_deadlines(filing['mode'],**filing['deadline_inputs'])
    if filing['mode']=='air':
        ft=filing.get('filing_type') or DEFAULT['air']
        if ft=='IE3F22':result['filing']=result['pre_arrival']
        elif ft in {'IE3F23','IE3F24'}:result['filing']=result['pre_loading']
    return result
