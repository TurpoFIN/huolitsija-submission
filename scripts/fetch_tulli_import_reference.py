"""Fetch unmodified official rows used by import preflight. No credentials required."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import httpx

DEST = Path(__file__).resolve().parents[1] / 'backend/data/tulli_codelists'
LISTS = {'CL754', 'CL347', 'CL521', 'CL603', 'CL213', 'CL017', 'NCL232', 'CL401', 'CL008', 'CL352', 'CL380', 'CL018', 'CL091', 'CL604', 'CL750', 'CL181'}
BASE = 'https://raw.githubusercontent.com/finnishcustoms-suomentulli/'

def fetch():
    DEST.mkdir(parents=True, exist_ok=True)
    sources = []
    def download(url):
        r = httpx.get(url, timeout=60); r.raise_for_status()
        sources.append({'url': url, 'fetched_at': datetime.now(timezone.utc).isoformat(),
                        'sha256': hashlib.sha256(r.content).hexdigest()})
        return r.json()
    raw = download(BASE + 'codelists/master/codelists.json')
    selected = {x['Identification']: x for x in raw['CodeLists'] if x['Identification'] in LISTS}
    assert selected.keys() == LISTS
    (DEST / 'codelists.json').write_text(json.dumps(selected, ensure_ascii=False, indent=2) + '\n')
    data = download(BASE + 'MIGs/master/Import/common/2.1.7/data.json')
    constraints = download(BASE + 'MIGs/master/Import/common/2.1.7/constraints.json')
    rows = {k: {**{a:b for a,b in v.items() if a != 'messages'}, 'messages': {'FI415B':v['messages']['FI415B']}}
            for k,v in data.items() if 'FI415B' in v.get('messages',{})}
    used = {c for v in rows.values() for c in v['messages']['FI415B'].get('constraints', [])}
    (DEST / 'import-requirements.json').write_text(json.dumps({'data':rows, 'constraints':{k:constraints[k] for k in sorted(used)}}, ensure_ascii=False, indent=2)+'\n')
    (DEST / 'sources.json').write_text(json.dumps(sources, indent=2)+'\n')
    (DEST / 'README.md').write_text('# Official Finnish Customs import reference\n\nFetched by `scripts/fetch_tulli_import_reference.py`.\n'
        'Codelists are trimmed by list, retaining original rows, dates, filters and translations.\n'
        'MIG rows retain FI415B requirements and referenced constraints. No values are fabricated.\n\n' + '\n'.join(
        f"- {s['url']}\n  - Fetched: {s['fetched_at']}\n  - SHA256 (complete downloaded bytes): `{s['sha256']}`" for s in sources)+'\n')
    print(json.dumps(sources, indent=2))

if __name__ == '__main__': fetch()
