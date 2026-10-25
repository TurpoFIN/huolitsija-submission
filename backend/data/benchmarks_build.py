"""Builds data/benchmark_indices.json (the seed loaded into Mongo `benchmark_indices`) from the dated research file.

Input: data/benchmark_research_2026-10-24.json — every value read from its publisher on 2026-10-24 with the
publication/assessment date, URL and the quoted sentence (sources listed in notes/benchmark-sources-2026-10-24.md):
- Drewry World Container Index 2026-10-22 (USD/FEU), Freightos Baltic Index 2026-10-23 (USD/FEU),
- Freightos Air Index and TAC Index (USD/kg), DAT US truckload (USD/mile), Shriram India truck rentals (INR/trip),
- TITR Middle Corridor through tariffs 2026 (USD/40'), index1520 China–Europe rail average (USD/FEU),
- carrier surcharge notices (PSS, war risk), ECB reference rates 2026-10-23, DHL Express Finland 2026 tariff.
This script only re-shapes them: region codes for the pricing model, ISO country codes for courier zones,
period labels ("2026-09", "2026-Q2") to the last day of the period. Derived per-km levels state their arithmetic.

Run: ~/work/venv/bin/python /home/user/work/huolitsija/backend/data/benchmarks_build.py
"""
import calendar
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
SRC = HERE / "benchmark_research_2026-10-24.json"
OUT = HERE / "benchmark_indices.json"

SEA_PAIRS = {
    "WCI-SHA-RTM": ("FEA", "NEU"), "WCI-RTM-SHA": ("NEU", "FEA"), "WCI-SHA-GOA": ("FEA", "MED"), "WCI-SHA-LAX": ("FEA", "NAW"),
    "WCI-LAX-SHA": ("NAW", "FEA"), "WCI-SHA-NYC": ("FEA", "NAE"), "WCI-NYC-RTM": ("NAE", "NEU"), "WCI-RTM-NYC": ("NEU", "NAE"),
    "FBX01": ("FEA", "NAW"), "FBX02": ("NAW", "FEA"), "FBX03": ("FEA", "NAE"), "FBX04": ("NAE", "FEA"), "FBX11": ("FEA", "NEU"),
    "FBX12": ("NEU", "FEA"), "FBX13": ("FEA", "MED"), "FBX14": ("MED", "FEA"), "FBX21": ("NAE", "NEU"), "FBX22": ("NEU", "NAE"),
    "FBX24": ("NEU", "SAE"), "FBX26": ("NEU", "SAW"),
}
AIR_PAIRS = {"FAX-CN-NA": ("FEA", "NAM"), "FAX-CN-EU": ("FEA", "EUR"), "FAX-EU-NA": ("EUR", "NAM"), "FAX-NA-EU": ("NAM", "EUR"),
             "TAC-HKG-NA": ("FEA", "NAM"), "TAC-HKG-EU": ("FEA", "EUR")}
# Which side's terminal handling the index level already contains (Drewry methodology, thc_by_route).
THC_INCLUDED = {"WCI-RTM-SHA": ["origin"], "WCI-SHA-LAX": ["destination"], "WCI-LAX-SHA": ["origin"], "WCI-SHA-NYC": ["destination"],
                "WCI-NYC-RTM": ["origin", "destination"], "WCI-RTM-NYC": ["origin", "destination"]}


def period_end(p: str | None) -> str | None:
    p = (p or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p):
        return p
    m = re.fullmatch(r"(\d{4})-(\d{2})", p)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return f"{y}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
    m = re.fullmatch(r"(\d{4})-Q([1-4])", p)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        mo = q * 3
        return f"{y}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
    return None


