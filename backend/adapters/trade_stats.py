"""Finland's goods trade by CN chapter and partner country, from Eurostat Comext (dataset DS-045409).

    GET https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data/DS-045409
        ?format=JSON&lang=en&reporter=FI&partner=WORLD&partner=DE&...&product=84&flow=1&flow=2
        &indicators=VALUE_IN_EUROS&freq=A&time=2025

JSON-stat 2.0: `value` is a sparse dict keyed by the row-major index over `id`/`size`. flow 1 = import, 2 = export.
Results are cached in `trade_stats_cache` with the source URL and period. TRADE_STATS_SOURCE=eurostat (only source).

Each target TOL 2008 group maps to the CN chapters its companies ship (TOL_CN, explicit and small), so a lead's score can
say: "TOL 4669 machinery wholesale -> CN 84 FI imports from DE EUR 2.2 bn (Eurostat 2025)".
"""
import logging
import math
from datetime import datetime, timedelta, timezone

import httpx

from core import config
from core.db import db, register_index
from core.util import NO_ID, now_iso

log = logging.getLogger("trade_stats")
register_index("trade_stats_cache", [("source", 1), ("reporter", 1), ("product", 1), ("period", 1)], unique=True)

COMEXT_URL = "https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data/DS-045409"
UA = {"User-Agent": "Huolitsija CRM/1.0 (trade statistics; +https://huolitsija.fi)"}
PARTNERS = ["WORLD", "DE", "SE", "NL", "CN", "US", "EE", "PL", "NO", "IT", "GB", "DK"]
CACHE_DAYS = 30

# TOL 2008 group -> (CN chapters shipped, label fi, label en)
TOL_CN = {
    "1610": (["44"], "sahatavara", "sawmilling"),
    "1623": (["44"], "rakennuspuusepäntuotteet", "builders' carpentry and joinery"),
    "1712": (["48"], "paperi ja kartonki", "paper and paperboard"),
    "2016": (["39"], "muoviraaka-aineet", "plastics in primary forms"),
    "2229": (["39"], "muut muovituotteet", "other plastic products"),
    "2410": (["72"], "rauta ja teräs", "basic iron and steel"),
    "2511": (["73"], "metallirakenteet", "metal structures"),
    "2562": (["84", "73"], "metallien työstö", "machining"),
    "2599": (["73", "83"], "muut metallituotteet", "other fabricated metal products"),
    "2711": (["85"], "sähkömoottorit ja muuntajat", "electric motors and transformers"),
    "2822": (["84"], "nosto- ja siirtolaitteet", "lifting and handling equipment"),
    "2829": (["84"], "muut yleiskäyttöiset koneet", "other general-purpose machinery"),
    "2830": (["84", "87"], "maa- ja metsätalouskoneet", "agricultural and forestry machinery"),
    "2849": (["84"], "muut työstökoneet", "other machine tools"),
    "2892": (["84"], "kaivos- ja rakennuskoneet", "mining and construction machinery"),
    "2932": (["87"], "ajoneuvojen osat", "motor vehicle parts"),
    "3109": (["94"], "muut huonekalut", "other furniture"),
    "4641": (["61", "62", "63"], "tekstiilien tukkukauppa", "wholesale of textiles"),
    "4643": (["85"], "kodinkoneiden tukkukauppa", "wholesale of household appliances"),
    "4647": (["94"], "huonekalujen tukkukauppa", "wholesale of furniture"),
    "4652": (["85"], "elektroniikan tukkukauppa", "wholesale of electronic equipment"),
    "4662": (["84"], "työstökoneiden tukkukauppa", "wholesale of machine tools"),
    "4663": (["84"], "kaivos- ja rakennuskoneiden tukkukauppa", "wholesale of mining and construction machinery"),
    "4669": (["84", "85"], "muiden koneiden tukkukauppa", "wholesale of other machinery"),
    "4672": (["72", "73", "76"], "metallien tukkukauppa", "wholesale of metals"),
    "4673": (["44", "68", "69", "70"], "puu- ja rakennustarvikkeiden tukkukauppa", "wholesale of wood and construction materials"),
    "4674": (["73", "82", "84"], "rautakauppatavaroiden tukkukauppa", "wholesale of hardware and plumbing"),
    "4675": (["28", "29", "38"], "kemikaalien tukkukauppa", "wholesale of chemical products"),
    "4676": (["39", "40", "48"], "muiden välituotteiden tukkukauppa", "wholesale of other intermediate products"),
    "4690": (["39", "84", "85", "94"], "muu tukkukauppa", "non-specialised wholesale"),
    # TOL 2025 groups (PRH codes since 2026-01-01) where the number changed
    "3100": (["94"], "huonekalujen valmistus", "manufacture of furniture"),
    "4650": (["85"], "tieto- ja viestintälaitteiden tukkukauppa", "wholesale of ICT equipment"),
    "4664": (["84", "85"], "muiden koneiden ja laitteiden tukkukauppa", "wholesale of other machinery and equipment"),
    "4682": (["72", "73", "76"], "metallien ja metallimalmien tukkukauppa", "wholesale of metals and metal ores"),
    "4683": (["44", "68", "69", "70"], "puu- ja rakennustarvikkeiden tukkukauppa", "wholesale of wood and construction materials"),
    "4684": (["73", "82", "84"], "rautakauppatavaroiden tukkukauppa", "wholesale of hardware and plumbing"),
    "4685": (["28", "29", "38"], "kemikaalien tukkukauppa", "wholesale of chemical products"),
    "4686": (["39", "40", "48"], "muiden välituotteiden tukkukauppa", "wholesale of other intermediate products"),
}
CN_LABEL_EN = {"28": "inorganic chemicals", "29": "organic chemicals", "38": "chemical products", "39": "plastics",
               "40": "rubber", "44": "wood", "48": "paper", "61": "knitted apparel", "62": "apparel", "63": "textile articles",
               "68": "stone and cement articles", "69": "ceramics", "70": "glass", "72": "iron and steel",
               "73": "articles of iron or steel", "76": "aluminium", "82": "tools", "83": "misc. base-metal articles",
               "84": "machinery", "85": "electrical equipment", "87": "vehicles and parts", "94": "furniture"}
