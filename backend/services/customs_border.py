"""Goods formalities from structured territory codes, never from customer prose.

Sources checked 2026-10-25:
https://taxation-customs.ec.europa.eu/taxation/vat/vat-directive/how-does-vat-work/territorial-scope_en
https://www.revenue.ie/en/customs/businesses/info-movements-ni/info-businesses-trading-ni/protocol-ireland-ni.aspx
https://sede.agenciatributaria.gob.es/Sede/en_gb/aduanas/novedades/2026/marzo/3/aspectos-aduaneros-acuerdo-ue-reino-gibraltar.html
NI remains legally UK territory; EU goods rules apply under Windsor (not all UK).
Gibraltar's 2026 agreement retains formalities in its transitional third-territory
regime. Do not turn customs-union tariff treatment into a declaration exemption.
Åland/Canaries are inside the customs area but outside VAT/excise territory.
"""
from functools import lru_cache
import json
from pathlib import Path

EU = frozenset('AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE'.split())
SOURCE = 'https://taxation-customs.ec.europa.eu/taxation/vat/vat-directive/how-does-vat-work/territorial-scope_en'
HANDLINGS = frozenset(('us', 'customer_broker', 'unknown'))
POSTPONED_EN = 'Customs formalities required. Tulli filing is postponed: we are not registered as a customs representative. A person must arrange a broker or confirm that the customer handles customs.'
POSTPONED_FI = 'Tullimuodollisuudet vaaditaan. Tulli-ilmoituksen lähetys on lykätty: emme ole rekisteröityneet tulliedustajaksi. Henkilön on järjestettävä tulliasioitsija tai vahvistettava, että asiakas hoitaa tullauksen.'
CUSTOMER_EN = 'The customer arranges export and import formalities through their own broker.'
CUSTOMER_FI = 'Asiakas järjestää vienti- ja tuontimuodollisuudet oman tulliasioitsijansa kautta.'


@lru_cache
def _countries():
    return set(json.loads((Path(__file__).resolve().parents[1] / 'data/countries_i18n.json').read_text())['countries'])


# Exact territorial designations are structured catalogue keys, not inferred names.
# Customs/VAT/excise membership transcribed from SOURCE; GI keeps transitional formalities.
SPECIAL = {
    **{c: ('EU', c, c, True) for c in ('AX', 'IC', 'GP', 'GF', 'MQ', 'RE', 'YT', 'MF')},
    **{c: (c, c, c, False) for c in ('FO', 'GL', 'PF', 'NC', 'PM', 'WF', 'TF', 'BL', 'AN', 'AW', 'BQ', 'CW', 'SX',
                                   'DE-HGL', 'DE-BUS', 'IT-LIVIGNO', 'ES-CE', 'ES-ML', 'AD', 'VA')},
    **{c: ('EU', c, 'EU', True) for c in ('GR-69', 'IT-CAMPIONE', 'IT-LUGANO-WATERS', 'SM')},
    **{c: ('EU', 'EU', 'EU', True) for c in ('XI', 'MC', 'CY-SBA', 'PT-20', 'PT-30')},
    'GI': ('GI', 'GI', 'GI', True),
}
PARENT = {'AX':'FI','IC':'ES','DE-HGL':'DE','DE-BUS':'DE','IT-LIVIGNO':'IT','IT-CAMPIONE':'IT',
          'IT-LUGANO-WATERS':'IT','GR-69':'GR','ES-CE':'ES','ES-ML':'ES','XI':'GB','CY-SBA':'GB',
          'PT-20':'PT','PT-30':'PT', **{c:'FR' for c in ('GP','GF','MQ','RE','YT','MF','PF','NC','PM','WF','TF','BL')},
          'FO':'DK','GL':'DK', **{c:'NL' for c in ('AN','AW','BQ','CW','SX')}}
# Most specific known UN/LOCODE wins over broad provinces (e.g. Heligoland shares SH).
EXACT = {'DEHGL':'DE-HGL', 'FIMHQ':'AX', 'ESCEU':'ES-CE', 'ESMLN':'ES-ML',
         'CYAKT':'CY-SBA', 'CYDHK':'CY-SBA'}
