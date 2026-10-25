"""Capture unchanged official screening feeds, with provenance, for offline tests.
Run: PYTHONPATH=backend python scripts/controls_capture.py
"""
import asyncio
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import httpx
from services import screening

ROOT = Path(__file__).resolve().parents[1] / 'backend/tests/fixtures/controls'
URLS = {k: v['url'] for k,v in screening.LIST_SOURCES.items()}
URLS.update(ofac_alt=screening.OFAC_ALT_URL, ofac_add=screening.OFAC_ADD_URL)

async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        async def fetch(key, url):
            r = await client.get(url)
            r.raise_for_status()
            raw = r.content
            path = ROOT / (key + '.gz')
            path.write_bytes(gzip.compress(raw, mtime=0))
            return key, {'url': url, 'final_url': str(r.url), 'file': path.name,
                         'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
                         'fetched_at': datetime.now(timezone.utc).isoformat()}
        results = await asyncio.gather(*(fetch(k, u) for k,u in URLS.items()))
    (ROOT / 'manifest.json').write_text(json.dumps(dict(results), indent=2) + '\n')
    for k, v in results:
        print(k, v['bytes'], v['sha256'])

if __name__ == '__main__':
    asyncio.run(main())