def country_iso(name: str, alias: dict) -> str | None:
    from services.gazetteer import fold

    n = name.replace("’", "'").strip()
    cands = [n, re.sub(r",\s*The$", "", n), re.sub(r"^The\s+", "", n)]
    m = re.match(r"(.+?),\s*(Rep\. of|Republic of|People's Republic|Dem\. Rep\. of|Fed\. States of|Islamic Rep\. of)(.*)$", n)
    if m:
        cands += [m.group(1), f"{m.group(2).replace('Rep.', 'Republic')} {m.group(1)}"]
    cands += [re.sub(r"\s*\(.*?\)", "", c) for c in list(cands)]
    cands += [re.sub(r"\s+SAR\s+China$", "", c) for c in list(cands)]
    special = {"korea, rep. of (south)": "KR", "korea, d.p.r. of (north)": "KP", "united states of america": "US",
               "china, people's republic": "CN", "taiwan, china": "TW", "hong kong sar china": "HK", "macau sar china": "MO",
               "canary islands, the": "IC", "russian federation, the": "RU", "congo, dpr": "CD", "congo": "CG",
               "cote d'ivoire": "CI", "lao p.r.": "LA", "bonaire": "BQ", "saba": "BQ", "st. eustatius": "BQ",
               "st. kitts": "KN", "st. lucia": "LC", "st. vincent": "VC", "st. barthelemy": "BL", "st. maarten": "SX",
               "st. martin": "MF", "netherlands, the": "NL", "czech republic, the": "CZ", "philippines, the": "PH",
               "ireland, rep. of": "IE", "gambia": "GM", "bahamas": "BS", "comoros": "KM", "maldives": "MV",
               "turks & caicos": "TC", "virgin islands (british)": "VG", "virgin islands (us)": "VI", "kosovo": "XK",
               "east timor": "TL", "timor leste": "TL", "swaziland": "SZ", "eswatini": "SZ", "micronesia, fed. states of": "FM",
               "tahiti": "PF", "kyrgyzstan": "KG", "moldova, rep. of": "MD", "syria": "SY", "iran (islamic rep. of)": "IR",
               "vietnam": "VN", "brunei": "BN", "cape verde": "CV", "burma": "MM", "myanmar": "MM", "reunion, island of": "RE",
               "guinea rep.": "GN", "guinea-bissau": "GW", "equatorial guinea": "GQ", "central african republic": "CF",
               "south sudan": "SS", "north macedonia": "MK", "montenegro, rep. of": "ME", "serbia, rep. of": "RS",
               "faroe islands": "FO", "greenland": "GL", "guernsey": "GG", "jersey": "JE", "gibraltar": "GI",
               "nevis": "KN", "curacao": "CW", "new caledonia": "NC", "papua new guinea": "PG", "marshall islands": "MH",
               "palestine authority": "PS", "western sahara": "EH", "somaliland, rep. of (north somalia)": "SO",
               "antigua": "AG", "lao p .d.r.": "LA", "congo, democratic rep. of": "CD", "guinea republic": "GN",
               "guinea-equatorial": "GQ", "korea, d.p .r. of (north)": "KP", "somalia hargeisa": "SO", "somalia mogadishu": "SO"}
    if n.lower() in special:
        return special[n.lower()]
    for c in cands:
        hit = alias.get(fold(c))
        if hit:
            return hit
    return None


