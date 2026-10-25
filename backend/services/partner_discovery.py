"""Partner discovery for the Sourcing Autopilot (Hankinta-autopilotti): who could carry this lane for us?

Public sources only, each candidate keeps the URL it came from:
- PRH/YTJ open data (Finnish trade register): companies by main line of business (TOL 2008) in Finnish freight hubs.
  Business ID, legal form, registration date and the website the company registered itself.
- Wikidata: transport and logistics companies in the lane's foreign country with an official website.
- Huolitsija's own carrier directory (carriers collection).

B2B only: a candidate needs a business ID from a register or a company web domain, must be a legal person (no sole
traders: they are natural persons and need prior consent for e-mail marketing, Laki sähköisen viestinnän palveluista
200 §) and is dropped when its domain is a free-mail provider. Candidates are deduplicated by business ID, then web
domain, then normalised name, and scored with the reasons shown to the operator.
"""
import asyncio
import logging
import re
import unicodedata
import uuid
from datetime import date, timedelta
from urllib.parse import quote, urlparse

import httpx

from core.db import db, register_index
from core.util import NO_ID, now_iso

log = logging.getLogger("partner_discovery")
register_index("partner_candidates", "id", unique=True)
register_index("partner_candidates", [("lanes", 1), ("score", -1)])
register_index("partner_discovery_runs", "lane", unique=True)

YTJ_URL = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies"
YTJ_PAGE = "https://tietopalvelu.ytj.fi/yritys/{}"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "Huolitsija partner sourcing/1.0 (B2B freight RFQs; https://huolitsija-app.emergent.host)"
REFRESH_DAYS = 7
CONTACT_LOOKUPS_PER_LANE = 12

# TOL 2008 main lines of business -> (role, modes)
TOL = {
    "49410": ("haulier", {"road"}),      # Tieliikenteen tavarankuljetus / road freight
    "52291": ("agent", {"road", "sea", "air", "rail"}),  # Huolinta ja rahtaus / freight forwarding
    "50200": ("carrier", {"sea"}),       # Meri- ja rannikkovesiliikenteen tavarankuljetus
    "51210": ("carrier", {"air"}),       # Lentoliikenteen tavarankuljetus
    "49200": ("carrier", {"rail"}),      # Rautateiden tavaraliikenne
    "52241": ("agent", {"sea"}),         # Satamien lastinkäsittely / port cargo handling
}
FI_HUBS = ["Helsinki", "Vantaa", "Espoo", "Kerava", "Turku", "Naantali", "Tampere", "Kotka", "Hamina", "Lahti",
           "Kouvola", "Oulu", "Rauma", "Pori", "Jyväskylä", "Kuopio", "Tornio"]
COUNTRY_QID = {"SE": "Q34", "EE": "Q191", "DE": "Q183", "PL": "Q36", "LT": "Q37", "LV": "Q211", "NO": "Q20", "DK": "Q35",
               "NL": "Q55", "BE": "Q31", "FR": "Q142", "IT": "Q38", "ES": "Q29", "CZ": "Q213", "AT": "Q40", "CN": "Q148",
               "US": "Q30", "GB": "Q145", "FI": "Q33", "HU": "Q28", "SK": "Q214", "TR": "Q43", "IN": "Q668", "VN": "Q881"}
# Wikidata industries / instance-of classes: logistics, freight transport, freight transport industry, road freight,
# trucking industry, freight forwarder, shipping company, cargo airline
WD_CLASSES = {"Q177777": ("agent", {"road", "sea", "air", "rail"}), "Q651658": ("carrier", {"road", "sea", "rail"}),
              "Q57615173": ("carrier", {"road", "sea", "air", "rail"}), "Q29585624": ("haulier", {"road"}),
              "Q83698112": ("haulier", {"road"}), "Q840850": ("agent", {"road", "sea", "air", "rail"}),
              "Q2401749": ("carrier", {"sea"}), "Q1057312": ("carrier", {"air"})}
# Classes that say which mode a company runs; "logistics" / "freight transport" alone also match software and
# distribution firms, so they only earn the weak mode score.
WD_SPECIFIC = {"Q29585624", "Q83698112", "Q840850", "Q2401749", "Q1057312"}
FREEMAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "icloud.com",
            "me.com", "aol.com", "protonmail.com", "proton.me", "gmx.com", "gmx.de", "web.de", "mail.ru", "yandex.ru",
            "suomi24.fi", "luukku.com", "kolumbus.fi", "elisanet.fi", "welho.com", "saunalahti.fi", "pp.inet.fi",
            "netti.fi", "dnainternet.net", "telia.com", "wp.pl", "o2.pl", "seznam.cz", "qq.com", "163.com"}