NI_SUBDIVISIONS = frozenset(('NIR', 'ABC', 'AND', 'ANN', 'BFS', 'CCG', 'DRS', 'FER', 'LBC', 'MEA', 'MUL', 'NMD'))
EXCEPTION_COUNTRIES = frozenset(('FI','DE','IT','ES','FR','GR','DK','NL','GB'))
# A province alone cannot distinguish the small excluded enclave/island/waters within it.
AMBIGUOUS_SUBDIVISIONS = {'DE': {'SH','BW'}, 'IT': {'SO','CO','25'}, 'GR': {'B'}}
UNVERIFIED_EN = 'Territory unverified. A person must verify the structured origin and destination territories before customs requirements can be determined.'
UNVERIFIED_FI = 'Tullialue on vahvistamatta. Henkilön on vahvistettava lähtö- ja määräpaikan rakenteiset aluetiedot ennen tullivaatimusten määrittämistä.'


def _territory(code, areas=None, *, verified=True, resolution='country'):
    customs, vat, excise, rules = areas or (None, None, None, None)
    return {'code':code, 'customs_area':customs, 'vat_area':vat, 'excise_area':excise,
            'eu_customs_rules':rules, 'verified':verified, 'resolution':resolution}


@lru_cache(maxsize=1)
def _subdivisions():
    from services import geo
    out = {}
    for d in geo._dataset()['by_code'].values():
        if d.get('subdivision'):
            out.setdefault(d['country'], set()).add(str(d['subdivision']).upper())
    return out


def territory(place):
    """Resolve official code -> subdivision -> country; missing regional evidence never guesses mainland."""
    from services import geo
    p = place if isinstance(place, dict) else {'unlocode':place} if isinstance(place,str) else {}
    locode = str(p.get('unlocode') or p.get('code') or '').upper()
    known = geo.lookup_code(locode) if locode else None
    cc = str((known or {}).get('country') or p.get('country') or '').upper()
    cc = 'GB' if cc == 'UK' else cc
    exact = EXACT.get(locode) or EXACT.get(str((known or {}).get('unlocode') or '').upper())
    if exact:
        return _territory(exact, SPECIAL[exact], resolution='unlocode')
    sub = str((known or {}).get('subdivision') or p.get('subdivision') or '').upper()
    if sub.startswith(cc+'-'):
        sub = sub[len(cc)+1:]
    explicit = str(p.get('territory_code') or '').upper()
    # An explicit catalogue designation resolves small areas with no independent ISO subdivision.
    if explicit in SPECIAL and (explicit == cc or PARENT.get(explicit) == cc or explicit == 'CY-SBA' and cc == 'CY'):
        return _territory(explicit, SPECIAL[explicit], resolution='territory_code')
    if cc in SPECIAL:
        return _territory(cc, SPECIAL[cc])
    mapped = None
    if cc == 'FI' and sub == '01': mapped = 'AX'
    elif cc == 'ES':
        mapped = {'CN':'IC','GC':'IC','TF':'IC','CE':'ES-CE','ML':'ES-ML'}.get(sub)
    elif cc == 'GB' and sub in NI_SUBDIVISIONS: mapped = 'XI'
    elif cc == 'GR' and sub == '69': mapped = 'GR-69'
    elif cc == 'PT' and sub in ('20','30'): mapped = 'PT-'+sub
    elif cc == 'FR':
        mapped = {'971':'GP','972':'MQ','973':'GF','974':'RE','976':'YT','977':'BL','978':'MF',
                  '984':'TF','986':'WF','987':'PF','988':'NC','975':'PM'}.get(sub)
    elif cc == 'DK' and sub in ('FO','GL'): mapped = sub
    if mapped:
        return _territory(mapped, SPECIAL[mapped], resolution='unlocode' if known else 'subdivision')
    if cc in EXCEPTION_COUNTRIES:
        # Unknown/invalid subdivision is not proof of ordinary mainland. A recognised point can
        # resolve a province containing an enclave; a broad province with no point cannot.
        if sub not in _subdivisions().get(cc,set()) or (not known and sub in AMBIGUOUS_SUBDIVISIONS.get(cc,set())):
            return _territory(cc, verified=False, resolution='unverified')
    if cc in EU:
        return _territory(cc, ('EU','EU','EU',True), resolution='unlocode' if known else 'subdivision' if sub else 'country')
    if cc in _countries():
        return _territory(cc, (cc,cc,cc,False), resolution='unlocode' if known else 'country')
    return _territory(cc, verified=False, resolution='unverified')


