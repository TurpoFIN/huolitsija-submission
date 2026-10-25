"""Serialization against pinned, unmodified Finnish Customs schema contracts."""
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
import hashlib
from lxml import etree
import xmlschema

ROOT = Path(__file__).resolve().parents[2] / 'data/xsd/tulli'
VERSIONS = {'import': '2.1.7', 'export': '1.1.1'}
XSD_VERSIONS = {'import': '3.0.37', 'export': '1.0.90'}
NAMESPACES = {'import': 'http://tulli.fi/schema/external/import/ext/v3',
              'export': 'http://tulli.fi/schema/external/export/ext/v1'}
XS = '{http://www.w3.org/2001/XMLSchema}'


class TulliValidationError(ValueError):
    def __init__(self, xml, result):
        self.xml, self.result = xml, result
        super().__init__('Tulli XSD: ' + '; '.join(result['errors']))



def parse(xml):
    return etree.fromstring(xml.encode() if isinstance(xml, str) else xml,
                            etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))


@lru_cache(maxsize=80)
def schema(system, message):
    filename = message[:-1] if system == 'import' else message
    return xmlschema.XMLSchema(str(ROOT / system / VERSIONS[system] / (filename + '.xsd')), allow='local')


def validate(xml, system):
    root = parse(xml)
    message = etree.QName(root).localname
    s = schema(system, message)
    errors = [str(e) for e in s.iter_errors(etree.tostring(root))]
    valid = not errors
    return {'valid': valid, 'message_type': message, 'schema_version': XSD_VERSIONS[system],
            'mig_version': VERSIONS[system], 'sha256': hashlib.sha256(etree.tostring(root)).hexdigest(),
            'errors': errors}


def assert_valid(xml, system):
    result = validate(xml, system)
    if not result['valid']:
        raise TulliValidationError(xml, result)
    return result


@lru_cache(maxsize=2)
def model(system):
    types, elements = {}, {}
    for path in (ROOT / system / VERSIONS[system]).glob('*.xsd'):
        for node in etree.parse(str(path)).getroot():
            if node.get('name'):
                (elements if node.tag == XS+'element' else types)[node.get('name')] = node
    return types, elements


def serialize(system, message, values, *, validate_output=True):
    """Use XSD sequence order; never generate values from examples or schema defaults."""
    types, elements = model(system)
    ns = NAMESPACES[system]
    def build(name, kind, value):
        node = etree.Element('{'+ns+'}'+name, nsmap={None: ns})
        if not isinstance(value, dict):
            node.text = str(value)
            return node
        for key, val in value.get('@', {}).items():
            node.set(key, str(val))
        if '#' in value: node.text = str(value['#'])
        spec = types.get(kind)
        children = [] if spec is None else spec.findall(XS+'sequence/'+XS+'element')
        allowed = {e.get('name') for e in children}
        unknown = set(value) - allowed - {'@', '#'}
        if unknown: raise ValueError(f'Unsupported Tulli fields in {name}: {sorted(unknown)}')
        for child in children:
            val = value.get(child.get('name'))
            if val is None: continue
            for item in val if isinstance(val, list) else [val]:
                node.append(build(child.get('name'), child.get('type'), item))
        return node
    root = build(message, elements[message].get('type'), values)
    xml = etree.tostring(root, xml_declaration=True, encoding='UTF-8', pretty_print=True).decode()
    if validate_output:
        assert_valid(xml, system)
    return xml


def val(text, **attrs):
    if text is None or text == '': return None
    return {'#': text, '@': {k:v for k,v in attrs.items() if v is not None}}


def amount(cents):
    return None if cents is None else str(Decimal(str(cents)) / 100)


def address(p):
    a = p.get('address') or {}
    if isinstance(a, str): a = {'street': a}
    if not a: return None
    return {k:v for k,v in {'Line': a.get('street'), 'CityName': a.get('city'),
             'Postcode': a.get('postal_code'), 'Country': a.get('country') or p.get('country')}.items() if v}