ROLE_ADDRESS = re.compile(r"^(info|myynti|sales|tilaukset|tilaus|orders?|booking|rahti|freight|cargo|logistiikka|logistics|"
                          r"asiakaspalvelu|customer|service|tarjous|tarjoukset|quotes?|office|toimisto|ajojarjestely|"
                          r"dispo|traffic|liikenne|export|import|kuljetus|transport)", re.I)
NOT_A_COMPANY_SITE = re.compile(r"facebook\.com|linkedin\.com|instagram\.com|wikipedia\.org|google\.|yritys\.|finder\.fi|"
                                r"fonecta|kauppalehti|asiakastieto|youtube\.com|twitter\.com|x\.com", re.I)


def base_domain(host_or_url: str) -> str:
    host = urlparse(host_or_url if "//" in host_or_url else "https://" + host_or_url).hostname or ""
    parts = host.lower().removeprefix("www.").split(".")
    two_level = {"co.uk", "com.cn", "com.pl", "co.jp", "com.tr", "com.au"}
    if len(parts) >= 3 and ".".join(parts[-2:]) in two_level:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""


def normalise_name(name: str) -> str:
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(oy|oyj|ab|ky|ay|ltd|limited|gmbh|as|a/s|sp\.? z o\.?o\.?|sia|uab|inc|llc|group|finland|suomi)\b", " ", n)
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def candidate_id(business_id: str | None, domain: str | None, name: str) -> str:
    key = f"bid:{business_id}" if business_id else f"dom:{domain}" if domain else f"name:{normalise_name(name)}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"huolitsija-partner-{key}"))


def b2b_check(cand: dict) -> tuple[bool, str]:
    domain = cand.get("domain")
    if domain and domain in FREEMAIL:
        return False, f"free-mail domain {domain}"
    if cand.get("legal_form_ok") is False:
        return False, f"not a legal person ({cand.get('legal_form')})"
    if not cand.get("business_id") and not domain:
        return False, "no business ID and no company domain"
    return True, "business ID" if cand.get("business_id") else "company domain"


def email_is_b2b(email: str, cand: dict) -> tuple[bool, str]:
    """A recipient must be on the company's own domain (never free-mail) and look like a business address."""
    email = (email or "").lower().strip()
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", email):
        return False, "not an e-mail address"
    dom = base_domain(email.split("@")[1])
    if dom in FREEMAIL:
        return False, f"free-mail address ({dom})"
    if cand.get("domain") and dom != cand["domain"]:
        return False, f"address domain {dom} is not the company domain {cand['domain']}"
    return True, "company-domain address"


def _website(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if NOT_A_COMPANY_SITE.search(url):
        return None
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    host = urlparse(url).hostname
    if not host or "." not in host:
        return None
    return f"https://{host}/"


def _lane(lane: str) -> tuple[str, str, str]:
    mode, countries = lane.split(":")
    origin, destination = countries.split(">")
    return mode, origin, destination


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})


def _en(descriptions: list, default="") -> str:
    for d in descriptions or []:
        if d.get("languageCode") == "3":
            return d.get("description") or default
    return default


