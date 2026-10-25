"""H1 import source mapping and pre-approval checks (Tulli import MIG 2.1.7).

No location is inferred from a port, mode, company address or presentation office.
NC0035/36/37 and NR0234 govern the supported domestic inspection locations.
"""
from copy import deepcopy
import re
from services.documents import field_value
from services.tulli_codelists import entry

NUMBER_FIELDS = {'commercial_invoice':'invoice_number', 'packing_list':'packing_list_number',
                 'bill_of_lading':'bl_number', 'sea_waybill':'bl_number', 'air_waybill':'awb_number',
                 'cmr':'cmr_number', 'cim':'consignment_number', 'certificate_of_origin':'certificate_number',
                 'eur1':'certificate_number'}


def document_number(doc):
    return (doc or {}).get('number') or field_value(doc, NUMBER_FIELDS.get((doc or {}).get('type'), 'number'))


def source_location(shipment):
    if shipment.get('goods_location'):
        return deepcopy(shipment['goods_location'])
    storage = shipment.get('temporary_storage') or {}
    # Warehouse identifier = operator EORI plus warehouse (3) and location (2).
    identifier = storage.get('warehouse_identifier')
    if not identifier and storage.get('eori') and re.fullmatch(r'\d{3}', str(storage.get('warehouse_code') or '')) and re.fullmatch(r'\d{2}', str(storage.get('location_code') or '')):
        identifier = str(storage['eori']) + str(storage['warehouse_code']) + str(storage['location_code'])
    if identifier:
        return {'type':'B', 'qualifier':'EO', 'identification':identifier, 'source':'shipment.temporary_storage'}
    inspection = shipment.get('inspection_location') or {}
    if inspection:
        return {**deepcopy(inspection), 'source':'shipment.inspection_location'}
    return None


def seller_address(inv, shipment):
    party = ((shipment.get('parties') or {}).get('shipper') or {})
    a = party.get('address') if isinstance(party.get('address'), dict) else {}
    return {'street':field_value(inv, 'seller_address') or a.get('street'),
            'city':field_value(inv, 'seller_city') or a.get('city'),
            'postal_code':field_value(inv, 'seller_postal_code') or a.get('postal_code'),
            'country':field_value(inv, 'seller_country') or a.get('country') or party.get('country')}


def location_patch(decl, shipment):
    loc = source_location(shipment)
    body = {}
    if not decl.get('goods_location') and loc:
        body['goods_location'] = loc
    return body


