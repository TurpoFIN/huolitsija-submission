"""Official Annex I/II/IV reference and conservative, explainable export assessment.

CN correlation is a candidate search, never a technical classification. General
licences below are possible routes, not a grant or verification of their conditions.
"""
from __future__ import annotations
import json
import re
from functools import lru_cache
from pathlib import Path
from datetime import date
from services import taric

DATA = Path(__file__).resolve().parents[1] / 'data' / 'dualuse'
EU = set('AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE'.split())

@lru_cache(maxsize=8)
def data(name):
    return json.loads((DATA / (name + '.json')).read_text())

@lru_cache(maxsize=1)
def _entries():
    return {e['code']: e for e in data('annex1')['entries']}

def _code(code):
    s = re.sub(r'\s+', '', str(code)).rstrip('.')
    return s[:5].upper() + s[5:].lower()

def entry(code):
    e = _entries().get(_code(code))
    if not e:
        return None
    return {**e, 'descendants': [n for c, n in _entries().items() if c.startswith(e['code'] + '.')],
            'source': data('annex1')['source']}

def search(q, limit=50):
    q = q.strip().casefold()
    if not q:
        return []
    return [e for e in _entries().values() if q in e['code'].casefold() or q in
            json.dumps([e['text'], e['notes'], e['technical_notes']], ensure_ascii=False).casefold()][:limit]

def candidates_for_cn(cn):
    cn = re.sub(r'\D', '', str(cn))
    if len(cn) not in (4, 6, 8, 10):
        return []
    found = {}
    for tariff, rows in data('cn_correlation')['cn'].items():
        if tariff.startswith(cn):
            for row in rows:
                e = _entries().get(row['code'])
                if e:
                    found[e['code']] = {'entry': e['code'], 'text': e['text'], 'footnote': row['footnote'],
                                        'source': 'Commission CN–dual-use correlation table'}
    return list(found.values())

def reason(code, fi, en):
    return {'code': code, 'fi': fi, 'en': en}

def _overlap(code, codes):
    return any(code == c or code.startswith(c + '.') or c.startswith(code + '.') for c in codes)

def _authorisations(code, destination):
    a = data('annex2_eugea')
    embargo = data('embargo_destinations')['countries'].get(destination, {}).get('measures', [])
    excluded = _overlap(code, a['section_i']['codes'] + [x['code'] for x in data('annex4')['items']])
    options = []
    for key, auth in a['eugea'].items():
        dest = auth['destinations']
        eligible = destination in dest.get('countries', [])
        if dest['mode'] == 'all_except':
            eligible = destination not in (dest['excluded'] + a['eugea']['EU001']['destinations']['countries']) and not embargo
        if not eligible:
            continue
        if auth['items_mode'] == 'all_except_section_i':
            covered = not excluded and not _overlap(code, auth['items'])
        elif key == 'EU007':
            # EU007's extracted codes are exclusions, not an allow-list; software/technology only.
            covered = code[1:2] in ('D', 'E') and not excluded and not _overlap(code, auth['items'])
        else:
            covered = _overlap(code, auth['items'])
        if covered:
            options.append({'type': key, 'basis': 'Reg (EU) 2021/821 Annex II ' + key,
                            'status': 'conditions_require_review', 'conditions': auth['conditions_text'],
                            'items_scope': auth['items_text']})
    return options

