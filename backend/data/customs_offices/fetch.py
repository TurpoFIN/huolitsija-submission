"""Capture the complete official COL ZIP, download page and provenance. No mirrors.

PYTHONPATH=backend python backend/data/customs_offices/fetch.py --out <new-directory>
Optional --install loads into the explicitly configured DB_NAME; normally use the daily job.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx


async def capture(out, install=False):
    from services import customs_offices as col
    out.mkdir(parents=True, exist_ok=False)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 headers={"User-Agent": col.USER_AGENT, "Accept-Language": "en"}) as client:
        page = await client.get(col.DOWNLOAD_PAGE)
        page.raise_for_status()
        (out / "download-page.html").write_bytes(page.content)
        raw, rows, source = await col.download(client=client)
    if source['source_url'] not in page.text:
        raise ValueError("Official page did not link the requested COL snapshot")
    source['download_page'] = col.DOWNLOAD_PAGE
    source['download_page_sha256'] = hashlib.sha256(page.content).hexdigest()
    source['archive_file'] = 'COL-Generic.zip'
    (out / source['archive_file']).write_bytes(raw)
    (out / 'SOURCE.json').write_text(json.dumps(source, indent=2) + '\n')
    (out / 'README.md').write_text(
        '# Official EU Customs Office List capture\n\n'
        f"URL: {source['source_url']}\n\nCaptured: {source['fetched_at']}\n\n"
        f"Snapshot date: {source['source_date']}\n\nSHA-256 (unaltered ZIP): `{source['sha256']}`\n\n"
        f"{len(rows)} offices. Reproduce with `PYTHONPATH=backend python backend/data/customs_offices/fetch.py --out <new-directory>`.\n\n"
        'The archive includes roles, traffic competence, seasonal timetables, addresses, status/version dates and UN/LOCODE. '
        'No coordinates are supplied in this capture. GeoInfoCode is a frontier/type code, not a coordinate. '
        'An absent activeTo is an open-ended office version, not a fabricated expiry. '
        'The dated extract is not a complete history of office versions.\n')
    summary = {**source, 'countries':len({r['country'] for r in rows}),
               'coordinates_count':sum('lat' in r for r in rows),
               'offices':[{k:r[k] for k in ('code','name','roles','valid_from','valid_to','unlocode','address')}
                          for r in rows if r['code'] in {'FI009801','FI015300','FI002000','DE004851'}]}
    if install:
        if not os.environ.get('DB_NAME'):
            raise ValueError('--install requires explicit DB_NAME')
        summary['installed'] = await col.install_snapshot(raw, source)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--install', action='store_true')
    args = p.parse_args()
    asyncio.run(capture(args.out, args.install))
