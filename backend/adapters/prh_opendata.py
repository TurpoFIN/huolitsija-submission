"""PRH / YTJ open data v3 (Finnish trade register), company search for B2B prospecting.

    GET https://avoindata.prh.fi/opendata-ytj-api/v3/companies?mainBusinessLine=4669&page=1

Search by main line of business, 4-digit group (5-digit sub-classes return nothing). What the live API does (probed
2026-10-24, see backend/tests/fixtures/s7_crm_prh_*.json):
- Active companies carry TOL 2025 codes (`typeCodeSet: "TOIMI4"`, recoded 2026-01-01). A TOL 2008 group whose number
  changed (4669, 4673, 4675, 3109 ...) now returns almost only ceased companies, so TOL2008_TO_2025 translates first.
- The filter is a substring match: "3100" also returns 63100 and 13100. `matches()` keeps only records whose code starts
  with the group and is in the current code set.
- `location` (town, any address of the company), `postCode` and `registrationDateStart` filter; `companyForm` is ignored.
- 100 records per page, ordered by registration (oldest first), `totalResults` for paging.
Polite client: own User-Agent, one request per second, 429 answered with back-off.
"""
import asyncio
import logging
import re
import time
from urllib.parse import urlencode, urlparse

import httpx

log = logging.getLogger("prh_opendata")

YTJ_URL = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies"
YTJ_PAGE = "https://tietopalvelu.ytj.fi/yritys/{}"
UA = {"User-Agent": "Huolitsija CRM/1.0 (B2B prospecting; +https://huolitsija.fi)", "Accept": "application/json"}
PAGE_SIZE = 100
MIN_INTERVAL_S = 1.0
# companySituations: restructuring, liquidation, bankruptcy (any active one excludes the company)
SITUATIONS = {"SANE": "restructuring", "SELTILA": "liquidation", "KONK": "bankruptcy"}
SOLE_TRADER = re.compile(r"elinkeinonharjoittaja|private trader|sole trader|enskild", re.I)
NOT_A_COMPANY_SITE = re.compile(r"facebook\.|linkedin\.|instagram\.|wikipedia\.|google\.|youtube\.|twitter\.|x\.com|"
                                r"fonecta|finder\.fi|kauppalehti|asiakastieto|yritys\.", re.I)

CURRENT_CODE_SET = "TOIMI4"
# TOL 2008 group -> TOL 2025 group where the number changed (wholesale and furniture; manufacturing mostly unchanged)
TOL2008_TO_2025 = {"4669": "4664", "4673": "4683", "4674": "4684", "4675": "4685", "4676": "4686", "4672": "4682",
                   "4651": "4650", "4652": "4650", "3101": "3100", "3102": "3100", "3103": "3100", "3109": "3100"}

_lock = asyncio.Lock()
_last = 0.0


class PRHUnavailable(Exception):
    pass


def search_url(params: dict) -> str:
    return f"{YTJ_URL}?{urlencode({k: v for k, v in params.items() if v not in (None, '')})}"


async def _polite_get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    global _last
    for attempt in range(4):
        async with _lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - _last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await client.get(url)
            except httpx.TransportError as exc:
                _last = time.monotonic()
                if attempt == 3:
                    raise PRHUnavailable(f"PRH open data did not answer ({type(exc).__name__})") from exc
                await asyncio.sleep(2 * (attempt + 1))
                continue
            _last = time.monotonic()
        if r.status_code == 429 and attempt < 3:
            await asyncio.sleep(float(r.headers.get("retry-after") or 0) or 3 * (attempt + 1))
            continue
        return r
    return r


async def search(main_business_line: str, *, page: int = 1, location: str | None = None, post_code: str | None = None,
                 registration_date_start: str | None = None, client: httpx.AsyncClient | None = None) -> dict:
    """One page of companies. -> {url, total, page, companies (raw v3 records), fetched_at}."""
    if location and re.fullmatch(r"\d{5}", location.strip()):
        location, post_code = None, location.strip()
    params = {"mainBusinessLine": main_business_line, "location": location, "postCode": post_code,
              "registrationDateStart": registration_date_start, "page": page if page > 1 else None}
    url = search_url(params)
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, headers=UA)
    try:
        r = await _polite_get(client, url)
    finally:
        if own:
            await client.aclose()
    if r.status_code == 404:
        return {"url": url, "total": 0, "page": page, "companies": []}
    if r.status_code != 200:
        raise PRHUnavailable(f"PRH open data answered HTTP {r.status_code} for {url}")
    data = r.json()
    return {"url": url, "total": int(data.get("totalResults") or 0), "page": page, "companies": data.get("companies") or []}


