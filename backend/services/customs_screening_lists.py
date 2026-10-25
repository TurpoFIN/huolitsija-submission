"""Official no-key CSL CSV and FCDO XML adapters for the shared screening engine."""
import csv
import io
from datetime import date
from lxml import etree

CSL_URL = 'https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.csv'
UK_URL = 'https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml'
SOURCES = {
    'csl': {'label': 'US Consolidated Screening List', 'short': 'US CSL', 'url': CSL_URL,
            'authority': 'US International Trade Administration'},
    'uk': {'label': 'UK Sanctions List', 'short': 'UK', 'url': UK_URL,
           'authority': 'UK Foreign, Commonwealth & Development Office'},
}

def parse_csl(raw):
    rows = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    if not {'_id', 'source', 'name', 'alt_names'}.issubset(rows.fieldnames or []):
        raise ValueError('CSL CSV schema changed')
    entries = []
    for r in rows:
        # SDN already has its dedicated adapter. DPL orders can expire.
        if r['source'].startswith('Specially Designated Nationals'):
            continue
        if r.get('end_date') and r['source'].startswith('Denied Persons'):
            try:
                if date.fromisoformat(r['end_date'][:10]) < date.today():
                    continue
            except ValueError:
                pass  # retain for review if the publisher changes the date format
        names = [r['name']] + [x.strip() for x in r['alt_names'].split(';') if x.strip()]
        if not r['name'].strip():
            continue
        entries.append({'list': 'csl', 'ref': r['_id'], 'logical_id': r['_id'], 'primary_name': names[0],
                        'names': names, 'type': {'individual': 'person', 'entity': 'entity', 'vessel': 'vessel'}.get(r.get('type', '').lower()),
                        'countries': [], 'programmes': [r['source']], 'listed_on': r.get('start_date') or None,
                        'remarks': r.get('remarks', '')[:300]})
    return entries, None

def parse_uk(raw):
    root = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    entries = []
    for el in root.findall('Designation'):
        names = [' '.join((n.findtext('Name' + str(i)) or '').strip() for i in range(1, 7)).strip()
                 for n in el.findall('Names/Name')]
        names += [n.text for n in el.findall('NonLatinNames/NonLatinName/NameNonLatinScript') if n.text]
        names = list(dict.fromkeys(n for n in names if n))
        if not names:
            continue
        from services.screening import country_iso2
        countries = [country_iso2(n.text) for n in el.findall('.//AddressCountry')]
        entries.append({'list': 'uk', 'ref': el.findtext('UniqueID'), 'logical_id': el.findtext('UniqueID'),
                        'primary_name': names[0], 'names': names,
                        'type': {'Individual': 'person', 'Entity': 'entity', 'Ship': 'vessel'}.get(el.findtext('IndividualEntityShip')),
                        'countries': sorted({c for c in countries if c}),
                        'programmes': [el.findtext('RegimeName') or 'UK sanctions'],
                        'listed_on': el.findtext('DateDesignated'), 'remarks': (el.findtext('OtherInformation') or '')[:300]})
    return entries, root.findtext('DateGenerated')