def party(p, role=False, representative=None):
    if not p: return None
    a = address(p)
    if p.get('eori') and (not a or not all(a.get(k) for k in ('Line','CityName','Postcode','Country'))):
        a = None  # MIG: registered EORI identifies the party; never send a partial address.
    data = {'Identification': p.get('eori'), 'Name': p.get('name'), 'Address': a}
    if role: data['Role'] = p.get('role', 'Y')  # MIG party identifier role: company
    if representative: data['Function'] = representative
    if p.get('additional_identifier'): data['AdditionalIdentifier'] = {'Identification': p['additional_identifier']}
    return data


def goods_location(decl):
    location = decl.get('goods_location')
    if location:
        data = {'Type': location.get('type'), 'Address': address(location)}
        if location.get('identification'):
            data['Identification'] = val(location['identification'], function=location.get('qualifier'))
        return data
    # A free-text legacy location is not an authorisation or structured address.
    return None


def item(it, decl, system):
    from services.tulli import package_code
    cn = str(it.get('cn_code') or '')
    hs = str(it.get('hs_code') or cn[:6])
    classifications = [{'Identification':hs,'IdentificationType':'HS'},
                       {'Identification':cn[-2:] if len(cn)==8 else cn, 'IdentificationType':'CN'}]
    taric = str(it.get('taric_code') or '')
    if system == 'import':
        if not taric and (it.get('taric') or {}).get('status') == 'ok':
            taric = (it.get('taric') or {}).get('goods_code') or ''
        if taric: classifications.append({'Identification':taric[-2:], 'IdentificationType':'TAR'})
        for c in it.get('taric_additional_codes') or []:
            classifications.append({'Identification': c, 'IdentificationType':'TAN'})
    commodity = {'Description': it.get('description'), 'Classification': classifications}
    if system == 'import':
        commodity['InvoiceLine'] = {'ItemCharge': val(it.get('invoice_amount'), CurrencyUnit=it.get('currency') or (decl.get('invoice') or {}).get('currency'))} if it.get('invoice_amount') is not None else None
        commodity['DutyTaxFee'] = {'DutyRegime':it.get('preference') or '100'}
    procedure = str(it.get('procedure_code') or decl.get('procedure_code') or ('4000' if system=='import' else '1000'))
    data = {'Sequence':it.get('seq'), 'StatisticalValue':amount(it.get('statistical_value_cents')),
            'Commodity':commodity, 'GoodsMeasure':{'GrossMass':it.get('gross_kg'), 'NetNetMass':it.get('net_kg')},
            'GovernmentProcedure':{'Current':procedure[:2], 'Previous':procedure[2:]},
            'Packaging':{'Type':package_code(it.get('package_type')), 'Quantity':int(it['packages']) if isinstance(it.get('packages'),(int,float)) and float(it['packages']).is_integer() else it.get('packages'), 'MarksNumbers':it.get('marks')},
            'Origin':{'Country':it['origin_country'], 'Type':'OC'} if it.get('origin_country') else None}
    extra = it.get('additional_procedure') or decl.get('additional_procedure') or ('999' if system=='import' else None)
    if extra: data['GovernmentProcedure']['AdditionalProcedure'] = {'Procedure':extra, **({'Sequence':1} if system=='export' else {})}
    if system == 'export': data['Packaging']['Sequence'] = 1
    else:
        data['CustomsValuation'] = {'Method':(decl.get('valuation') or {}).get('method') or '1'}
        costs = (it.get('freight_cents') or 0) + (it.get('insurance_cents') or 0)
        if costs:
            data['CustomsValuation']['ChargeDeduction'] = {'ChargesType':'AK', 'OtherChargeDeduction':val(amount(costs), CurrencyUnit='EUR')}

    docs = it.get('documents') or []
    if docs:
        data['AdditionalDocument'] = [dict(Type=d['code'], Identification=d.get('number') or d.get('reference'), **{'@':{'function':'AdditionalReference' if system=='import' and d['code'].startswith('Y') else 'SupportingDocument'}}) for d in docs]
        if system == 'export':
            for sequence, document in enumerate(data['AdditionalDocument'], 1): document['Sequence'] = sequence
    return data