async def assess(item, *, flow, destination, on=None):
    if flow not in ('import', 'export', 'transit', 'ens', 'transfer'):
        raise ValueError('Invalid flow')
    destination = (destination or '').upper()
    on = str(on or date.today())[:10]
    source = data('annex1')['source']
    reasons = []
    code = _code(item.get('dual_use_code') or item.get('control_code') or '')
    exact = entry(code) if code else None
    cn = item.get('cn_code') or item.get('taric_code') or item.get('hs_code') or ''
    candidates = candidates_for_cn(cn)
    controlled = True if exact else None
    if code and not exact:
        reasons.append(reason('unknown_entry', 'Valvontakoodia ei löydy liitteestä I.', 'Control code is absent from Annex I.'))
    if not code:
        reasons.append(reason('technical_classification', 'Tekniset ominaisuudet on tarkistettava liitteestä I.',
                              'Technical specifications must be checked against Annex I; CN alone is not a classification.'))
    # An explicit, evidenced negative technical assessment is needed to conclude False.
    classification = item.get('dual_use_classification') or {}
    if not code and classification.get('controlled') is False and classification.get('basis') and classification.get('reviewed_by'):
        controlled = False
    measures, unavailable = [], False
    if cn and flow in ('export', 'transit') and destination:
        try:
            t = await taric.lookup(cn, country=destination, flow='export', on=on)
            measures = [m for m in t['measures'] if 'dual' in m.get('type', '').lower() or
                        any(c.get('certificate') in ('X002', 'Y901') for c in m.get('conditions', []))]
            unavailable = not t.get('known_code', True)
        except (taric.TaricUnavailable, ValueError):
            unavailable = True
    embargo = data('embargo_destinations')['countries'].get(destination, {}).get('measures', [])
    arms = any(m['kind'].startswith('arms') and not m.get('political_declaration') for m in embargo)
    catch = []
    for flag, fi, en in [('wmd_end_use', 'Joukkotuhoaseiden loppukäyttö', 'WMD end use'),
                         ('military_end_use', 'Sotilaallinen loppukäyttö asevientikiellon kohdemaassa', 'Military end use in an embargo destination'),
                         ('illicit_military_parts', 'Luvattomasti vietyjen sotilastuotteiden osat', 'Parts for unlawfully exported military items'),
                         ('cyber_surveillance_abuse', 'Kybervalvonnan väärinkäyttö', 'Cyber-surveillance abuse'),
                         ('authority_notified', 'Viranomaisen ilmoittama lupavaatimus', 'Authority-notified authorisation requirement')]:
        if item.get(flag) and (flag != 'military_end_use' or arms):
            catch.append(reason(flag, fi, en))
    export = flow == 'export' and destination not in EU
    annex_iv = bool(exact and _overlap(code, [x['code'] for x in data('annex4')['items']]))
    licence = (controlled if export else bool(annex_iv) if flow in ('export', 'transfer') and destination in EU else False)
    if catch and flow != 'import':
        licence = True
    applicable = export or flow in ('transit', 'transfer')
    if not destination or on != source['valid_on']:
        reasons.append(reason('scope_review', 'Kohdemaa tai aineiston ajantasaisuus on tarkistettava.',
                              'Destination or reference-data validity requires review.'))
    if unavailable:
        reasons.append(reason('taric_unavailable', 'TARIC-valvontatoimia ei voitu varmistaa.', 'TARIC controls could not be verified.'))
    prohibited = bool(controlled and export and any(m['kind'].startswith('dual_use') and m['kind'] != 'dual_use_entities' for m in embargo))
    options = _authorisations(code, destination) if exact and export and not prohibited and not catch else []
    # EU003/004/007 require a particular transaction (repair/exhibition/intragroup).
    # Display all candidate routes, but never assume that purpose from the destination.
    suggested = next((a for a in options if a['type'] == 'EU001'), None)
    auth = suggested if suggested else {'type': 'individual', 'basis': 'Reg (EU) 2021/821 Articles 3, 4, 5, 12 and 22',
                                     'status': 'competent_authority_review', 'alternatives': ['global']}
    review = applicable and (controlled is not False or bool(catch) or unavailable or on != source['valid_on'] or not destination)
    if not applicable and not licence:
        auth = None
    if prohibited:
        reasons.append(reason('destination_ban', 'Kohdemaan kaksikäyttötuotteiden vientikielto; poikkeus vaatii viranomaispäätöksen.',
                              'Destination dual-use prohibition; a derogation requires a competent-authority decision.'))
    if licence:
        reasons.append(reason('licence_required', 'Vientilupa tai siirtolupa vaaditaan.', 'Export or transfer authorisation is required.'))
    return {'controlled': controlled, 'candidates': candidates, 'licence_required': licence,
            'authorisation': auth, 'authorisation_options': options, 'catch_all': catch,
            'legal_point': 'dual_use_export' if review or licence else None, 'reasons': reasons,
            'prohibited': prohibited, 'requires_review': bool(review or licence), 'annex_iv': annex_iv,
            'taric_measures': measures, 'source': source, 'date': on}