COUNTRY_FI = {"DE": "Saksa", "SE": "Ruotsi", "NL": "Alankomaat", "CN": "Kiina", "US": "Yhdysvallat", "EE": "Viro",
              "PL": "Puola", "NO": "Norja", "IT": "Italia", "GB": "Iso-Britannia", "DK": "Tanska"}


class TradeStatsUnavailable(Exception):
    pass


def source() -> str:
    return (config.env("TRADE_STATS_SOURCE") or "eurostat").lower()


def comext_url(product: str, period: str, partners: list[str] | None = None) -> str:
    q = ["format=JSON", "lang=en", "reporter=FI", *[f"partner={p}" for p in partners or PARTNERS], f"product={product}",
         "flow=1", "flow=2", "indicators=VALUE_IN_EUROS", "freq=A", f"time={period}"]
    return COMEXT_URL + "?" + "&".join(q)


def parse_jsonstat(data: dict) -> dict:
    """-> {partner: {"import": eur, "export": eur}} from a JSON-stat 2.0 Comext answer (sparse values)."""
    ids, sizes, dims = data["id"], data["size"], data["dimension"]
    index = {d: {code: pos for code, pos in dims[d]["category"]["index"].items()} for d in ids}
    rev = {d: {pos: code for code, pos in index[d].items()} for d in ids}
    out: dict[str, dict] = {}
    for key, value in (data.get("value") or {}).items():
        flat, coords = int(key), {}
        for d, size in reversed(list(zip(ids, sizes))):
            coords[d] = rev[d][flat % size]
            flat //= size
        flow = {"1": "import", "2": "export"}.get(coords["flow"])
        if flow and value is not None:
            out.setdefault(coords["partner"], {})[flow] = int(value)
    return out


async def _fetch(product: str, period: str) -> tuple[dict | None, str]:
    url = comext_url(product, period)
    async with httpx.AsyncClient(timeout=90, headers=UA) as c:
        r = await c.get(url)
    if r.status_code != 200:
        raise TradeStatsUnavailable(f"Eurostat Comext answered HTTP {r.status_code} for {url}")
    data = r.json()
    values = parse_jsonstat(data)
    if not values.get("WORLD"):
        return None, url
    return {"values": values, "label": data["dimension"]["product"]["category"]["label"].get(product),
            "updated": data.get("updated")}, url


