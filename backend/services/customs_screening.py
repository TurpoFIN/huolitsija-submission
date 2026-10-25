"""Declaration-wide sanctions checks and goods controls backed by official sources."""
from __future__ import annotations
import hashlib
import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from core.db import db
from core.jobs import job, schedule
from core.util import NO_ID, now, now_iso, parse_iso
from services import screening, taric, dual_use

COLLECTIONS = {'import': 'customs_declarations', 'export': 'export_declarations',
               'transit': 'transit_declarations', 'ens': 'ens_filings'}
ROLES = set('seller buyer importer exporter declarant representative carrier consignor consignee shipper '
            'notify_party notify_parties freight_forwarder holder holder_of_transit_procedure principal '
            'guarantor end_user manufacturer supplier purchaser additional_supply_chain_actors '
            'supply_chain_actors transporter vessel'.split())
SKIP = {'screening', 'validations', 'history', 'edits', 'submission', 'messages', 'controls'}
FINAL = {
    'import': ['released', 'closed', 'invalidated', 'cancelled'],
    'export': ['exited', 'closed', 'invalidated', 'cancelled'],
    'transit': ['closed', 'invalidated', 'cancelled'],
    'ens': ['arrived', 'closed', 'invalidated', 'cancelled'],
}


def parties(decl):
    """Walk party roles at every level, including house consignments and goods items.

    Preserve paths, even for the same name in several roles. Unnamed party objects
    remain errors, never an implicit clearance. Skip prior screening/audit outputs.
    """
    out = []
    def add(value, path):
        if isinstance(value, list):
            for i, p in enumerate(value):
                add(p, f'{path}[{i}]')
        elif isinstance(value, dict):
            if 'name' in value or any(k in value for k in ('eori', 'id', 'address', 'country', 'legal_name')) or not value:
                out.append({'role': path, 'name': value.get('name') or value.get('legal_name'),
                            'kind': value.get('kind') or ('vessel' if path.endswith('vessel') else 'any'),
                            'country': value.get('country'), 'identity': value})
            else:
                for k, v in value.items():
                    add(v, f'{path}.{k}')
        elif isinstance(value, str):
            out.append({'role': path, 'name': value, 'kind': 'any', 'identity': value})
        else:
            out.append({'role': path, 'name': None, 'kind': 'any', 'identity': value})
    def walk(value, path=''):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in SKIP:
                    continue
                p = f'{path}.{k}' if path else k
                if k in ROLES or k == 'parties':
                    add(v, p)
                elif k.endswith('_name') and k[:-5] in ROLES:
                    add(v, p)
                else:
                    walk(v, p)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f'{path}[{i}]')
    walk(decl)
    return out


def party_fingerprint(decl):
    return hashlib.sha256(json.dumps(parties(decl), sort_keys=True, default=str).encode()).hexdigest()


def screening_current(decl):
    s = decl.get('screening') or {}
    at = parse_iso(s.get('screened_at'))
    return bool(s.get('complete') and s.get('status') == 'clear' and at and
                0 <= (now() - at).total_seconds() < 86400 and s.get('party_fingerprint') == party_fingerprint(decl))


def _fresh(m):
    return bool(m.get('loaded') and m.get('status') == 'ok' and m.get('fetched_at') and
                0 <= (now() - parse_iso(m['fetched_at'])).total_seconds() < 86400)


async def required_lists():
    """Lists a filing cannot do without (settings.customs_screening_required_lists).

    Default EU + UN: the restrictive measures binding on a Finnish operator (UN designations apply through EU law).
    OFAC, UK and the US CSL are screened whenever loaded, and a hit on any list blocks; a missing one is named in the
    result and the daily re-screen picks it up once it loads."""
    from core.settings import get_settings
    keys = (await get_settings()).get('customs_screening_required_lists') or ['eu', 'un']
    return [k for k in keys if k in screening.LIST_SOURCES] or ['eu', 'un']