def validations(decl):
    from services.tulli import package_code
    out = []
    on = decl.get('declaration_date')
    def check(rule, ok, field, fi, en, owner='broker', item=None):
        out.append({'rule':rule, 'ok':bool(ok), 'severity':'blocking', 'owner':owner, 'item':item,
                    'field':field, 'message':en, 'message_i18n':{'fi':fi, 'en':en}})
    def code(list_id, value):
        return entry(list_id, value, on=on) is not None
    inv, tr = decl.get('invoice') or {}, decl.get('transport') or {}
    check('IMP-SCOPE', (decl.get('declaration_type') or 'IM') == 'IM'
          and (decl.get('additional_declaration_type') or 'A') == 'A'
          and all((it.get('procedure_code') or decl.get('procedure_code') or '4000') == '4000'
                  and (it.get('additional_procedure') or decl.get('additional_procedure') or '999') == '999'
                  for it in decl.get('items') or []), 'procedure_code',
          'Tämä automaattinen tuontipolku tukee IM/A-menettelyä 4000/999. Muu menettely vaatii välittäjän toteutuksen.',
          'This automatic import path supports IM/A procedure 4000/999; other procedures require a broker-supported workflow.')
    check('IMP-CONTAINERS', not tr.get('containerised') or bool(tr.get('container_numbers')), 'transport.container_numbers',
          'Kontitetun lähetyksen konttitunnukset puuttuvat.', 'Containerised shipment requires the actual container identifiers.')
    if decl.get('vat_route') == 'tilitys':
        check('IMP-VAT-ID', bool((decl.get('importer') or {}).get('vat_id')), 'importer.vat_id',
              'ALV-tilityksen verotunniste puuttuu.', 'Postponed import VAT requires the importer VAT identifier.', 'customer')
    sub = decl.get('submitter') or {}
    check('IMP-SUBMITTER', bool(sub.get('name') and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',str(sub.get('email') or ''))),
          'submitter', 'Ilmoituksen lähettäjän nimi ja sähköposti puuttuvat (MIG Submitter).',
          'Submitter name and email are required by the MIG.', 'agent')
    check('IMP-TRANSACTION', code('CL091', decl.get('transaction_nature')), 'transaction_nature',
          'Kauppatapahtuman luonne puuttuu tai ei ole CL091-koodi.', 'Nature of transaction is missing or not a CL091 code.', 'customer')
    check('IMP-BORDER-MODE', code('CL018', tr.get('mode_at_border')), 'transport.mode_at_border',
          'Rajakuljetusmuoto puuttuu tai ei ole CL018-koodi.', 'Border transport mode is missing or not a CL018 code.')
    check('IMP-ARRIVAL-MODE', code('CL018', tr.get('inland_mode') or tr.get('mode_at_border')), 'transport.inland_mode',
          'Saapuvan kuljetusvälineen kuljetusmuoto puuttuu.', 'Arrival transport mode is required (NC0208).')
    if tr.get('identification_type') is not None:
        check('IMP-TRANSPORT-ID-TYPE', code('CL750', tr['identification_type']), 'transport.identification_type',
              'Kuljetusvälineen tunnistamistapa ei ole CL750-koodi.', 'Transport identification type must be a CL750 code.')
    if str(tr.get('mode_at_border')) not in {'2','5','7'}:
        check('IMP-TRANSPORT-NATIONALITY', code('CL008', tr.get('nationality')), 'transport.nationality',
              'Rajakuljetusvälineen kansallisuus puuttuu (NC0031).', 'Border transport nationality is required (NC0031).')
    check('IMP-VALUATION-METHOD', code('CL604', (decl.get('valuation') or {}).get('method') or '1'), 'valuation.method',
          'Tullausarvon määritysmenetelmä ei ole CL604-koodi.', 'Valuation method must be a CL604 code.')
    check('IMP-INVOICE-CURRENCY', code('CL352', inv.get('currency')), 'invoice.currency',
          'Kauppalaskun valuutta puuttuu tai ei ole CL352-koodi.', 'Invoice currency is missing or not a CL352 code.', 'customer')
    check('IMP-INVOICE-TOTAL', isinstance(inv.get('total'), (int,float)) and inv['total'] > 0, 'invoice.total',
          'Kauppalaskun positiivinen kokonaisarvo puuttuu.', 'A positive invoice total is required.', 'customer')
    for k in ('dispatch_country', 'destination_country'):
        check('IMP-COUNTRY', code('CL008', tr.get(k)), 'transport.'+k,
              f'Maa puuttuu tai ei ole CL008-koodi: {k}.', f'Missing or invalid CL008 country: {k}.', 'customer')
    td = tr.get('transport_document') or {}
    from services.customs import DOC_CODES, TRANSPORT_DOC_TYPES
    check('IMP-TRANSPORT-CODE', code('CL754', td.get('code') or DOC_CODES.get(td.get('type'))),
          'transport.transport_document.type', 'Kuljetusasiakirjan tyyppi ei ole voimassa oleva CL754-koodi.',
          'Transport document type must be a current import CL754 code.')
    loc = decl.get('goods_location') or {}
    typ, qual, ident = loc.get('type'), loc.get('qualifier'), loc.get('identification')
    addr = loc.get('address') or {}
    check('IMP-LOCATION-TYPE', code('CL521', typ) and code('CL347', typ) and typ in {'B','C','D'}, 'goods_location.type',
          'Anna tuonnin tarkastuspaikan tyyppi B, C tai D (MIG DE 5/23). A on vain tullivarastointiin.',
          'Supply import inspection location type B, C or D (MIG DE 5/23). A is reserved for customs warehousing.')
    valid_addr = isinstance(addr, dict) and all(addr.get(k) for k in ('street','city','postal_code','country')) and addr.get('country') == 'FI'
    valid_id = bool(ident) and ((typ in {'B','C'} and qual == 'EO') or (typ == 'C' and qual == 'Authorisation'))
    # For B, the identifier must include the warehouse and location suffix, not just a naked EORI.
    if typ == 'B' and qual == 'EO':
        valid_id = bool(re.fullmatch(r'[A-Z]{2}[A-Z0-9-]+[0-9]{5}', str(ident or '')))
    valid_location = ((typ in {'C','D'} and valid_addr and not ident) or (valid_id and not addr))
    check('IMP-LOCATION-IDENTITY', valid_location, 'goods_location',
          'Tarkastuspaikan täydellinen FI-osoite tai MIG NC0035/NC0036:n mukainen tunniste puuttuu; B: EORI+3+2.',
          'Supply the actual inspection place: complete FI address or identifier under MIG NC0035/NC0036; B requires EORI+3+2.')
    for role in ('importer', 'declarant', 'representative', 'exporter'):
        p = decl.get(role) or (decl.get('seller') if role == 'exporter' else {}) or {}
        check('IMP-PARTY', bool(p.get('eori') or (role == 'exporter' and p.get('name') and isinstance(p.get('address'),dict)
              and all(p['address'].get(k) for k in ('street','city','postal_code','country')))), role,
              f'Osapuolen {role} EORI tai nimi ja täydellinen osoite puuttuu.',
              f'{role}: provide EORI or name and complete structured address.', 'customer' if role in {'importer','exporter'} else 'broker')
    for it in decl.get('items') or []:
        n=it.get('seq')
        check('IMP-SHIPPING-MARKS', bool(it.get('marks')), 'marks',
              'Pakkausten merkit ja numerot puuttuvat (NC0209).', 'Shipping marks and numbers are required (NC0209).', 'customer', n)
        check('IMP-LINE-AMOUNT', isinstance(it.get('invoice_amount'),(int,float)) and it['invoice_amount'] > 0,
              'invoice_amount', 'Laskurivin arvo puuttuu (NC0226).', 'Invoice line amount is required (NC0226).', 'customer', n)
        tc = it.get('taric_code') or (it.get('taric') or {}).get('goods_code')
        check('IMP-TARIC-CODE', bool(re.fullmatch(r'\d{10}',str(tc or ''))), 'taric_code',
              'TARIC-koodin 10 numeroa vaaditaan (NC0230).', 'A ten-digit TARIC code is required (NC0230).', item=n)
        check('IMP-PACKAGE-CODE', code('CL017', package_code(it.get('package_type'))), 'package_type',
              'Pakkauslaji puuttuu tai ei ole CL017-koodi.', 'Package type is missing or not a CL017 code.', 'customer', n)
        qty=it.get('packages')
        count_ok = qty is None if code('CL181', package_code(it.get('package_type'))) else isinstance(qty,(int,float)) and qty >= 0 and int(qty)==qty
        check('IMP-PACKAGE-COUNT', count_ok, 'packages',
              'Kollimäärän tulee olla kokonaisluku; CL181-irtotavaralle määrää ei ilmoiteta (NC0010).', 'Package count must be an explicit non-negative integer; omit it for CL181 bulk goods (NC0010).', 'customer',n)
        check('IMP-ORIGIN-COUNTRY', code('CL008', it.get('origin_country')), 'origin_country',
              'Alkuperämaa ei ole voimassa oleva CL008-koodi.', 'Origin country must be a current CL008 code.', 'customer', n)
        check('IMP-NET-MASS', isinstance(it.get('net_kg'),(int,float)) and it['net_kg'] > 0, 'net_kg',
              'Nettopaino puuttuu.', 'Net mass is required.', 'customer', n)
        check('IMP-PREFERENCE', code('CL603', it.get('preference')), 'preference',
              'Etuuskohtelu ei ole voimassa oleva CL603-koodi.', 'Preference must be a current CL603 code.', item=n)
        procedure = it.get('procedure_code') or decl.get('procedure_code') or '4000'
        additional = it.get('additional_procedure') or decl.get('additional_procedure') or '999'
        check('IMP-PROCEDURE', code('NCL232', procedure+additional), 'procedure_code',
              'Menettelyjen yhdistelmä ei ole sallittu (NCL232).', 'Procedure/additional procedure combination is not permitted (NCL232).',item=n)
    docs = [(d,None) for d in decl.get('documents') or [] if d.get('type') not in TRANSPORT_DOC_TYPES]
    docs += [(d,it.get('seq')) for it in decl.get('items') or [] for d in it.get('documents') or []]
    check('IMP-INVOICE-REFERENCE', any(d.get('code') == 'N380' and (d.get('number') or d.get('reference')) for d in decl.get('documents') or []),
          'documents', 'Kauppalaskun N380-viite puuttuu (DE 2/3).', 'Commercial invoice reference N380 is required in DE 2/3.', 'customer')
    for d,n in docs:
        check('IMP-DOCUMENT-CODE', code('CL380' if n is not None and str(d.get('code') or '').startswith('Y') else 'CL213', d.get('code')), 'documents.code',
              f'Liiteasiakirjan koodi ei kelpaa: {d.get("code")}.', f'Invalid import CL213/CL380 document code: {d.get("code")}.',item=n)
        check('IMP-DOCUMENT-REFERENCE', bool(d.get('number') or d.get('reference')), 'documents.reference',
              'Liiteasiakirjan viite puuttuu.', 'Supporting document reference is missing.', 'customer', n)
    # Validate the exact outbound projection before approval, including lengths, numeric facets,
    # attributes and optional groups whose children become mandatory when the group is present.
    from services.customs_messages.tulli_common import declaration, serialize, schema
    try:
        xml = serialize('import','FI415B',declaration(decl,'import'), validate_output=False)
        for err in schema('import','FI415B').iter_errors(xml):
            check('IMP-XSD', False, err.path, 'FI415B-tietovaatimus ei täyty: '+str(err.path),
                  f'FI415B {err.path}: {err.reason}')
    except (ValueError, TypeError, KeyError) as exc:
        check('IMP-MESSAGE-DATA',False,'declaration','FI415B:n lähtötieto puuttuu tai on virheellinen.',f'Invalid FI415B source data: {exc}')
    return out
