"""Builds data/places_geonames_towns.tsv.gz: the gazetteer's towns tier (s5).

data/places_geonames.json (places_build.py) covers every city with >= 15 000 inhabitants, with aliases and Finnish
exonyms. This file adds the smaller towns, so a quote from "Mazamitla, Mexico" or "Tsumeb" resolves offline instead of
depending on the public Nominatim / Photon servers, which rate-limit or refuse shared IPs.

Source
- GeoNames (CC BY 4.0, https://www.geonames.org/): https://download.geonames.org/export/dump/cities1000.zip, every
  populated place with population >= 1 000 (plus seats of administrative divisions), columns as in
  https://download.geonames.org/export/dump/readme.txt; https://download.geonames.org/export/dump/admin1CodesASCII.txt
  for the admin1 name ("Jalisco").

Rows: cities1000 places not already in cities15000 (by geonameid), without sections of cities (PPLX), historical,
abandoned or destroyed places (PPLH, PPLQ, PPLW, PPLCH). Columns: id, name, ascii, country, admin1, lat, lon,
population. No aliases: the GeoNames name and its ASCII form (empty when equal to the name) only. Gzipped TSV, ~3 MB:
the first line is "# " + a JSON header (version, sources, licence, columns), then one row per line, no tabs inside
fields (GeoNames names never contain tabs). TSV rather than JSON so the app can load it line by line into columns.

Run: ~/work/venv/bin/python /home/user/work/huolitsija/backend/data/towns_build.py [cache_dir]
"""
import gzip
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
OUT = HERE / "places_geonames_towns.tsv.gz"
MAIN = HERE / "places_geonames.json"
GEONAMES = "https://download.geonames.org/export/dump/"
TOWNS_URL = GEONAMES + "cities1000.zip"
ADMIN1_URL = GEONAMES + "admin1CodesASCII.txt"
UA = {"User-Agent": "Huolitsija-build/1.0 (ops@huolitsija.fi)"}
COLUMNS = ["id", "name", "ascii", "country", "admin1", "lat", "lon", "population"]
SKIP_CODES = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"}


def fetch(url: str, cache: Path) -> bytes:
    path = cache / url.rsplit("/", 1)[-1]
    if path.exists():
        return path.read_bytes()
    r = httpx.get(url, headers=UA, timeout=300, follow_redirects=True)
    r.raise_for_status()
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return r.content


def main():
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "work" / "cache" / "geonames"
    admin1 = {}
    for line in fetch(ADMIN1_URL, cache).decode("utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            admin1[parts[0]] = parts[1]
    main_ids = {str(r[0]) for r in json.loads(MAIN.read_text(encoding="utf-8"))["rows"]}
    with zipfile.ZipFile(io.BytesIO(fetch(TOWNS_URL, cache))) as z:
        text = z.read("cities1000.txt").decode("utf-8")
    rows, skipped = [], {"in_main": 0, "feature": 0}
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) < 15:
            continue
        gid, name, ascii_, lat, lon, fcode, cc, adm1, pop = f[0], f[1], f[2], f[4], f[5], f[7], f[8], f[10], f[14]
        if gid in main_ids:
            skipped["in_main"] += 1
            continue
        if fcode in SKIP_CODES:
            skipped["feature"] += 1
            continue
        rows.append([int(gid), name, ascii_ if ascii_ != name else "", cc, admin1.get(f"{cc}.{adm1}", ""),
                     round(float(lat), 4), round(float(lon), 4), int(pop or 0)])
    rows.sort(key=lambda r: (r[3], -r[7], r[1]))
    header = {"version": datetime.now(timezone.utc).strftime("%Y%m%d") + f"-{len(rows)}",
              "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "sources": [TOWNS_URL + " (GeoNames, CC BY 4.0)", ADMIN1_URL + " (GeoNames, CC BY 4.0)"],
              "license": "CC BY 4.0, GeoNames (https://www.geonames.org/)", "columns": COLUMNS,
              "excluded": {"in_cities15000": skipped["in_main"], "feature_codes": sorted(SKIP_CODES), "by_feature": skipped["feature"]}}
    lines = ["# " + json.dumps(header, ensure_ascii=False, separators=(",", ":"))]
    lines += ["\t".join(str(x) for x in r) for r in rows]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    OUT.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    print(json.dumps({**header, "bytes_tsv": len(data), "bytes_gz": OUT.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