async def screen_parties(decl, *, kind='import'):
    if kind not in COLLECTIONS:
        raise ValueError('Invalid declaration kind')
    # Every invocation refreshes only lists older than 24h; never approve a screen without fresh required lists.
    metas = await screening.refresh_lists()
    required = await required_lists()
    by_list = {m['list']: m for m in metas}
    complete = all(_fresh(by_list.get(k, {})) for k in required)
    missing = [m['list'] for m in metas if not _fresh(m)]
    rows, hits = [], []
    for p in parties(decl):
        try:
            r = await screening.screen_name(p.get('name'), kind=p['kind'], country=p.get('country'))
        except Exception as exc:
            r = {'status': 'error', 'hits': [], 'error': str(exc)[:200], 'complete': False}
        if r['status'] == 'clear' and not complete:
            r['status'] = 'error'
        if 'complete' in r:
            r['complete'] = complete and r['status'] != 'error'
        row = {**r, 'role': p['role'], 'name': p.get('name')}
        rows.append(row)
        hits += [{**h, 'role': p['role'], 'name': p.get('name')} for h in r.get('hits', [])]
    status = ('potential_match' if hits else 'error' if not rows or not complete or
              any(r['status'] != 'clear' for r in rows) else 'clear')
    result = {'status': status, 'parties': rows, 'hits': hits, 'screened_at': now_iso(),
              'name': (decl.get('seller') or {}).get('name'), 'complete': complete and bool(rows) and
              all(r.get('complete', False) for r in rows), 'party_fingerprint': party_fingerprint(decl),
              'lists': [{k: m.get(k) for k in ('list', 'version', 'sha256', 'fetched_at', 'status')} for m in metas],
              'required_lists': required, 'missing_lists': missing,
              'note': {'fi': 'Tarkistettu ilman listoja: ' + ', '.join(m.upper() for m in missing) +
                             ' (ei ladattavissa); tarkistetaan uudelleen, kun lista on saatavilla.',
                       'en': 'Screened without ' + ', '.join(m.upper() for m in missing) +
                             ' (list not available); re-screened when it loads.'} if missing else None,
              'legal_point': 'sanctions_potential_match' if hits else None}
    if hits:
        from core import agent_runtime
        decision = await agent_runtime.decide('customs_agent', 'screen_parties',
            subject=(COLLECTIONS[kind], decl.get('id') or result['party_fingerprint']),
            company_id=decl.get('company_id'), inputs={'kind': kind, 'parties': rows},
            decision={'status': status}, legal_point='sanctions_potential_match',
            approval_kind='document_review', title='Customs sanctions potential match',
            title_fi='Tulli-ilmoituksen mahdollinen pakoteosuma',
            summary='Every potential match requires a compliance decision.',
            summary_fi='Jokainen mahdollinen osuma vaatii vaatimustenmukaisuuden tarkistuksen.',
            approval_data={'declaration_id': decl.get('id'), 'kind': kind, 'screening': result})
        result['decision_id'] = decision.id
    return result


@lru_cache(maxsize=6)
def _goods_data(country):
    return json.loads((Path(__file__).resolve().parents[1] / 'data' / 'sanctions_goods' / (country.lower() + '.json')).read_text())


async def goods_sanctions(item, *, flow, country, on=None):
    if flow not in ('import', 'export', 'transit', 'ens'):
        raise ValueError('Invalid flow')
    country = (country or '').upper()
    cn = re.sub(r'\D', '', str(item.get('cn_code') or item.get('taric_code') or item.get('hs_code') or ''))
    on = str(on or date.today())[:10]
    reasons, measures, matches = [], [], []
    chpi = [r for r in _goods_data('chpi')['items'] if cn.startswith(r['hs6'])]
    restricted = country in ('RU', 'BY', 'IR', 'KP', 'SY')
    available = False
    if restricted and cn:
        try:
            t = await taric.lookup(cn, country=country, flow='import' if flow in ('import', 'ens') else 'export', on=on)
            available = t.get('known_code', False) and t.get('declarable', False)
            # Keep country-specific and sanctions-labelled restrictions, with certificates/conditions.
            measures = [m for m in t.get('restrictions', []) if m.get('geo_code') == country or
                        any(w in m.get('type', '').lower() for w in ('sanction', 'restriction', 'prohibition'))]
        except (taric.TaricUnavailable, ValueError):
            pass
    if restricted:
        src = _goods_data(country)
        for annex, a in src['annexes'].items():
            for row in a['items']:
                if cn and (cn.startswith(row['cn']) or row['cn'].startswith(cn)) and flow in a['flows']:
                    matches.append({**row, 'annex': annex, 'basis': a['basis'], 'source': src['source'],
                                    'status': 'scope_requires_review'})
        if not available:
            reasons.append(dual_use.reason('taric_unavailable', 'Voimassa olevia TARIC-pakotteita ei voitu varmistaa.',
                                            'Current TARIC sanctions could not be verified.'))
    prohibition = any(m.get('kind') == 'prohibition' and not m.get('conditions') for m in measures)
    # RU/BY is a standing legal human point, even where no commodity prohibition is found.
    blocked = country in ('RU', 'BY') or prohibition
    review = not country or not cn or (restricted and (bool(matches or measures) or not available or on != _goods_data(country)['source']['valid_on']))
    if not country or not cn:
        reasons.append(dual_use.reason('missing_goods_context', 'Nimike ja alkuperä- tai kohdemaa vaaditaan.',
                                        'Commodity code and origin/destination country are required.'))
    if blocked:
        reasons.append(dual_use.reason('ru_by_trade' if country in ('RU', 'BY') else 'goods_prohibition',
                                        'Kauppa on pysäytetty pakotetarkistusta varten.', 'Trade is blocked for sanctions review.'))
    if review and not blocked:
        reasons.append(dual_use.reason('goods_scope_review', 'Pakotteen soveltamisala ja poikkeukset on tarkistettava.',
                                        'Sanction scope and exceptions require review.'))
    if chpi:
        reasons.append(dual_use.reason('common_high_priority', 'Yhteisen korkean prioriteetin tuote; kiertämisriski on arvioitava.',
                                        'Common high priority item; assess circumvention risk.'))
    return {'blocked': blocked, 'prohibited': prohibition, 'requires_review': bool(review or blocked),
            'legal_point': 'ru_by_trade' if country in ('RU', 'BY') else None,
            'matches': matches, 'taric_measures': measures, 'taric_verified': available,
            'common_high_priority': chpi, 'reasons': reasons, 'country': country, 'flow': flow, 'date': on}