def main():
    from services.gazetteer import _countries

    alias = _countries()["alias"]
    d = json.loads(SRC.read_text(encoding="utf-8"))
    fx = {f["pair"]: f for f in d["fx"]}
    series = []
    for s in d["series"]:
        code = s["lane_code"]
        doc = {k: s.get(k) for k in ("index", "lane_code", "label", "origin", "destination", "mode", "unit", "value", "source_url", "snippet")}
        doc["period"] = s.get("as_of")
        doc["as_of"] = period_end(s.get("as_of"))
        if code in SEA_PAIRS:
            doc["origin_region"], doc["destination_region"] = SEA_PAIRS[code]
            doc["unit"] = "USD/FEU"
            doc["thc_included"] = THC_INCLUDED.get(code, [])
        elif code in AIR_PAIRS:
            doc["origin_region"], doc["destination_region"] = AIR_PAIRS[code]
            doc["unit"] = "USD/kg"
        elif doc["mode"] == "sea":
            doc["mode"] = "sea_composite"  # WCI composite / FBX global: reference only, no region pair
        elif doc["mode"] == "air":
            doc["mode"] = "air_composite"
        series.append(doc)
    by = {s["lane_code"]: s for s in series}

    # Derived road levels used by services.benchmarks.road_eur_per_km (unit converted at runtime with the ECB rate)
    dat = by.get("DAT-SPOT-VAN-W")
    if dat:
        series.append({**dat, "lane_code": "ROAD-NORTH_AMERICA", "label": "US/Canada full truck (DAT dry van spot incl. fuel)",
                       "mode": "road", "unit": "USD/mile", "derived_from": "DAT-SPOT-VAN-W",
                       "derivation": "DAT national dry-van spot rate incl. fuel, 7-day average, used per loaded mile"})
    ind = by.get("IN-DEL-CCU-DEL")
    if ind:
        km = 1500.0  # Delhi–Kolkata by road (NH19), one way
        series.append({**ind, "lane_code": "ROAD-SOUTH_ASIA", "label": "India full truck (Shriram Delhi–Kolkata–Delhi rental)",
                       "mode": "road", "unit": "INR/km", "value": round(float(ind["value"]) / (2 * km), 2),
                       "derived_from": "IN-DEL-CCU-DEL",
                       "derivation": f"INR {ind['value']:,} per 18 t round trip / (2 × {km:,.0f} km)".replace(",", " ")})
    # Middle Corridor through tariffs (rail + Caspian ferry), by direction
    for code, key in (("TITR-ALT-CND", "RAIL-MIDCOR-WEST"), ("TITR-CND-ALT", "RAIL-MIDCOR-EAST"),
                      ("TITR-ALT-IST", "RAIL-MIDCOR-WEST-TR"), ("TITR-IST-ALT", "RAIL-MIDCOR-EAST-TR")):
        if code in by:
            series.append({**by[code], "lane_code": key, "mode": "rail", "unit": "USD/FEU", "derived_from": code,
                           "derivation": "TITR published through tariff for a loaded 40' container"})

    # Surcharge notices -> sea_surcharge levels (applied only to lanes without their own index level)
    rules = [
        # (match carrier, code, trade substring) -> applicability
        ("Hapag-Lloyd", "WRS", "Gulf", {"regions_any": ["MEG"], "unit": "USD/TEU"}),
        ("Hapag-Lloyd", "PSS", "Southwest Africa", {"origin_regions": ["FEA", "SEA", "OCE"],
                                                     "destination_countries": ["AO", "CG", "CD", "GA", "CM", "NA", "GQ"], "unit": "USD/TEU"}),
        ("CMA CGM", "PSS", "South China", {"origin_regions": ["FEA"], "destination_countries": ["ZA"], "unit": "USD/TEU"}),
        ("Hapag-Lloyd", "PSS", "Northern Europe", {"origin_regions": ["NEU"], "destination_countries": ["US"], "unit": "USD/TEU"}),
        ("CMA CGM", "PSS", "Brazil", {"origin_countries": ["BR", "AR", "UY", "PY"], "destination_regions": ["NEU", "MED", "MEG", "RSE"],
                                      "unit": "USD/FEU"}),
    ]
    for s in d.get("surcharges") or []:
        for carrier, code, trade, extra in rules:
            if s["carrier"] == carrier and s["code"] == code and trade in s["trade"]:
                vt = s.get("valid_to")
                vt = vt if vt and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(vt)) else None
                series.append({"index": f"{carrier} {code}", "lane_code": f"SUR-{carrier.split()[0].upper()}-{code}-{trade.split()[0].upper()}",
                               "label": f"{carrier} {code}: {s['trade']}", "mode": "sea_surcharge", "code": "WRS" if code == "WRS" else code,
                               "value": float(s["amount"]), "valid_from": s.get("valid_from"), "valid_to": vt,
                               "as_of": s.get("published") or s.get("valid_from"), "source_url": s.get("source_url"),
                               "snippet": s.get("snippet"), **extra})
    # Courier: DHL Express Finland 2026 export tariff, one level per zone with ISO country codes
    c = d.get("courier") or {}
    unmapped = []
    for zone, names in (c.get("zones") or {}).items():
        zkey = zone.replace("Zone ", "").split()[0].split("/")[0]
        prices = (c.get("prices") or {}).get(f"Zone {zkey}")
        if not prices:
            continue
        isos = []
        for n in names:
            iso = country_iso(n, alias)
            (isos.append(iso) if iso else unmapped.append(n))
        series.append({"index": "DHL Express FI 2026", "lane_code": f"COURIER-DHL-FI-Z{zkey}", "label": f"DHL Express Worldwide export from Finland, zone {zkey}",
                       "mode": "courier", "zone": zkey, "countries": sorted(set(isos)), "prices": prices, "currency": c.get("currency", "EUR"),
                       "vat_included": bool(c.get("vat_included")), "unit": "EUR/shipment", "value": prices.get("1"),
                       "as_of": period_end(c.get("as_of")), "source_url": c.get("source_url"), "snippet": c.get("snippet"),
                       "transit_days": 1 if zkey in ("1", "2") else 2 if zkey in ("3", "4", "5", "6") else 3 if zkey in ("7", "8") else 4})
    out = {"version": "2026-10-24", "built_from": SRC.name, "series": series,
           "fx": [{k: f.get(k) for k in ("pair", "value", "as_of", "source_url", "snippet")} for f in d["fx"]],
           "methodology": d.get("methodology"), "missing": d.get("missing")}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"series {len(series)} fx {len(out['fx'])} -> {OUT.name}")
    print("courier countries unmapped:", unmapped)


if __name__ == "__main__":
    main()