def declaration(decl, system):
    tr = decl.get('transport') or {}
    inv = decl.get('invoice') or {}
    export = system == 'export'
    data = {'Identification':decl.get('message_id') or decl.get('lrn'),
            'FunctionalReference':val(decl.get('lrn'), function='LRN'),
            'Type':[val(decl.get('declaration_type') or ('EX' if export else 'IM'), function='DeclarationType'),
                    val(decl.get('additional_declaration_type') or 'A', function='AdditionalDeclarationType')],
            'Language':decl.get('language','fi'), 'Invoice':val(inv.get('total'), CurrencyUnit=inv.get('currency')),
            'Declarant':party(decl.get('declarant'), True),
            'Exporter':party(decl.get('exporter') or decl.get('seller'), export)}
    if decl.get('representative'):
        data['Agent'] = party(decl['representative'], True, '3' if decl.get('representation_type')=='indirect' else '2')
    if decl.get('submitter'):
        s = decl['submitter']; data['Submitter'] = {'Name':s.get('name'), 'Communication':{'Identification':s.get('email'), 'Type':'EM'}}
    consignment = {'Container':int(bool(tr.get('containerised'))), 'GoodsLocation':goods_location(decl)}
    gs = {'TransactionNature':decl.get('transaction_nature','11'),
          'Destination':{'Country':tr.get('destination_country')},
          'ExportCountry':{'Country':tr.get('dispatch_country')}, 'Consignment':consignment,
          'GovernmentAgencyGoodsItem':[item(i,decl,system) for i in decl.get('items') or []]}
    dt = decl.get('delivery_terms') or {}
    if dt.get('incoterm'):
        gs['TradeTerms'] = {'Condition':dt['incoterm'], 'LocationName':dt.get('place'), 'Country':dt.get('country')}
    if export:
        data['Security'] = decl.get('security')
        consignment.update(BorderTransportMode=tr.get('mode_at_border'), InlandTransportMode=tr.get('inland_mode'),
                           Consignee=party(decl.get('consignee') or decl.get('buyer')))
        data['ExitOffice'] = {'Identification':(decl.get('exit_office') or {}).get('code')}
        data['ExportOffice'] = {'Identification':(decl.get('office') or {}).get('code')}
    else:
        data['BorderTransportMeans'] = {'Mode':tr.get('mode_at_border'), 'RegistrationNationality':tr.get('nationality')} if tr.get('mode_at_border') else None
        gs['Importer'] = party(decl.get('importer'), True)
        if (decl.get('office') or {}).get('code'): data['PresentationOffice'] = {'Identification':decl['office']['code']}
        if tr.get('inland_mode') or tr.get('mode_at_border'):
            consignment['ArrivalTransportMeans'] = {'Mode':tr.get('inland_mode') or tr.get('mode_at_border'), 'Identification':tr.get('means_on_arrival'), 'IdentificationType':tr.get('identification_type')}
    td = tr.get('transport_document') or {}
    if td.get('number'):
        codes = {'bill_of_lading':'N705','sea_waybill':'N710','air_waybill':'N740','cmr':'N730','cim':'N720'}
        code = td.get('code') or codes.get(td.get('type'))
        if not code: raise ValueError('Transport document type/code is missing')
        consignment['TransportContractDocument'] = {'Type':code,'Identification':td['number']}
        if export: consignment['TransportContractDocument']['Sequence']=1
    if tr.get('container_numbers'):
        if export:
            # Seal counts must come from the shipment; an absent count is not zero.
            consignment['TransportEquipment'] = [{'Sequence':i,'Identification':c,
                'SealsAffixed':(tr.get('seal_counts') or {}).get(c)} for i,c in enumerate(tr['container_numbers'],1)]
        else: consignment['TransportEquipment'] = [{'Identification':c} for c in tr['container_numbers']]
    docs = [d for d in decl.get('documents') or [] if d.get('code') and d.get('number') and d.get('type') not in ('bill_of_lading','sea_waybill','air_waybill','cmr','cim')]
    if docs:
        data['AdditionalDocument'] = [{'Type':d['code'],'Identification':d.get('number') or d.get('reference'), **({'@':{'function':'SupportingDocument'}} if not export else {})} for d in docs]
        if export:
            for i,doc in enumerate(data['AdditionalDocument'],1): doc['Sequence']=i
    if not export and decl.get('vat_route')=='tilitys' and (decl.get('importer') or {}).get('vat_id'):
        gs['DomesticDutyTaxParty'] = {'Identification':decl['importer']['vat_id'],'Role':'FR1'}
    data['GoodsShipment'] = gs
    return data
