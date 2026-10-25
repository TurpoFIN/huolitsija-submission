"""Official Tulli codes, including temporal validity and import applicability."""
import json
from datetime import date
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'data/tulli_codelists'

@lru_cache(maxsize=1)
def lists():
    return json.loads((ROOT / 'codelists.json').read_text())


def entry(list_id, code, *, on=None, flow='import'):
    day = (on or date.today().isoformat())[:10]
    matches = []
    for row in lists()[list_id]['CodeItems']:
        if row['Code'] != str(code or '') or not row['StartDate'] <= day <= row['EndDate']:
            continue
        filters = row.get('Filters') or {}
        keys = [k for k in filters if k.startswith('Filter_IMP')] if flow == 'import' else []
        if keys and not any(filters[k] for k in keys):
            continue
        matches.append(row)
    return max(matches, key=lambda r:r['StartDate']) if matches else None


def error_text(code, *, on=None):
    row = entry('CL401', code, on=on, flow=None)
    if row and isinstance(row.get('Name'), dict):
        return {k:row['Name'].get(k) for k in ('fi', 'en')}
    return {'fi': f'Tuntematon Tullin virhekoodi: {code}', 'en': f'Unknown Tulli error code: {code}'}
