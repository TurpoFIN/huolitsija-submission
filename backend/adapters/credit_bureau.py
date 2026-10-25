"""Credit signals for a Finnish company: free public registers, plus a paid bureau behind CREDIT_BUREAU.

provider "free" (always):
  ytj        PRH / Vero YTJ open data v3: Business ID status, trade register status, end date, company situations
             (KONK bankruptcy, SELTILA liquidation, SANE restructuring), registration age, VAT / employer / prepayment
             register entries, main business line. https://avoindata.prh.fi/opendata-ytj-api/v3/companies?businessId=
  tax_debt   Vero tax-debt register: no public machine endpoint (see tax_debt()); listed as not_available.
  financials PRH digital financial statements, XBRL open data v3 (see financials()): profit/loss, equity, turnover.
  Insolvency: the Oikeusrekisterikeskus bankruptcy and restructuring register has no free machine-readable endpoint;
  PRH records bankruptcy, liquidation and restructuring as YTJ company situations, which `ytj` reads.

provider "sandbox": reserved test Business IDs 399xxxx-x (TEST_TENANT_SANDBOX, default on, like s2's sandbox registers).

paid bureau (CREDIT_BUREAU=creditsafe|asiakastieto, default none): Creditsafe Connect API v1 (production base URL default)
and Asiakastieto; see Creditsafe / Asiakastieto below. Their rating and limit override the free score when present.

Every reason is {code, fi, en, source, url, points}: the score is 100 plus the sum of the points.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date

import httpx

from core import config, recorded_http
from core.refs import ytunnus_valid
from core.util import now_iso

log = logging.getLogger("credit_bureau")

UA = {"User-Agent": "Huolitsija credit/1.0 (+https://huolitsija.fi)", "Accept": "application/json"}
YTJ_URL = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies"
YTJ_SOURCE = "PRH/Vero YTJ open data API v3"
SITUATION_TEXT = {"KONK": ("konkurssissa", "in bankruptcy"), "SELTILA": ("selvitystilassa", "in liquidation"),
                  "SANE": ("yrityssaneerauksessa", "in corporate restructuring")}
RATINGS = ("A", "B", "C", "D", "decline")


class SourceError(Exception):
    pass


def normalise_bid(v: str | None) -> str | None:
    s = re.sub(r"\s", "", str(v or "")).upper()
    s = s[2:] if s.startswith("FI") else s
    m = re.fullmatch(r"([0-9]{6,7})-?([0-9])", s)
    if not m:
        return None
    bid = f"{m.group(1).rjust(7, '0')}-{m.group(2)}"
    return bid if ytunnus_valid(bid) else None


def is_sandbox_bid(bid: str | None) -> bool:
    return bool(bid and bid.startswith("399") and config.env_bool("TEST_TENANT_SANDBOX", True))


def reason(code: str, fi: str, en: str, points: int = 0, source: str | None = None, url: str | None = None) -> dict:
    return {"code": code, "fi": fi, "en": en, "points": points, "source": source, "url": url}


def _eur(cents: int) -> tuple[str, str]:
    s = f"{(cents or 0) / 100:,.0f}"
    return f"{s.replace(',', ' ')} €", f"€{s}"


def _desc(descs: list | None, lang: str = "3") -> str | None:
    for d in descs or []:
        if d.get("languageCode") == lang:
            return d.get("description")
    return (descs or [{}])[0].get("description") if descs else None


async def _get(url: str, *, timeout: float = 20) -> httpx.Response:
    try:
        return await recorded_http.request("GET", url, headers=UA, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise SourceError(f"{type(exc).__name__}: {str(exc)[:160]}") from exc


# ---------------------------------------------------------------------------------------------
# YTJ (PRH open data v3)
# ---------------------------------------------------------------------------------------------
def parse_ytj(raw: dict, bid: str, url: str) -> dict:
    comps = raw.get("companies") or []
    if not comps:
        return {"found": False, "business_id": bid, "url": url}
    c = comps[0]
    names = c.get("names") or []
    main = next((n for n in names if n.get("type") == "1" and not n.get("endDate")), names[0] if names else {})
    entries = c.get("registeredEntries") or []

    def active(reg: str, typ: str | None = None) -> bool:
        return any(e.get("register") == reg and (typ is None or e.get("type") == typ) and not e.get("endDate") for e in entries)

    def ended(reg: str) -> str | None:
        ends = [e.get("endDate") for e in entries if e.get("register") == reg and e.get("endDate")]
        return max(ends) if ends and not active(reg) else None

    situations = [{"type": s.get("type"), "since": s.get("registrationDate")}
                  for s in c.get("companySituations") or [] if not s.get("endDate")]
    mbl = c.get("mainBusinessLine") or {}
    reg_date = c.get("registrationDate") or ((c.get("businessId") or {}).get("registrationDate"))
    return {
        "found": True, "business_id": (c.get("businessId") or {}).get("value") or bid, "name": main.get("name"),
        "company_form_code": ((c.get("companyForms") or [{}])[0]).get("type"),
        "company_form": _desc(((c.get("companyForms") or [{}])[0]).get("descriptions")),
        "status": c.get("status"), "trade_register_status": c.get("tradeRegisterStatus"),
        "registration_date": reg_date, "end_date": c.get("endDate"),
        "main_business_line": {"code": mbl.get("type"), "text": _desc(mbl.get("descriptions"))},
        "vat_registered": active("6"), "employer_register": active("7"), "prepayment_register": active("5"),
        "trade_register": active("1"), "vat_ended": ended("6"), "prepayment_ended": ended("5"),
        "situations": situations, "last_modified": c.get("lastModified"), "url": url,
    }


async def ytj_fetch(bid: str) -> dict:
    url = f"{YTJ_URL}?businessId={bid}"
    r = await _get(url)
    if r.status_code == 404:
        return {"found": False, "business_id": bid, "url": url, "raw": None}
    if r.status_code != 200:
        raise SourceError(f"HTTP {r.status_code}")
    raw = r.json()
    return {**parse_ytj(raw, bid, url), "raw": raw}


def ytj_reasons(y: dict, today: date | None = None) -> tuple[list[dict], bool]:
    """(reasons, hard_decline)."""
    today = today or date.today()
    u, src = y.get("url"), YTJ_SOURCE
    out: list[dict] = []
    if not y.get("found"):
        return [reason("ytj_not_found", f"{y.get('business_id')} ei löydy YTJ:stä", f"{y.get('business_id')} is not in YTJ",
                       -100, src, u)], True
    hard = False
    if y.get("status") not in ("2", 2):
        out.append(reason("bid_not_valid", "Y-tunnus ei ole voimassa", "The Business ID is not valid (status "
                          f"{y.get('status')})", -100, src, u))
        hard = True
    if y.get("end_date"):
        out.append(reason("ended", f"Yrityksen toiminta päättynyt {y['end_date']}", f"The company ended on {y['end_date']}",
                          -100, src, u))
        hard = True
    if str(y.get("trade_register_status")) in ("2", "4"):
        out.append(reason("trade_register_removed", "Poistettu kaupparekisteristä tai lopettanut",
                          "Removed from the trade register or ceased", -100, src, u))
        hard = True
    for s in y.get("situations") or []:
        fi, en = SITUATION_TEXT.get(s["type"], (s["type"], s["type"]))
        pts = -60 if s["type"] == "SANE" else -100
        hard = hard or s["type"] != "SANE"
        out.append(reason(f"situation_{s['type'].lower()}", f"Yritys on {fi} ({s.get('since') or '—'} alkaen)",
                          f"The company is {en} (since {s.get('since') or '—'})", pts, src, u))
    rd = y.get("registration_date")
    if rd:
        years = (today - date.fromisoformat(rd[:10])).days / 365.25
        if years < 1:
            out.append(reason("age_under_1y", f"Rekisteröity {rd[:10]}: alle vuoden ikäinen", f"Registered {rd[:10]}: "
                              "under one year old", -25, src, u))
        elif years < 3:
            out.append(reason("age_under_3y", f"Rekisteröity {rd[:10]}: alle kolme vuotta vanha",
                              f"Registered {rd[:10]}: under three years old", -10, src, u))
        else:
            out.append(reason("age_ok", f"Rekisteröity {rd[:10]} ({years:.0f} v)", f"Registered {rd[:10]} "
                              f"({years:.0f} years)", 0, src, u))
    if y.get("vat_registered"):
        out.append(reason("vat_ok", "Arvonlisäverorekisterissä", "In the VAT register", 0, src, u))
    else:
        out.append(reason("no_vat", "Ei arvonlisäverorekisterissä" + (f" (poistettu {y['vat_ended']})" if y.get("vat_ended") else ""),
                          "Not in the VAT register" + (f" (removed {y['vat_ended']})" if y.get("vat_ended") else ""),
                          -20 if y.get("vat_ended") else -10, src, u))
    if y.get("prepayment_register"):
        out.append(reason("prepayment_ok", "Ennakkoperintärekisterissä", "In the prepayment register", 0, src, u))
    else:
        # Vero removes a company from the prepayment register when it neglects its tax obligations.
        out.append(reason("no_prepayment", "Ei ennakkoperintärekisterissä" + (f" (poistettu {y['prepayment_ended']})"
                                                                            if y.get("prepayment_ended") else ""),
                          "Not in the prepayment register" + (f" (removed {y['prepayment_ended']})"
                                                             if y.get("prepayment_ended") else ""),
                          -20 if y.get("prepayment_ended") else -10, src, u))
    if y.get("employer_register"):
        out.append(reason("employer_ok", "Työnantajarekisterissä (säännöllinen palkanmaksaja)",
                          "In the employer register (pays salaries regularly)", 0, src, u))
    if (y.get("company_form_code") or "") in ("17",) or "julkinen osakeyhtiö" in (y.get("company_form") or "").lower():
        out.append(reason("public_company", "Julkinen osakeyhtiö (Oyj)", "Public limited company (Oyj)", 5, src, u))
    return out, hard


# ---------------------------------------------------------------------------------------------
# Vero tax-debt register: no machine-readable public endpoint (checked 2026-10-24). vero.fi sends people to the YTJ
# company search; its API (tietopalvelu.ytj.fi/api/TaxDebt/{businessId}) answers 401 without a browser session, so it is
# not called. Vero's own tax-neglect signal reaches us through YTJ: removal from the prepayment register (ytj_reasons).
# A paid bureau (CREDIT_BUREAU) adds payment defaults.
# ---------------------------------------------------------------------------------------------
TAX_DEBT_NOTE = ("Vero tax-debt register has no public machine-readable endpoint; the prepayment-register entry in YTJ is "
                 "used instead and a bureau (CREDIT_BUREAU) adds payment defaults")


async def tax_debt(bid: str) -> dict:
    return {"label": "Vero verovelkarekisteri", "url": "https://www.vero.fi/verovelkarekisteri", "status": "not_available",
            "note": TAX_DEBT_NOTE, "reasons": []}


# ---------------------------------------------------------------------------------------------
# PRH digital financial statements (open data XBRL API v3, iXBRL filings only; most small companies since 2025)
#   list:  https://avoindata.prh.fi/opendata-xbrl-api/v3/financials?businessId=   -> {totalResults, financials[{financialDate}]}
#   one:   https://avoindata.prh.fi/opendata-xbrl-api/v3/financial?businessId=&financialDate=   -> XBRL instance (SBR taxonomy)
# Facts are fi_met:mi53 (amount) with the line item in dimension fi_dim:MCY, member fi_MC:x<code>; codes from the official
# SBR-DPM-2025-12-31 taxonomy (avoindata.suomi.fi): x673 Turnover, x520 Gross profit or loss, x689 Operating profit (loss),
# x740 Profit (loss) for the financial year, x360 Assets, x376 Capital and reserves (equity), x399 Cash in hand and at banks.
# ---------------------------------------------------------------------------------------------
XBRL_URL = "https://avoindata.prh.fi/opendata-xbrl-api/v3"
XBRL_SOURCE = "PRH digital financial statements (XBRL open data v3)"
MC_FIELDS = {"x673": "revenue", "x520": "gross_profit", "x689": "operating_profit", "x740": "net_profit", "x360": "assets",
             "x376": "equity", "x399": "cash"}


def parse_xbrl(xml: str, financial_date: str) -> dict:
    """{field: cents} for the period ending financial_date and {field_prev: cents} for the year before."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    ns_x, ns_d = "{http://www.xbrl.org/2003/instance}", "{http://xbrl.org/2006/xbrldi}"
    ctx = {}
    for c in root.iter(f"{ns_x}context"):
        period = c.find(f"{ns_x}period")
        end = period.findtext(f"{ns_x}instant") or period.findtext(f"{ns_x}endDate") if period is not None else None
        member = None
        for m in c.iter(f"{ns_d}explicitMember"):
            if (m.get("dimension") or "").endswith(":MCY"):
                member = (m.text or "").split(":")[-1]
        ctx[c.get("id")] = (end, member)
    out: dict = {"financial_date": financial_date}
    for el in root:
        if not el.tag.endswith("}mi53") or el.text is None:
            continue
        end, member = ctx.get(el.get("contextRef"), (None, None))
        field = MC_FIELDS.get(member or "")
        if not field or not end:
            continue
        try:
            cents = int(round(float(el.text) * 100))
        except ValueError:
            continue
        key = field if end[:10] == financial_date[:10] else f"{field}_prev"
        out.setdefault(key, cents)
    return out


def financial_reasons(f: dict, url: str) -> tuple[list[dict], bool]:
    out, hard, src = [], False, XBRL_SOURCE
    yr = f["financial_date"][:4]
    net, prev = f.get("net_profit"), f.get("net_profit_prev")
    if net is not None:
        fi, en = _eur(net)
        if net > 0:
            out.append(reason("profit", f"Tilikauden voitto {fi} ({yr})", f"Profit for the year {en} ({yr})", 5, src, url))
        elif net < 0:
            pts = -15 if prev is not None and prev < 0 else -8
            out.append(reason("loss", f"Tilikauden tappio {fi} ({yr})" + (", myös edellisenä vuonna" if pts == -15 else ""),
                              f"Loss for the year {en} ({yr})" + (", also the year before" if pts == -15 else ""), pts, src, url))
    eq = f.get("equity")
    if eq is not None and eq < 0:
        fi, en = _eur(eq)
        out.append(reason("negative_equity", f"Oma pääoma negatiivinen {fi} ({yr})", f"Negative equity {en} ({yr})", -25, src, url))
    rev, rev_prev = f.get("revenue"), f.get("revenue_prev")
    if rev and rev_prev and rev_prev > 0:
        change = (rev - rev_prev) / rev_prev * 100
        if change <= -30:
            out.append(reason("revenue_drop", f"Liikevaihto laski {abs(change):.0f} % ({yr})",
                              f"Turnover fell {abs(change):.0f} % ({yr})", -8, src, url))
    if f.get("cash") is not None and f.get("assets"):
        if f["cash"] / f["assets"] >= 0.3:
            out.append(reason("cash_strong", f"Kassa {f['cash'] / f['assets'] * 100:.0f} % taseesta ({yr})",
                              f"Cash {f['cash'] / f['assets'] * 100:.0f} % of total assets ({yr})", 3, src, url))
    return out, hard


async def financials(bid: str) -> dict:
    list_url = f"{XBRL_URL}/financials?businessId={bid}"
    r = await _get(list_url)
    if r.status_code != 200:
        raise SourceError(f"PRH XBRL list answered HTTP {r.status_code}")
    dates = sorted(x.get("financialDate") for x in (r.json().get("financials") or []) if x.get("financialDate"))
    if not dates:
        return {"label": XBRL_SOURCE, "url": list_url, "status": "none_filed", "reasons": [reason(
            "no_digital_statements", "Ei digitaalisia tilinpäätöksiä PRH:n avoimessa datassa",
            "No digital financial statements in PRH open data", 0, XBRL_SOURCE, list_url)]}
    one_url = f"{XBRL_URL}/financial?businessId={bid}&financialDate={dates[-1]}"
    r = await _get(one_url, timeout=30)
    if r.status_code != 200:
        raise SourceError(f"PRH XBRL statement answered HTTP {r.status_code}")
    try:
        f = parse_xbrl(r.text, dates[-1])
    except Exception as exc:  # noqa: BLE001
        raise SourceError(f"PRH XBRL statement could not be read: {exc}") from exc
    rs, hard = financial_reasons(f, one_url)
    return {"label": XBRL_SOURCE, "url": one_url, "status": "ok", "financial_date": dates[-1], "filings": len(dates),
            "revenue_cents": f.get("revenue"), "figures": f, "reasons": rs, "hard": hard}


# ---------------------------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------------------------
def rating_for(score: int, hard: bool) -> str:
    if hard or score < 40:
        return "decline"
    return "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"


def recommend_limit(rating: str, *, young: bool, settings: dict, revenue_cents: int | None = None,
                    bureau_limit_cents: int | None = None) -> int:
    """A: the auto maximum, B: 40 % of it, C: the default limit, D / decline: 0 (prepayment only). A known revenue caps it at
    2 % of a year's revenue (never below the default); companies under two years old get at most the default."""
    cap = int(settings.get("credit_auto_limit_max_cents") or 5_000_000)
    default = int(settings.get("credit_default_limit_cents") or 500_000)
    if rating in ("D", "decline"):
        return 0
    if bureau_limit_cents is not None:
        return max(0, min(cap, int(bureau_limit_cents)))
    limit = {"A": cap, "B": int(cap * 0.4), "C": default}[rating]
    if revenue_cents:
        limit = min(limit, max(default, int(revenue_cents * 0.02)))
    if young:
        limit = min(limit, default)
    return min(limit, cap)


async def free_check(business_id: str, settings: dict) -> dict:
    """All free signals for one Business ID. Never raises for a source that is down: it is listed with status error."""
    bid = normalise_bid(business_id)
    if not bid:
        return {"provider": "free", "business_id": business_id, "status": "error", "rating": None, "score": None,
                "recommended_limit_cents": 0, "sources": [], "reasons": [reason(
                    "no_business_id", "Y-tunnus puuttuu tai on virheellinen", "Business ID missing or invalid")]}
    if is_sandbox_bid(bid):
        return sandbox_check(bid, settings)
    sources, reasons_, hard, signals = [], [], False, {}
    try:
        y = await ytj_fetch(bid)
        signals["ytj"] = {k: v for k, v in y.items() if k != "raw"}
        sources.append({"name": "ytj", "label": YTJ_SOURCE, "url": y["url"], "status": "ok", "fetched_at": now_iso()})
        rs, hard = ytj_reasons(y)
        reasons_ += rs
    except SourceError as exc:
        sources.append({"name": "ytj", "label": YTJ_SOURCE, "url": f"{YTJ_URL}?businessId={bid}", "status": "error",
                        "error": str(exc), "fetched_at": now_iso()})
    for name, fn in (("tax_debt", tax_debt), ("financials", financials)):
        try:
            res = await fn(bid)
            signals[name] = {k: v for k, v in res.items() if k not in ("raw", "reasons")}
            sources.append({"name": name, "label": res.get("label"), "url": res.get("url"), "status": res.get("status", "ok"),
                            "fetched_at": now_iso()})
            reasons_ += res.get("reasons") or []
            hard = hard or bool(res.get("hard"))
        except SourceError as exc:
            sources.append({"name": name, "status": "error", "error": str(exc), "fetched_at": now_iso()})
    ytj_ok = any(s["name"] == "ytj" and s["status"] == "ok" for s in sources)
    if not ytj_ok:
        return {"provider": "free", "business_id": bid, "status": "error", "rating": None, "score": None,
                "recommended_limit_cents": 0, "sources": sources, "signals": signals, "reasons": reasons_ + [reason(
                    "ytj_unavailable", "YTJ ei vastannut; luottopäätöstä ei tehty", "YTJ did not answer; no credit decision made",
                    0, YTJ_SOURCE, f"{YTJ_URL}?businessId={bid}")]}
    score = max(0, min(100, 100 + sum(r.get("points") or 0 for r in reasons_)))
    rating = rating_for(score, hard)
    rd = (signals.get("ytj") or {}).get("registration_date")
    young = bool(rd and (date.today() - date.fromisoformat(rd[:10])).days < 2 * 365)
    revenue = ((signals.get("financials") or {}).get("revenue_cents"))
    limit = recommend_limit(rating, young=young, settings=settings, revenue_cents=revenue)
    return {"provider": "free", "business_id": bid, "status": "ok", "name": (signals.get("ytj") or {}).get("name"),
            "score": score, "rating": rating, "recommended_limit_cents": limit, "young": young,
            "sources": sources, "signals": signals, "reasons": reasons_}


def sandbox_check(bid: str, settings: dict) -> dict:
    limit = int(settings.get("credit_default_limit_cents") or 500_000)
    return {"provider": "sandbox", "business_id": bid, "status": "ok", "score": 70, "rating": "B",
            "recommended_limit_cents": limit, "sources": [{"name": "sandbox", "label": "Test tenant sandbox register",
                                                           "status": "ok", "url": None, "fetched_at": now_iso()}],
            "signals": {}, "reasons": [reason("sandbox", "Varattu testi-Y-tunnus (399xxxx): testiympäristön rekisteri, "
                                              "oletusluottoraja", "Reserved test Business ID (399xxxx): sandbox register, "
                                              "default credit limit", -30, "sandbox", None)]}


# ---------------------------------------------------------------------------------------------
# Paid bureaus (CREDIT_BUREAU)
# ---------------------------------------------------------------------------------------------
def bureau_name() -> str:
    v = config.env("CREDIT_BUREAU", "none").lower()
    return v if v in ("creditsafe", "asiakastieto") else "none"


class BureauError(Exception):
    pass


class Creditsafe:
    """Creditsafe Connect API v1 (https://connect.sandbox.creditsafe.com/v1 sandbox, https://connect.creditsafe.com/v1 live).
    POST /authenticate {username, password} -> {token} (Bearer, ~1 h); GET /companies?countries=FI&regNo= -> companies[].id;
    GET /companies/{id}?language=en -> report.creditScore.currentCreditRating {commonValue A-E, commonDescription,
    creditLimit {value, currency}, providerValue {value}}."""

    _token: tuple[str, float] | None = None

    def __init__(self):
        self.base = config.env("CREDITSAFE_BASE_URL", "https://connect.creditsafe.com/v1").rstrip("/")
        self.username = config.env("CREDITSAFE_USERNAME")
        self.password = config.env("CREDITSAFE_PASSWORD")

    def configured(self) -> bool:
        return bool(self.username and self.password)

    async def token(self, c: httpx.AsyncClient) -> str:
        if Creditsafe._token and Creditsafe._token[1] > time.time() + 60:
            return Creditsafe._token[0]
        r = await c.post(f"{self.base}/authenticate", json={"username": self.username, "password": self.password})
        if r.status_code != 200 or not (r.json() or {}).get("token"):
            raise BureauError(f"Creditsafe authenticate: HTTP {r.status_code}")
        Creditsafe._token = (r.json()["token"], time.time() + 50 * 60)
        return Creditsafe._token[0]

    async def report(self, bid: str) -> dict:
        if not self.configured():
            raise BureauError("CREDITSAFE_USERNAME / CREDITSAFE_PASSWORD not set")
        async with httpx.AsyncClient(timeout=40, headers=UA) as c:
            h = {"Authorization": f"Bearer {await self.token(c)}"}
            r = await c.get(f"{self.base}/companies", params={"countries": "FI", "regNo": bid}, headers=h)
            if r.status_code != 200:
                raise BureauError(f"Creditsafe company search: HTTP {r.status_code}")
            comps = (r.json() or {}).get("companies") or []
            if not comps:
                return {"found": False, "provider": "creditsafe", "url": f"{self.base}/companies?countries=FI&regNo={bid}"}
            cid = comps[0].get("id") or comps[0].get("connectId")
            rr = await c.get(f"{self.base}/companies/{cid}", params={"language": "en"}, headers=h)
            if rr.status_code != 200:
                raise BureauError(f"Creditsafe credit report: HTTP {rr.status_code}")
            return {"found": True, "provider": "creditsafe", "connect_id": cid, **parse_creditsafe(rr.json() or {}),
                    "url": f"{self.base}/companies/{cid}"}


def parse_creditsafe(body: dict) -> dict:
    rep = body.get("report") or body
    cur = ((rep.get("creditScore") or {}).get("currentCreditRating")) or {}
    common = (cur.get("commonValue") or "").upper() or None
    lim = cur.get("creditLimit") or {}
    try:
        limit_cents = int(round(float(str(lim.get("value")).replace(",", "")) * 100)) if lim.get("value") not in (None, "") else None
    except ValueError:
        limit_cents = None
    return {"common_value": common, "description": cur.get("commonDescription"),
            "provider_value": (cur.get("providerValue") or {}).get("value"), "limit_cents": limit_cents,
            "limit_currency": lim.get("currency"),
            "rating": {"A": "A", "B": "B", "C": "C", "D": "D", "E": "decline"}.get(common or "", None)}


class Asiakastieto:
    """Asiakastieto (Enento) company credit report web service. Endpoint, credentials and query parameters come from the
    customer contract: ASIAKASTIETO_BASE_URL, ASIAKASTIETO_USER_ID, ASIAKASTIETO_PASSWORD (see notes/s7-credit.md)."""

    def __init__(self):
        self.base = config.env("ASIAKASTIETO_BASE_URL")
        self.user = config.env("ASIAKASTIETO_USER_ID")
        self.password = config.env("ASIAKASTIETO_PASSWORD")

    def configured(self) -> bool:
        return bool(self.base and self.user and self.password)

    async def report(self, bid: str) -> dict:
        if not self.configured():
            raise BureauError("ASIAKASTIETO_BASE_URL / ASIAKASTIETO_USER_ID / ASIAKASTIETO_PASSWORD not set")
        raise BureauError("Asiakastieto client needs the contract's web-service specification")


async def bureau_check(bid: str) -> dict | None:
    name = bureau_name()
    if name == "none":
        return None
    client = Creditsafe() if name == "creditsafe" else Asiakastieto()
    try:
        return await client.report(bid)
    except (BureauError, httpx.HTTPError) as exc:
        return {"provider": name, "error": str(exc)[:200], "found": None}
