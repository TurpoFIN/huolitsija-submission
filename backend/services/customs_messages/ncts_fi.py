"""Finnish external transit messages, Tulli MIG 2.2.0 (NCTS P6).

National data is explicitly supplied using the MIG field names; it is not
obtained by renaming CC elements, whose contracts differ materially.
"""
from copy import deepcopy
from lxml import etree as E
from core import config
from .schema_codec import DATA, encode, validate, parse_xml, as_dict
DIRECTORY=DATA/'ncts-fi'/'2.2.0'
OUTBOUND={'CC015C':'FI015A','CC007C':'FI007A','CC014C':'FI014A','CC044C':'FI044A'}
STATES={'FI026A':'submitted','FI028A':'accepted','FI029A':'released','FI016A':'rejected',
        'FI050A':'under_control','FI025A':'arrived','FI043A':'unloading',
        'FI010A':'invalidated','FI045A':'closed'}


def build(message,data):
    if message not in OUTBOUND.values():raise ValueError('Unsupported Finnish transit message')
    return encode(DIRECTORY/(message+'.xsd'),message,data)


def build_for(decl,cc_message):
    message=OUTBOUND[cc_message]
    data=deepcopy(decl.get('national_data',{}).get(message))
    if not data: raise ValueError(f'{message}: Finnish national data is required')
    if message=='FI015A':
        data['Principal']['Identification']=config.env('TULLI_HOLDER_EORI',config.env('TULLI_DECLARANT_ID'))
        data['FunctionalReference']={'@function':'LRN','#text':decl['lrn']}
        if data['DepartureOffice']['Identification']!=decl['office_of_departure'] or data['DestinationOffice']['Identification']!=decl['office_of_destination']:
            raise ValueError('Finnish office data does not match declaration offices')
        if str(data['Security'])!='0':raise ValueError('Finland P6 opt-out: lodge ENS separately in ICS2')
    return build(message,data)


def parse_response(xml):
    root=parse_xml(xml);message=E.QName(root).localname
    if message not in STATES:raise ValueError('Unsupported Finnish transit response')
    validate(xml,DIRECTORY/(message+'.xsd'))
    refs={e.get('function'):e.text for e in root.iter() if E.QName(e).localname=='FunctionalReference'}
    return {'type':message,'status':STATES[message],'mrn':refs.get('MRN'),'lrn':refs.get('LRN'),
            'data':as_dict(root)}