async def ytj_candidates(mode: str, *, client: httpx.AsyncClient, hubs: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """Finnish companies from the trade register for this mode. Returns (candidates, notes)."""
    out, notes = [], []
    sem = asyncio.Semaphore(2)  # the open-data API answers 429 to bursts
    codes = [c for c, (_, modes) in TOL.items() if mode in modes]

    async def one(code: str, city: str):
        async with sem:
            for attempt in range(4):
                try:
                    r = await client.get(YTJ_URL, params={"mainBusinessLine": code, "location": city})
                except httpx.TransportError as exc:
                    if attempt < 3:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    notes.append(f"YTJ {code} {city}: {type(exc).__name__}")
                    return []
                except (httpx.HTTPError, ValueError) as exc:
                    notes.append(f"YTJ {code} {city}: {type(exc).__name__}")
                    return []
                if r.status_code == 429 and attempt < 3:
                    await asyncio.sleep(float(r.headers.get("retry-after") or 0) or 2 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    notes.append(f"YTJ {code} {city}: HTTP {r.status_code}")
                    return []
                await asyncio.sleep(0.3)
                return [(code, c) for c in r.json().get("companies") or []]
            return []

    batches = await asyncio.gather(*(one(code, city) for code in codes for city in (hubs or FI_HUBS)))
    for code, c in (x for batch in batches for x in batch):
        bid = (c.get("businessId") or {}).get("value")
        site = _website((c.get("website") or {}).get("url"))
        if not bid or c.get("tradeRegisterStatus") not in (None, "1") or c.get("companySituations"):
            continue
        name = next((n["name"] for n in c.get("names") or [] if not n.get("endDate") and n.get("type") == "1"), None)
        if not name:
            continue
        forms = [_en(f.get("descriptions")) for f in c.get("companyForms") or [] if not f.get("endDate")]
        form = forms[0] if forms else ""
        city = next((po.get("city") for a in c.get("addresses") or [] for po in a.get("postOffices") or []
                     if po.get("languageCode") == "1"), None)
        role, modes = TOL[code]
        out.append({
            "name": name, "business_id": bid, "country": "FI", "city": city.title() if city else None,
            "website": site, "domain": base_domain(site) if site else None, "roles": [role], "modes": sorted(modes),
            "legal_form": form, "legal_form_ok": not re.search(r"private trader|sole|elinkeinonharjoittaja", form, re.I) and bool(form),
            "registered": (c.get("businessId") or {}).get("registrationDate"),
            "sources": [{"source": "ytj", "url": YTJ_PAGE.format(bid), "api": f"{YTJ_URL}?mainBusinessLine={code}",
                         "detail": f"TOL {code} {_en((c.get('mainBusinessLine') or {}).get('descriptions'))}"}],
        })
    return out, notes


async def wikidata_candidates(country: str, mode: str, *, client: httpx.AsyncClient) -> tuple[list[dict], list[str]]:
    qid = COUNTRY_QID.get(country)
    if not qid:
        return [], [f"Wikidata: no country mapping for {country}"]
    classes = " ".join(f"wd:{q}" for q, (_, modes) in WD_CLASSES.items() if mode in modes)
    query = (f"SELECT DISTINCT ?c ?cLabel ?web ?cls WHERE {{ VALUES ?cls {{ {classes} }} {{ ?c wdt:P452 ?cls }} UNION "
             f"{{ ?c wdt:P31 ?cls }} ?c wdt:P17 wd:{qid}; wdt:P856 ?web. FILTER NOT EXISTS {{ ?c wdt:P576 ?end }} "
             f"SERVICE wikibase:label {{ bd:serviceParam wikibase:language \"en,{country.lower()}\". }} }} LIMIT 200")
    try:
        r = await client.get(WIKIDATA_SPARQL, params={"query": query}, headers={"Accept": "application/sparql-results+json"})
        if r.status_code != 200:
            return [], [f"Wikidata {country}: HTTP {r.status_code}"]
        rows = r.json()["results"]["bindings"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return [], [f"Wikidata {country}: {type(exc).__name__}"]
    out = []
    for row in rows:
        site = _website(row["web"]["value"])
        name = row.get("cLabel", {}).get("value") or ""
        entity = row["c"]["value"]
        if not site or re.fullmatch(r"Q\d+", name):
            continue
        cls = row["cls"]["value"].rsplit("/", 1)[-1]
        role, modes = WD_CLASSES.get(cls, ("carrier", {mode}))
        out.append({"name": name, "business_id": None, "country": country, "website": site, "domain": base_domain(site),
                    "roles": [role], "modes": sorted(modes), "legal_form": "organisation (Wikidata)", "legal_form_ok": True,
                    "mode_specific": cls in WD_SPECIFIC,
                    "wikidata": entity.rsplit("/", 1)[-1],
                    "sources": [{"source": "wikidata", "url": entity.replace("http://", "https://"),
                                 "detail": f"official website {row['web']['value']}"}]})
    return out, []


async def directory_candidates(mode: str) -> list[dict]:
    out = []
    async for c in db.carriers.find({"active": {"$ne": False}, "modes": mode, "website": {"$regex": "^https://"}}, NO_ID):
        site = _website(c.get("website"))
        out.append({"name": c["name"], "business_id": c.get("business_id"), "country": c.get("country"),
                    "website": site, "domain": base_domain(site) if site else None, "roles": ["carrier"],
                    "modes": c.get("modes") or [mode], "legal_form": "carrier directory", "legal_form_ok": True,
                    "carrier_id": c["id"], "contact_email": c.get("contact_email"),
                    "sources": [{"source": "directory", "url": c.get("website"), "detail": "Huolitsija carrier directory"}]})
    return out


def merge(cands: list[dict]) -> list[dict]:
    """Dedupe by business ID, then domain, then normalised name; sources, roles and modes are united."""
    by_key: dict[str, dict] = {}
    alias: dict[str, str] = {}
    for c in cands:
        keys = [k for k in (f"bid:{c['business_id']}" if c.get("business_id") else None,
                            f"dom:{c['domain']}" if c.get("domain") else None,
                            f"name:{normalise_name(c['name'])}" if normalise_name(c["name"]) else None) if k]
        hit = next((alias[k] for k in keys if k in alias), None)
        if hit is None:
            hit = keys[0]
            by_key[hit] = {**c, "sources": list(c["sources"]), "roles": list(c["roles"]), "modes": list(c["modes"])}
        else:
            m = by_key[hit]
            seen = {(s["source"], s["url"]) for s in m["sources"]}
            m["sources"] += [s for s in c["sources"] if (s["source"], s["url"]) not in seen]
            m["roles"] = sorted(set(m["roles"]) | set(c["roles"]))
            m["modes"] = sorted(set(m["modes"]) | set(c["modes"]))
            m["mode_specific"] = m.get("mode_specific", True) or c.get("mode_specific", True)
            for f in ("business_id", "website", "domain", "registered", "city", "carrier_id", "contact_email", "wikidata"):
                if not m.get(f) and c.get(f):
                    m[f] = c[f]
            if c.get("legal_form_ok") is False:
                m["legal_form_ok"], m["legal_form"] = False, c.get("legal_form")
        for k in keys:
            alias.setdefault(k, hit)
    return list(by_key.values())


def score(c: dict, lane: str, today: date | None = None) -> tuple[int, list[str]]:
    mode, origin, destination = _lane(lane)
    today = today or date.today()
    pts, why = 0, []
    if mode in c.get("modes", []) and c.get("mode_specific", True):
        pts += 30; why.append(f"+30 operates {mode}")
    elif mode in c.get("modes", []):
        pts += 10; why.append("+10 general logistics (mode not stated)")
    if c.get("country") in (origin, destination):
        pts += 15; why.append(f"+15 based in {c['country']} (lane end)")
    want = {"road": "haulier", "sea": "carrier", "air": "carrier", "rail": "carrier"}[mode]
    if want in c.get("roles", []):
        pts += 10; why.append(f"+10 {want}")
    elif "agent" in c.get("roles", []) and c.get("country") == destination != "FI":
        pts += 10; why.append("+10 agent at destination")
    elif "agent" in c.get("roles", []):
        pts += 5; why.append("+5 forwarder/agent")
    if c.get("domain"):
        pts += 10; why.append("+10 company website")
    if c.get("business_id"):
        pts += 10; why.append("+10 business ID in trade register")
    reg = c.get("registered")
    if reg:
        try:
            years = (today - date.fromisoformat(reg[:10])).days // 365
            if years >= 5:
                pts += 10; why.append(f"+10 registered {years} years ago")
        except ValueError:
            pass
    if len({s["source"] for s in c.get("sources", [])}) > 1:
        pts += 5; why.append("+5 found in several sources")
    if c.get("contact_email"):
        local = c["contact_email"].split("@")[0]
        if ROLE_ADDRESS.search(local):
            pts += 10; why.append("+10 published role address")
        else:
            pts += 5; why.append("+5 published personal business address")
    if c.get("declined_count"):
        pts -= 10 * c["declined_count"]; why.append(f"-{10 * c['declined_count']} declined before")
    return max(0, min(100, pts)), why


async def discover_lane(lane: str, *, force: bool = False, contact_lookup=None, hubs: list[str] | None = None) -> dict:
    """Refresh candidates for one lane (cached REFRESH_DAYS). contact_lookup(candidate) -> (email|None, evidence)."""
    run = await db.partner_discovery_runs.find_one({"lane": lane}, NO_ID)
    if run and not force and run.get("at", "") >= (date.today() - timedelta(days=REFRESH_DAYS)).isoformat():
        return {"lane": lane, "status": "cached", **{k: run.get(k) for k in ("found", "eligible", "at")}}
    mode, origin, destination = _lane(lane)
    notes: list[str] = []
    found: list[dict] = []
    async with _client() as client:
        tasks = []
        if "FI" in (origin, destination):
            tasks.append(ytj_candidates(mode, client=client, hubs=hubs))
        for country in {origin, destination} - {"FI"}:
            tasks.append(wikidata_candidates(country, mode, client=client))
        for cands, n in await asyncio.gather(*tasks):
            found += cands
            notes += n
    found += await directory_candidates(mode)
    merged = merge(found)
    without_site = sum(1 for c in merged if not c.get("website"))
    if without_site:
        notes.append(f"{without_site} register entries without a website skipped (no public contact route)")
    merged = [c for c in merged if c.get("website")]
    existing = {c["id"]: c async for c in db.partner_candidates.find({"id": {"$in": [
        candidate_id(c.get("business_id"), c.get("domain"), c["name"]) for c in merged]}}, NO_ID)}
    optout_domains = {o["domain"] async for o in db.partner_optouts.find({"domain": {"$ne": None}}, {"_id": 0, "domain": 1})}
    rows = []
    for c in merged:
        cid = candidate_id(c.get("business_id"), c.get("domain"), c["name"])
        prev = existing.get(cid) or {}
        c.update(id=cid, declined_count=prev.get("declined_count", 0),
                 contact_email=prev.get("contact_email") or c.get("contact_email"),
                 contact_source_url=prev.get("contact_source_url"))
        ok, why = b2b_check(c)
        c["b2b"] = {"ok": ok, "basis": why}
        if c.get("domain") in optout_domains or prev.get("status") == "opted_out":
            c["status"] = "opted_out"
        elif not ok:
            c["status"] = "excluded"
        else:
            c["status"] = prev.get("status") if prev.get("status") in ("contacted", "partner", "declined") else "new"
        c["score"], c["score_reasons"] = score(c, lane)
        rows.append(c)
    # Contact lookup for the best eligible candidates without a known address (bounded: websites are slow).
    if contact_lookup:
        open_ = sorted([c for c in rows if c["status"] == "new" and c.get("website") and not c.get("contact_email")
                        and not prev_checked(existing.get(c["id"]))], key=lambda c: -c["score"])
        foreign = [c for c in open_ if c.get("country") != "FI"][:CONTACT_LOOKUPS_PER_LANE // 3]
        todo = foreign + [c for c in open_ if c not in foreign][:CONTACT_LOOKUPS_PER_LANE - len(foreign)]
        sem = asyncio.Semaphore(4)

        async def look(c):
            async with sem:
                try:
                    email, evidence = await contact_lookup(c)
                except Exception as exc:  # noqa: BLE001 - a broken website must not stop discovery
                    email, evidence = None, f"lookup failed: {type(exc).__name__}"
            c["contact_checked_at"] = now_iso()
            if email:
                ok, why = email_is_b2b(email, c)
                if ok:
                    c["contact_email"], c["contact_source_url"] = email.lower(), evidence
                else:
                    c["contact_note"] = f"{email}: {why}"
            else:
                c["contact_note"] = evidence[:500]
            c["score"], c["score_reasons"] = score(c, lane)
        await asyncio.gather(*(look(c) for c in todo))
    for c in rows:
        c["status"] = "contactable" if c["status"] == "new" and c.get("contact_email") else c["status"]
        doc = {k: v for k, v in c.items() if k not in ("lanes",)}
        doc["updated_at"] = now_iso()
        await db.partner_candidates.update_one({"id": c["id"]}, {"$set": doc, "$addToSet": {"lanes": lane},
                                                                 "$setOnInsert": {"created_at": now_iso()}}, upsert=True)
    summary = {"lane": lane, "found": len(rows), "eligible": sum(1 for c in rows if c["status"] in ("new", "contactable")),
               "contactable": sum(1 for c in rows if c["status"] == "contactable"), "notes": notes[:20], "at": now_iso()}
    await db.partner_discovery_runs.update_one({"lane": lane}, {"$set": summary}, upsert=True)
    return {"status": "ran", **summary}


def prev_checked(prev: dict | None) -> bool:
    """A website that published nothing is re-read only after REFRESH_DAYS."""
    at = (prev or {}).get("contact_checked_at")
    return bool(at) and at >= (date.today() - timedelta(days=REFRESH_DAYS)).isoformat()


def wikidata_url(qid: str) -> str:
    return f"https://www.wikidata.org/wiki/{quote(qid)}"