async def validation_rows(decl, shipment=None):
    """Import validation hook; export/transit/ENS lanes may call this same hook."""
    shipment = shipment or {}
    flow = decl.get('type') if decl.get('type') in COLLECTIONS else 'import'
    rows = []
    def row(rule, ok, fi, en, **extra):
        rows.append({'rule': rule, 'ok': bool(ok), 'severity': 'blocking', 'owner': 'broker',
                     'message': en, 'message_i18n': {'fi': fi, 'en': en}, **extra})
    valid_screen = screening_current(decl)
    if valid_screen:
        current_lists = await screening.lists_status()
        required = await required_lists()
        recorded = {m['list']: m.get('version') for m in decl['screening'].get('lists', [])}
        # Required lists must be loaded and screened at their current version; any other list that is loaded now
        # must have been screened at its current version too (a list that comes back forces a re-screen).
        valid_screen = all((m.get('loaded') and m.get('status') == 'ok' and recorded.get(m['list']) == m.get('version'))
                           if m['list'] in required else
                           (not (m.get('loaded') and m.get('status') == 'ok') or recorded.get(m['list']) == m.get('version'))
                           for m in current_lists)
    row('K-1', valid_screen, 'Kaikkien osapuolten ajantasainen pakotetarkistus vaaditaan.',
        'A current, complete sanctions clearance is required for every party.', field='screening')
    destination = decl.get('destination_country') or (shipment.get('destination') or {}).get('country')
    for item in decl.get('items') or []:
        country = (item.get('origin_country') or (shipment.get('origin') or {}).get('country')) if flow in ('import', 'ens') else destination
        du = await dual_use.assess(item, flow=flow, destination=destination)
        goods = await goods_sanctions(item, flow=flow, country=country)
        item['controls'] = {'dual_use': du, 'goods_sanctions': goods}
        row('DU-1', not du['requires_review'], 'Kaksikäyttötuotteiden valvonta ja lupa on tarkistettava.',
            'Dual-use classification and authorisation must be reviewed.' if du['requires_review'] else 'No export/transfer licence requirement for this flow.', item=item.get('seq'), legal_point=du['legal_point'])
        row('SANC-GOODS', not goods['requires_review'], 'Tavaran pakoterajoitukset on tarkistettava.',
            'Goods sanctions require review.' if goods['requires_review'] else 'No country goods sanction hold identified.', item=item.get('seq'), legal_point=goods['legal_point'])
    return rows


@job('customs.rescreen_parties', agent=True, timeout=1800,
     label_fi='Tulli-ilmoitusten pakotetarkistus', label_en='Rescreen customs parties')
async def rescreen_parties(j):
    counts = {'screened': 0, 'potential_match': 0, 'error': 0}
    for kind, collection in COLLECTIONS.items():
        async for decl in db[collection].find({'status': {'$nin': FINAL[kind]}}, NO_ID):
            result = await screen_parties(decl, kind=kind)
            await db[collection].update_one({'id': decl['id']}, {'$set': {'screening': result}})
            counts['screened'] += 1
            if result['status'] in counts:
                counts[result['status']] += 1
    return counts

schedule('customs.rescreen_parties.daily', job='customs.rescreen_parties', daily_at='04:30',
         label_fi='Tulli-ilmoitusten pakotetarkistus', label_en='Rescreen customs parties')()