def assess(origin, destination):
    o, d = territory(origin), territory(destination)
    unknown = not o['verified'] or not d['verified']
    border = None if unknown else o['customs_area'] != d['customs_area']
    tax = None if unknown else not border and (o['vat_area'] != d['vat_area'] or o['excise_area'] != d['excise_area'])
    direction = ('unknown' if unknown else 'export' if o['customs_area'] == 'EU' and border else
                 'import' if d['customs_area'] == 'EU' and border else 'third_country' if border else
                 'tax_border' if tax else 'none')
    return {'customs_required': None if unknown else border or tax, 'customs_border_crossing': border,
            'customs_territory_verified': not unknown, 'vat_excise_formalities': tax, 'customs_direction': direction,
            'origin_territory': o, 'destination_territory': d, 'source': SOURCE}


def handling(request):
    value = request.get('customs_handling')
    if value in HANDLINGS:
        return value
    # Backwards-compatible explicit form request; a false checkbox never establishes a customer's broker.
    return 'us' if request.get('customs_clearance') is True else 'unknown'


def required_review(request):
    if not request.get('origin') or not request.get('destination'):
        return bool(request.get('customs_required')) and handling(request) != 'customer_broker'
    required = assess(request.get('origin'), request.get('destination'))['customs_required']
    return required is None or required and handling(request) != 'customer_broker'


def apply(request):
    result = assess(request.get('origin'), request.get('destination'))
    request.update({k: result[k] for k in ('customs_required', 'customs_territory_verified', 'customs_border_crossing', 'vat_excise_formalities', 'customs_direction')})
    request['customs_border'] = result
    request['customs_handling'] = handling(request)
    # Preserve the unknown state; this field describes our service, not the border requirement.
    request['customs_clearance'] = True if request['customs_handling'] == 'us' else False if request['customs_handling'] == 'customer_broker' else None
    if result['customs_required'] is None:
        request['customs_notice'] = {'en':UNVERIFIED_EN,'fi':UNVERIFIED_FI}
    elif result['customs_required']:
        customer = request['customs_handling'] == 'customer_broker'
        request['customs_notice'] = {'en': CUSTOMER_EN if customer else POSTPONED_EN,
                                    'fi': CUSTOMER_FI if customer else POSTPONED_FI}
    else:
        request.pop('customs_notice', None)
    return request


def postponed_row(request):
    if request.get('customs_required') is False:
        return None
    if request.get('customs_required') is None:
        return {'code':'TERRITORY_UNVERIFIED','description':'Territory unverified','description_fi':'Tullialue on vahvistamatta',
                'amount_cents':None,'status':'territory_unverified','reason_code':'customs.territory_unverified',
                'reason':UNVERIFIED_EN,'reason_fi':UNVERIFIED_FI}
    customer = request['customs_handling'] == 'customer_broker'
    code = 'EXPORT_CUSTOMS' if request['customs_direction'] == 'export' else 'CUSTOMS'
    if request.get('vat_excise_formalities'):
        code = 'TAX_BORDER_FORMALITIES'
    return {'code': code, 'description': 'Customs formalities' if code != 'TAX_BORDER_FORMALITIES' else 'VAT and excise border formalities',
            'description_fi': 'Tullimuodollisuudet' if code != 'TAX_BORDER_FORMALITIES' else 'Verorajamuodollisuudet (ALV ja valmistevero)',
            'amount_cents': None, 'status': 'customer_arranged' if customer else 'postponed',
            'reason_code': 'customs.customer_broker' if customer else 'customs.postponed',
            'reason': request['customs_notice']['en'], 'reason_fi': request['customs_notice']['fi']}


def human_approved(quote):
    # Reuse the quote-send gate's exact-request authority, including prices/revision.
    from services.agents.quote_agent import human_authorized
    return human_authorized(quote)


def quote_needs_review(quote):
    request = quote.get('request') or {}
    return required_review(request) or (bool(quote.get('customs_review_changed'))
        and assess(request.get('origin'), request.get('destination'))['customs_required'] is not False)


def review_reason(quote):
    request = quote.get('request') or {}
    if request.get('origin') and request.get('destination') and assess(request['origin'],request['destination'])['customs_required'] is None:
        return 'territory_unverified'
    return 'customs_required' if quote_needs_review(quote) else None
