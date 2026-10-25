"""NCTS P6 DDNTA 6.4.1 / XSD 60.4.16 (EU common messages).

Finland's external interface uses FIxxxA: see ncts_fi.py. CC messages must
never be posted directly to Tulli's national message exchange.
"""
from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timezone
from uuid import uuid4
from lxml import etree as E
from core import config
from .schema_codec import DATA, encode, validate, parse_xml, as_dict

DIRECTORY = DATA / 'ncts' / '60.4.16'
NAMES = {
 'CC015C': 'Declaration data', 'CC007C': 'Arrival notification',
 'CC044C': 'Unloading remarks', 'CC014C': 'Declaration invalidation request',
 'CC028C': 'MRN allocated', 'CC029C': 'Release for transit',
 'CC056C': 'Rejection from office of departure', 'CC060C': 'Control decision notification',
 'CC025C': 'Goods release notification', 'CC043C': 'Unloading permission',
 'CC009C': 'Invalidation decision', 'CC045C': 'Write-off notification',
}
STATES = {'CC028C':'accepted', 'CC029C':'released', 'CC056C':'rejected',
          'CC060C':'under_control', 'CC025C':'arrived', 'CC043C':'unloading',
          'CC009C':'invalidated', 'CC045C':'closed'}


def timestamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def build(message, payload, *, sender=None, recipient='NTA.FI', message_id=None):
    if message not in NAMES: raise ValueError('Unsupported NCTS message')
    data = deepcopy(payload)
    data.update(messageSender=sender or config.env('TULLI_DECLARANT_ID'),
                messageRecipient=recipient, preparationDateAndTime=timestamp(),
                messageIdentification=message_id or uuid4().hex, messageType=message)
    return encode(DIRECTORY / f'{message}.xsd', message, data)


def declaration_data(d):
    if d.get('ncts_data'):
        data = deepcopy(d['ncts_data'])
        data['TransitOperation']['LRN'] = d['lrn']
        data['HolderOfTheTransitProcedure'] = {'identificationNumber': config.env('TULLI_HOLDER_EORI', config.env('TULLI_DECLARANT_ID'))}
        return data
    holder = config.env('TULLI_HOLDER_EORI', config.env('TULLI_DECLARANT_ID'))
    items = []
    for seq, it in enumerate(d['items'], 1):
        items.append({'goodsItemNumber':seq, 'declarationGoodsItemNumber':seq,
          'Commodity': {'descriptionOfGoods':it['description'],
                        'CommodityCode':{'harmonizedSystemSubHeadingCode':it['hs_code'][:6]},
                        'GoodsMeasure':{'grossMass':it['gross_mass']}},
          'Packaging':[{'sequenceNumber':1, 'typeOfPackages':it['package_type'],
                        'numberOfPackages':it['packages'], 'shippingMarks':it['shipping_marks']}]})
    total = str(sum(Decimal(str(i['gross_mass'])) for i in d['items']))
    consignment = deepcopy(d['transport'])
    consignment.update(grossMass=total, HouseConsignment=[{'sequenceNumber':1, 'grossMass':total,
                                                        'ConsignmentItem':items}])
    if d.get('seals'):
        consignment['TransportEquipment'] = [{'sequenceNumber':1,'numberOfSeals':len(d['seals']),
           'Seal':[{'sequenceNumber':n,'identifier':s} for n,s in enumerate(d['seals'],1)],
           'GoodsReference':[{'sequenceNumber':n,'declarationGoodsItemNumber':n} for n in range(1,len(items)+1)]}]
    return {'TransitOperation':{'LRN':d['lrn'],'declarationType':d['declaration_type'],
              'additionalDeclarationType':d.get('additional_declaration_type','A'),
              'security':'0','reducedDatasetIndicator':'0','bindingItinerary':'0'},
        'CustomsOfficeOfDeparture':{'referenceNumber':d['office_of_departure']},
        'CustomsOfficeOfDestinationDeclared':{'referenceNumber':d['office_of_destination']},
        'CustomsOfficeOfTransitDeclared':[{'sequenceNumber':n,'referenceNumber':s} for n,s in enumerate(d.get('offices_of_transit',[]),1)],
        'HolderOfTheTransitProcedure':{'identificationNumber':holder, 'name':config.env('TULLI_HOLDER_NAME')},
        'Guarantee':[{'sequenceNumber':1,'guaranteeType':d.get('guarantee_type','1'),
            'GuaranteeReference':[{'sequenceNumber':1,'GRN':config.env('TULLI_GUARANTEE_GRN'),
              'accessCode':config.env('TULLI_GUARANTEE_ACCESS_CODE'),
              'amountToBeCovered':d['guarantee_amount'],'currency':d['guarantee_currency']}]}],
        'Consignment':consignment}


def build_cc015c(decl, **kw): return build('CC015C', declaration_data(decl), **kw)
def build_cc007c(data, **kw): return build('CC007C', data, **kw)
def build_cc044c(data, **kw): return build('CC044C', data, **kw)
def build_cc014c(data, **kw): return build('CC014C', data, **kw)


def parse_response(xml):
    root = parse_xml(xml)
    message = E.QName(root).localname
    if message not in STATES: raise ValueError('Unsupported NCTS response')
    validate(xml, DIRECTORY / f'{message}.xsd')
    data = as_dict(root)
    if data['messageType'] != message: raise ValueError('NCTS header type mismatch')
    op = data['TransitOperation']
    state = STATES[message]
    if message == 'CC009C' and data['Invalidation'].get('decision') != '1':
        state = None  # refusal is not an invalidation
    return {'type':message,'name':NAMES[message],'status':state,'mrn':op.get('MRN'),
            'lrn':op.get('LRN'),'message_id':data['messageIdentification'],
            'correlation_id':data.get('correlationIdentifier'),'data':data}
