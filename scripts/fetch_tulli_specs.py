"""Fetch unmodified official Tulli schemas/examples; pin release and record SHA256."""
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import zipfile
import requests

ROOT = Path(__file__).resolve().parents[1] / 'backend/data/xsd/tulli'
BASE = 'https://finnishcustoms-suomentulli.github.io/MIGs'
RELEASES = {'import': ('Import', '2.1.7'), 'export': ('Export', '1.1.1')}

def main():
    for system, (remote, version) in RELEASES.items():
        dest = ROOT / system / version
        dest.mkdir(parents=True, exist_ok=True)
        records = []
        def fetch(relative):
            url = f'{BASE}/{remote}/common/{version}/{relative}'
            response = requests.get(url, timeout=45)
            response.raise_for_status()
            records.append({'url': url, 'sha256': hashlib.sha256(response.content).hexdigest(),
                            'retrieved_at': datetime.now(timezone.utc).isoformat()})
            return response.content
        archive = fetch('schemas.zip')
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            for info in z.infolist():
                if info.is_dir(): continue
                path = dest / info.filename
                if not path.resolve().is_relative_to(dest.resolve()): raise ValueError('Unsafe archive path')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(z.read(info))
        messages = ['FI415B','FI428B','FI429B','FI416B','FI444B'] if system == 'import' else ['FI515A','FI528A','FI529A','FI599A','FI516A','FI544A','FI510A']
        for message in messages:
            (dest / 'examples').mkdir(exist_ok=True)
            (dest / 'examples' / f'{message}.xml').write_bytes(fetch(f'examples/{message}.xml'))
        for name in ['data.json', 'constraints.json', 'usecases.json']:
            (dest / name).write_bytes(fetch(name))
        (dest / 'manifest.json').write_text(json.dumps(records, indent=2)+'\n')
        files = '\n'.join(f'- `{p.relative_to(dest)}` SHA256 `{hashlib.sha256(p.read_bytes()).hexdigest()}`' for p in sorted(dest.rglob('*')) if p.is_file() and p.name not in ['README.md', 'manifest.json'])
        (dest / 'README.md').write_text(f'# Official Tulli {system} MIG {version}\n\nSource: {BASE}/{remote}/en/index.html\n\nFetched by `scripts/fetch_tulli_specs.py` on {datetime.now(timezone.utc).date()}.\nSchemas and examples are unmodified official downloads. Archive URLs, UTC timestamps and SHA256 are in manifest.json.\nThe import schema internal version is 3.0.37; export is 1.0.90. MIG version and XSD version are distinct.\nExamples are authority examples, not customer declarations and not evidence of legal correctness.\n\n'+files+'\n')
        print(system, version, len(list(dest.glob('*.xsd'))), 'official XSD files')

if __name__ == '__main__': main()