async def chapter(product: str) -> dict:
    """Latest full year for one CN chapter (cached). -> cache row {product, period, url, values, label, fetched_at}."""
    if source() != "eurostat":
        raise TradeStatsUnavailable(f"TRADE_STATS_SOURCE={source()} is not supported (use eurostat)")
    year = datetime.now(timezone.utc).year
    fresh_after = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cached = await db.trade_stats_cache.find_one({"source": "eurostat_comext", "reporter": "FI", "product": product,
                                                 "fetched_at": {"$gte": fresh_after}}, NO_ID, sort=[("period", -1)])
    if cached:
        return cached
    last_err = None
    for period in (str(year - 1), str(year - 2)):
        try:
            got, url = await _fetch(product, period)
        except (httpx.HTTPError, ValueError, KeyError, TradeStatsUnavailable) as exc:
            last_err = exc
            continue
        if got is None:
            continue
        row = {"source": "eurostat_comext", "dataset": "DS-045409", "reporter": "FI", "product": product,
               "period": period, "url": url, "label": got["label"], "dataset_updated": got["updated"],
               "values": got["values"], "fetched_at": now_iso()}
        await db.trade_stats_cache.update_one({"source": row["source"], "reporter": "FI", "product": product, "period": period},
                                              {"$set": row}, upsert=True)
        return row
    stale = await db.trade_stats_cache.find_one({"source": "eurostat_comext", "reporter": "FI", "product": product}, NO_ID,
                                                sort=[("period", -1)])
    if stale:
        return {**stale, "stale": True}
    raise TradeStatsUnavailable(f"No Eurostat data for CN {product}: {last_err}")


def bn(eur: int) -> str:
    return f"€{eur / 1e9:.2f} bn" if eur >= 1e9 else f"€{eur / 1e6:.0f} m"


async def industry_score(tol: str) -> dict:
    """Trade-lane part of a lead score (0..40) with the reasons. Big FI trade in the goods this industry ships =
    more freight to win. -> {points, reasons[{code, points, fi, en, source_url, period}], top_lane, chapters}."""
    tol = (tol or "")[:4]
    if tol not in TOL_CN:
        return {"points": 0, "chapters": [], "top_lane": None, "reasons": [
            {"code": "trade_unmapped", "points": 0, "fi": f"TOL {tol}: ei CN-lukujen vastaavuutta, kauppatilastopisteitä ei anneta",
             "en": f"TOL {tol}: no CN chapter mapping, no trade-statistics points"}]}
    chapters, label_fi, label_en = TOL_CN[tol]
    rows, total, best = [], 0, None
    for cn in chapters:
        try:
            row = await chapter(cn)
        except TradeStatsUnavailable as exc:
            log.warning("trade stats CN %s: %s", cn, exc)
            continue
        rows.append(row)
        world = row["values"].get("WORLD", {})
        total += world.get("import", 0) + world.get("export", 0)
        for partner, v in row["values"].items():
            if partner == "WORLD":
                continue
            for flow in ("import", "export"):
                if v.get(flow) and (best is None or v[flow] > best["value_eur"]):
                    best = {"cn": cn, "partner": partner, "flow": flow, "value_eur": v[flow], "period": row["period"],
                            "url": row["url"]}
    if not rows:
        return {"points": 0, "chapters": chapters, "top_lane": None, "reasons": [
            {"code": "trade_unavailable", "points": 0, "fi": "Eurostat ei vastannut; kauppatilastopisteitä ei anneta",
             "en": "Eurostat did not answer; no trade-statistics points"}]}
    points = max(0, min(40, round(13.3 * math.log10(max(total, 1) / 1e8))))
    period = rows[0]["period"]
    cn_text = ", ".join(f"CN {r['product']}" for r in rows)
    reasons = [{"code": "trade_volume", "points": points, "source_url": rows[0]["url"], "period": period,
                "fi": f"TOL {tol} {label_fi} → {cn_text}: Suomen tuonti+vienti {bn(total)} (Eurostat {period})",
                "en": f"TOL {tol} {label_en} → {cn_text}: FI imports+exports {bn(total)} (Eurostat {period})"}]
    if best:
        verb_en = "imports from" if best["flow"] == "import" else "exports to"
        verb_fi = "tuonti maasta" if best["flow"] == "import" else "vienti maahan"
        reasons.append({"code": "trade_top_lane", "points": 0, "source_url": best["url"], "period": best["period"],
                        "fi": f"Suurin reitti: CN {best['cn']} FI {verb_fi} {COUNTRY_FI.get(best['partner'], best['partner'])} "
                              f"{bn(best['value_eur'])} (Eurostat {best['period']})",
                        "en": f"Top lane: CN {best['cn']} ({CN_LABEL_EN.get(best['cn'], '')}) FI {verb_en} {best['partner']} "
                              f"{bn(best['value_eur'])} (Eurostat {best['period']})"})
    return {"points": points, "chapters": chapters, "total_eur": total, "period": period, "top_lane": best, "reasons": reasons}