def current_code(code: str) -> str:
    code = str(code)[:4]
    return TOL2008_TO_2025.get(code, code)


def matches(c: dict, group: str) -> bool:
    mbl = c.get("mainBusinessLine") or {}
    return str(mbl.get("type") or "").startswith(group) and mbl.get("typeCodeSet") == CURRENT_CODE_SET


def _desc(descs: list | None, lang: str) -> str | None:
    for d in descs or []:
        if d.get("languageCode") == lang:
            return d.get("description")
    return None


def website(raw_url: str | None) -> str | None:
    """The website the company registered itself, as https://host/ (never guessed)."""
    if not raw_url:
        return None
    url = raw_url.strip()
    if NOT_A_COMPANY_SITE.search(url):
        return None
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    host = (urlparse(url).hostname or "").lower()
    if not host or "." not in host or " " in host:
        return None
    return f"https://{host}/"


def base_domain(host_or_url: str) -> str:
    host = urlparse(host_or_url if "//" in host_or_url else "https://" + host_or_url).hostname or ""
    parts = host.lower().removeprefix("www.").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in {"co.uk", "com.cn", "com.pl", "co.jp", "com.tr", "com.au"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""


def skip_reason(c: dict) -> str | None:
    """Why a register record is not a prospect (None = keep)."""
    bid = (c.get("businessId") or {}).get("value")
    if not bid:
        return "no business ID"
    if c.get("endDate"):
        return f"company ended {c['endDate']}"
    if str(c.get("status") or "") not in ("", "2"):
        return f"business ID status {c.get('status')} (not valid)"
    if str(c.get("tradeRegisterStatus") or "1") not in ("1",):
        return f"trade register status {c.get('tradeRegisterStatus')} (not registered)"
    names = c.get("names") or []
    main = [n for n in names if str(n.get("type")) == "1"]
    if main and all(n.get("endDate") for n in main):
        return "main name has an end date"
    if not any(not n.get("endDate") for n in main):
        return "no current main name"
    active = [s for s in c.get("companySituations") or [] if not s.get("endDate")]
    if active:
        kinds = ", ".join(SITUATIONS.get(s.get("type"), s.get("type") or "?") for s in active)
        return f"in {kinds}"
    form = " ".join(_desc(f.get("descriptions"), "3") or _desc(f.get("descriptions"), "1") or ""
                    for f in c.get("companyForms") or [] if not f.get("endDate"))
    if SOLE_TRADER.search(form):
        return "sole trader (natural person: B2C consent rules apply)"
    return None


def normalise(c: dict, tol: str, api_url: str) -> dict:
    bid = c["businessId"]["value"]
    names = c.get("names") or []
    name = next((n["name"] for n in names if str(n.get("type")) == "1" and not n.get("endDate")), None)
    forms = [f for f in c.get("companyForms") or [] if not f.get("endDate")]
    form_fi = _desc(forms[0].get("descriptions"), "1") if forms else None
    form_en = _desc(forms[0].get("descriptions"), "3") if forms else None
    addrs = c.get("addresses") or []
    addr = next((a for a in addrs if a.get("type") == 1), None) or next((a for a in addrs if a.get("type") == 2), None) or {}
    city = next((p.get("city") for p in addr.get("postOffices") or [] if p.get("languageCode") == "1"), None)
    street = " ".join(str(x) for x in [addr.get("street"), addr.get("buildingNumber"), addr.get("entrance")] if x) or None
    mbl = c.get("mainBusinessLine") or {}
    site = website((c.get("website") or {}).get("url"))
    entries = c.get("registeredEntries") or []
    vat = any(str(e.get("register")) == "6" and not e.get("endDate") for e in entries)
    employer = any(str(e.get("register")) == "7" and not e.get("endDate") for e in entries)
    return {
        "name": name, "business_id": bid, "country": "FI", "company_form": form_en or form_fi, "company_form_fi": form_fi,
        "tol_code": str(mbl.get("type") or tol)[:4], "tol_class": str(mbl.get("type") or ""),
        "tol_code_set": mbl.get("typeCodeSet"), "tol_label": _desc(mbl.get("descriptions"), "3"),
        "tol_label_fi": _desc(mbl.get("descriptions"), "1"), "registered": (c.get("businessId") or {}).get("registrationDate")
        or c.get("registrationDate"), "website": site, "domain": base_domain(site) if site else None,
        "address": {"street": street, "postal_code": addr.get("postCode"), "city": city.title() if city else None, "country": "FI"},
        "vat_registered": vat, "employer_register": employer, "last_modified": c.get("lastModified"),
        "source": {"source": "PRH/YTJ open data v3", "api_url": api_url, "url": YTJ_PAGE.format(bid)},
    }
