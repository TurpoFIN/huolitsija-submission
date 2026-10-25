"""Politically exposed persons (PEP) and their family members (RCA) from live public sources.

    await check_person("Petteri Orpo", country="FI", birth_date=None, refresh=False)
    -> {status: pep|rca|clear|error, matches[...], sources[...], checked_at, cached}

Sources (SOURCES-owners-pep.md has the licence findings):
  OpenSanctions  /match/default (schema Person, topics role.pep / role.rca) when OPENSANCTIONS_API_KEY is set. A
                 for-profit business needs a paid OpenSanctions licence (bulk data CC BY-NC 4.0; the API is billed
                 per query), so it is a config switch: never on by default.
  Wikidata       the default (CC0, free, live). wbsearchentities finds candidate items by name; SPARQL on
                 query.wikidata.org keeps humans (P31 = Q5) and reads positions held (P39) with start (P580) / end
                 (P582) dates. A term without its own end date ends with its parliamentary term (P2937 -> P582) or
                 where the next statement for the same position starts.
PEP rule (AMLD (EU) 2015/849 art. 3(9) and art. 22; rahanpesulaki 444/2017 1:4 § and 3:13 §): a position held is a
prominent public function when the position item, its P31 class or any P279 superclass is one of the classes in
PEP_CLASSES (head of state / government, minister, deputy minister, legislator, MP, MEP, party leader, supreme court
judge, central bank governor, ambassador, chief of defence ...), or the office directs (P2389 / P361 / P642) an
organisation that is a supreme or constitutional court, a central bank, a supreme audit institution, a political
party, a state-owned enterprise or an intergovernmental organisation. Local offices (council member, mayor) are not.
Phases: `current` (no end date), `former_within_12m` (ended <= 12 months ago: art. 22 minimum, enhanced due
diligence is mandatory), `former_over_12m` (art. 22 keeps the person a PEP "until deemed to pose no further risk":
that judgement is a person's, so the match stays status pep with this phase until compliance records it).
A deceased person is not a PEP. Family members (art. 3(10)): spouse P26, unmarried partner P451, child P40, parent
P22 / P25, both directions -> status rca when the relative is a PEP and the person is not.
Results are cached in `pep_checks` for 30 days (key = normalised name + country + birth year + source) and kept as
the evidence record (QIDs, position labels, dates, source URLs).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

import httpx

from core.db import db, register_index
from core.util import NO_ID, now_iso

log = logging.getLogger("pep")

WD_API = "https://www.wikidata.org/w/api.php"
WD_SPARQL = "https://query.wikidata.org/sparql"
WD_ENTITY = "https://www.wikidata.org/wiki/"
OS_MATCH = "https://api.opensanctions.org/match/default"
# Wikimedia's User-Agent policy: name the tool and a contact.
UA = {"User-Agent": "HuolitsijaCompliance/1.0 (https://huolitsija.fi; compliance@huolitsija.fi) httpx"}
CACHE_DAYS = 30
NAME_MATCH = 0.9
LANG_FOR = {"FI": "fi", "SE": "sv", "DE": "de", "FR": "fr", "ES": "es", "IT": "it", "NL": "nl", "PL": "pl",
            "EE": "et", "NO": "nb", "DK": "da", "RU": "ru", "UA": "uk", "PT": "pt", "LV": "lv", "LT": "lt"}

# Class QIDs verified live on 2026-10-24 (labels in the comment). Category -> {qid: label}.
PEP_CLASSES = {
    "head_of_state_or_government": {"Q48352": "head of state", "Q2285706": "head of government",
                                    "Q14212": "prime minister", "Q877955": "deputy prime minister"},
    "minister": {"Q83307": "minister", "Q26204040": "deputy minister", "Q16147743": "State Secretary"},
    "parliament": {"Q4175034": "legislator", "Q486839": "member of parliament",
                   "Q27169": "Member of the European Parliament"},
    "party_governing_body": {"Q1553195": "party leader"},
    "supreme_court": {"Q55736868": "national supreme court judge", "Q117826617": "supreme court judge"},
    "central_bank_or_audit": {"Q107363151": "central bank governor"},
    "ambassador": {"Q121998": "ambassador", "Q1251687": "head of mission"},
    "armed_forces": {"Q5097014": "chief of defence", "Q83460": "general", "Q132851": "admiral",
                     "Q977459": "flag officer"},
}
ORG_CLASSES = {
    "supreme_court": {"Q190752": "supreme court", "Q32766": "constitutional court", "Q1767520": "court of cassation"},
    "central_bank_or_audit": {"Q66344": "central bank", "Q43306178": "supreme audit institution"},
    "party_governing_body": {"Q7278": "political party"},
    "state_owned_enterprise": {"Q270791": "state-owned enterprise"},
    "international_organisation": {"Q245065": "intergovernmental organization", "Q484652": "international organization"},
}
LOCAL_CLASSES = {"Q708492": "council member", "Q30185": "mayor"}
CATEGORY_TEXT = {
    "head_of_state_or_government": ("valtionpäämies, pääministeri tai varapääministeri", "head of state or government"),
    "minister": ("ministeri, apulaisministeri tai valtiosihteeri", "minister, deputy or assistant minister"),
    "parliament": ("kansanedustaja tai lainsäädäntöelimen jäsen", "member of parliament or a legislative body"),
    "party_governing_body": ("poliittisen puolueen johtoelimen jäsen", "member of a political party's governing body"),
    "supreme_court": ("korkeimman oikeuden tai perustuslakituomioistuimen jäsen", "member of a supreme or constitutional court"),
    "central_bank_or_audit": ("keskuspankin tai ylimmän tilintarkastuselimen johtoelimen jäsen",
                              "member of a central bank board or court of auditors"),
    "ambassador": ("suurlähettiläs tai asiainhoitaja", "ambassador or chargé d'affaires"),
    "armed_forces": ("asevoimien korkea-arvoinen upseeri", "high-ranking officer in the armed forces"),
    "state_owned_enterprise": ("valtionyhtiön hallinto-, johto- tai valvontaelimen jäsen",
                               "member of a board of a state-owned enterprise"),
    "international_organisation": ("kansainvälisen järjestön johtaja tai hallituksen jäsen",
                                   "director or board member of an international organisation"),
}
RELATION = {"P26": "spouse", "P451": "partner", "P40": "child", "P22": "parent", "P25": "parent"}
INVERSE = {"P26": "spouse", "P451": "partner", "P40": "parent", "P22": "child", "P25": "child"}
RELATION_FI = {"spouse": "puoliso", "partner": "avopuoliso", "child": "lapsi", "parent": "vanhempi"}

register_index("pep_checks", "key")
register_index("pep_checks", "checked_at")


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _norm(name: str | None) -> str:
    from services.screening import normalize

    return normalize(name or "")


def name_score(a: str | None, b: str | None) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if sorted(na.split()) == sorted(nb.split()):
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _d(v: str | None) -> date | None:
    m = re.match(r"^\+?(\d{4})-(\d{2})-(\d{2})", v or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), max(1, int(m.group(2))), max(1, int(m.group(3))))
    except ValueError:
        return None


PHASES = ("current", "former_within_12m", "former_over_12m", "undated")


def phase(end: date | None, today: date | None = None, undated: bool = False) -> str:
    today = today or datetime.now(timezone.utc).date()
    if undated and end is None:
        return "undated"
    if end is None or end > today:
        return "current"
    return "former_within_12m" if today <= end + timedelta(days=365) else "former_over_12m"


def _qid(uri: str | None) -> str | None:
    return uri.rsplit("/", 1)[-1] if uri else None


def _values(qids) -> str:
    return " ".join(f"wd:{q}" for q in sorted(set(qids)) if re.fullmatch(r"Q\d+", q or ""))


async def _sparql(c: httpx.AsyncClient, query: str) -> list[dict]:
    for attempt in range(3):
        r = await c.post(WD_SPARQL, data={"query": query}, headers={"Accept": "application/sparql-results+json"})
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            await asyncio.sleep(float(r.headers.get("Retry-After") or 2 * (attempt + 1)))
            continue
        r.raise_for_status()
        return [{k: v.get("value") for k, v in b.items()} for b in r.json()["results"]["bindings"]]
    return []


# ---------------------------------------------------------------------------------------------
# Wikidata
# ---------------------------------------------------------------------------------------------
async def _search(c: httpx.AsyncClient, name: str, country: str | None, *, persona_business_id=None) -> dict[str, dict]:
    langs = ["en"] + ([LANG_FOR[country]] if country in LANG_FOR else [])
    found: dict[str, dict] = {}
    for lang in langs:
        from services.demo_world.boundary import pep_http
        params = {"action": "wbsearchentities", "search": name, "language": lang,
                  "uselang": lang, "type": "item", "limit": 15, "format": "json"}
        r = await pep_http("GET", WD_API, params=params, persona_business_id=persona_business_id)
        if r is None:
            r = await c.get(WD_API, params=params)
        r.raise_for_status()
        for x in r.json().get("search") or []:
            text = (x.get("match") or {}).get("text") or x.get("label")
            s = max(name_score(name, text), name_score(name, x.get("label")))
            if s >= NAME_MATCH and s > found.get(x["id"], {}).get("score", 0):
                found[x["id"]] = {"qid": x["id"], "label": x.get("label"), "matched_text": text, "score": round(s, 3),
                                  "description": x.get("description")}
    return found


async def _people(c, qids) -> dict[str, dict]:
    rows = await _sparql(c, f"""
SELECT ?p ?pLabel ?pDescription ?birth ?death ?iso WHERE {{
  VALUES ?p {{ {_values(qids)} }}
  ?p wdt:P31 wd:Q5 .
  OPTIONAL {{ ?p wdt:P569 ?birth }} OPTIONAL {{ ?p wdt:P570 ?death }}
  OPTIONAL {{ ?p wdt:P27 ?cit . ?cit wdt:P297 ?iso }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,fi,sv,mul". }}
}}""")
    out: dict[str, dict] = {}
    for r in rows:
        q = _qid(r["p"])
        p = out.setdefault(q, {"qid": q, "name": r.get("pLabel"), "description": r.get("pDescription"),
                               "birth_date": (r.get("birth") or "")[:10] or None,
                               "death_date": (r.get("death") or "")[:10] or None, "citizenship": []})
        if r.get("iso") and r["iso"] not in p["citizenship"]:
            p["citizenship"].append(r["iso"])
    return out


async def _relatives(c, qids) -> list[dict]:
    rows = await _sparql(c, f"""
SELECT ?p ?prop ?rel ?dir WHERE {{
  VALUES ?p {{ {_values(qids)} }}
  VALUES ?prop {{ wdt:P26 wdt:P451 wdt:P40 wdt:P22 wdt:P25 }}
  {{ ?p ?prop ?rel . BIND("out" AS ?dir) }} UNION {{ ?rel ?prop ?p . BIND("in" AS ?dir) }}
}}""")
    out = []
    for r in rows:
        pid = _qid(r["prop"])
        out.append({"of": _qid(r["p"]), "qid": _qid(r["rel"]),
                    "relation": (RELATION if r["dir"] == "out" else INVERSE).get(pid, "relative"), "property": pid})
    return out


async def _positions(c, qids) -> dict[str, list[dict]]:
    rows = await _sparql(c, f"""
SELECT ?p ?st ?pos ?posLabel ?start ?end ?termEnd ?iso WHERE {{
  VALUES ?p {{ {_values(qids)} }}
  ?p p:P39 ?st . ?st ps:P39 ?pos . ?st wikibase:rank ?rank . FILTER(?rank != wikibase:DeprecatedRank)
  OPTIONAL {{ ?st pq:P580 ?start }} OPTIONAL {{ ?st pq:P582 ?end }}
  OPTIONAL {{ ?st pq:P2937 ?term . ?term wdt:P582 ?termEnd }}
  OPTIONAL {{ ?pos wdt:P1001|wdt:P17 ?c . ?c wdt:P297 ?iso }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,fi,sv,mul". }}
}}""")
    by_st: dict[str, dict] = {}
    for r in rows:
        st = by_st.setdefault(r["st"], {"person": _qid(r["p"]), "qid": _qid(r["pos"]), "label": r.get("posLabel"),
                                        "start": (r.get("start") or "")[:10] or None,
                                        "end": (r.get("end") or "")[:10] or None,
                                        "term_end": (r.get("termEnd") or "")[:10] or None, "country": None})
        if r.get("iso") and not st["country"]:
            st["country"] = r["iso"]
    out: dict[str, list[dict]] = {}
    for st in by_st.values():
        out.setdefault(st.pop("person"), []).append(st)
    for lst in out.values():
        # a term without an end date ends with its parliamentary term, or when the next term of that office starts
        for s in lst:
            if s["end"]:
                s["end_basis"] = "P582"
                continue
            if s["term_end"]:
                s.update(end=s["term_end"], end_basis="P2937 term end")
                continue
            if not s["start"]:
                s["end_basis"] = "undated"  # held at some time; no start, end or term on the statement
                continue
            later = sorted(x["start"] for x in lst if x["qid"] == s["qid"] and x["start"] and x["start"] > s["start"])
            s.update(end=later[0] if later else None, end_basis="next term start" if later else None)
    return out


async def _classify(c, pos_qids) -> dict[str, dict]:
    if not pos_qids:
        return {}
    targets = {q for cat in PEP_CLASSES.values() for q in cat} | set(LOCAL_CLASSES)
    orgs = {q for cat in ORG_CLASSES.values() for q in cat}
    rows = await _sparql(c, f"""
SELECT DISTINCT ?pos ?cls WHERE {{
  VALUES ?pos {{ {_values(pos_qids)} }}
  VALUES ?cls {{ {_values(targets)} }}
  {{ ?pos wdt:P279* ?cls }} UNION {{ ?pos wdt:P31/wdt:P279* ?cls }}
}}""")
    org_rows = await _sparql(c, f"""
SELECT DISTINCT ?pos ?org ?orgLabel ?ocls WHERE {{
  VALUES ?pos {{ {_values(pos_qids)} }}
  VALUES ?ocls {{ {_values(orgs)} }}
  ?pos wdt:P2389|wdt:P361|wdt:P642 ?org . ?org wdt:P31/wdt:P279* ?ocls .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,fi,mul". }}
}}""")
    out: dict[str, dict] = {q: {"classes": [], "orgs": [], "local": False} for q in pos_qids}
    for r in rows:
        q, cls = _qid(r["pos"]), _qid(r["cls"])
        if cls in LOCAL_CLASSES:
            out[q]["local"] = True
        for cat, members in PEP_CLASSES.items():
            if cls in members:
                out[q]["classes"].append({"qid": cls, "label": members[cls], "category": cat})
    for r in org_rows:
        q, oc = _qid(r["pos"]), _qid(r["ocls"])
        for cat, members in ORG_CLASSES.items():
            if oc in members:
                out[q]["orgs"].append({"qid": _qid(r["org"]), "label": r.get("orgLabel"), "class": members[oc],
                                       "category": cat})
    return out


def _pep_positions(positions: list[dict], cls: dict[str, dict], death: date | None) -> list[dict]:
    out = []
    for s in positions:
        k = cls.get(s["qid"]) or {}
        classes = [x for x in k.get("classes") or [] if not (k.get("local") and x["category"] == "parliament")]
        cats = sorted({x["category"] for x in classes} | {x["category"] for x in k.get("orgs") or []})
        if not cats:
            continue
        end = _d(s.get("end"))
        if death and (end is None or death < end):
            end = death
        out.append({**s, "categories": cats, "classes": classes, "organisations": k.get("orgs") or [],
                    "phase": phase(end, undated=s.get("end_basis") == "undated"), "url": f"{WD_ENTITY}{s['qid']}",
                    "reason_en": "; ".join(CATEGORY_TEXT[c][1] for c in cats),
                    "reason_fi": "; ".join(CATEGORY_TEXT[c][0] for c in cats)})
    out.sort(key=lambda x: x.get("end") or x.get("start") or "", reverse=True)  # latest first within a phase
    return sorted(out, key=lambda x: PHASES.index(x["phase"]))


def _best_phase(positions: list[dict]) -> str | None:
    for p in PHASES:
        if any(x["phase"] == p for x in positions):
            return p
    return None


async def wikidata_check(name: str, country: str | None = None, birth_date: str | None = None, *, persona_business_id=None) -> dict:
    byear = int(birth_date[:4]) if birth_date and re.match(r"^\d{4}", birth_date) else None
    src = {"source": "Wikidata", "licence": "CC0 1.0", "url": "https://www.wikidata.org", "endpoint": WD_SPARQL}
    async with httpx.AsyncClient(timeout=40, headers=UA) as c:
        cands = await _search(c, name, country, persona_business_id=persona_business_id)
        if not cands:
            return {"status": "clear", "matches": [], "sources": [{**src, "status": "ok", "detail": "no item with this name"}]}
        people = await _people(c, list(cands))
        kept = {}
        for q, p in people.items():
            if byear and p.get("birth_date") and int(p["birth_date"][:4]) != byear:
                continue
            if country and p["citizenship"] and country not in p["citizenship"]:
                continue
            kept[q] = {**p, **{k: cands[q][k] for k in ("matched_text", "score")}}
        if not kept:
            return {"status": "clear", "matches": [], "sources": [{**src, "status": "ok", "detail":
                    f"{len(cands)} item(s) named alike, none a living human matching the country / birth date"}]}
        rels = await _relatives(c, list(kept))
        rel_people = await _people(c, [r["qid"] for r in rels]) if rels else {}
        positions = await _positions(c, list(kept) + list(rel_people))
        cls = await _classify(c, list({s["qid"] for lst in positions.values() for s in lst}))
    matches = []
    for q, p in kept.items():
        death = _d(p.get("death_date"))
        peps = [] if death and death < datetime.now(timezone.utc).date() - timedelta(days=365) else \
            _pep_positions(positions.get(q, []), cls, death)
        relatives = []
        seen = set()
        for r in rels:
            if r["of"] != q or r["qid"] not in rel_people or (r["qid"], r["relation"]) in seen:
                continue
            seen.add((r["qid"], r["relation"]))
            rp = rel_people[r["qid"]]
            rdeath = _d(rp.get("death_date"))
            if rdeath:
                continue  # a deceased relative is not a PEP
            rpos = _pep_positions(positions.get(r["qid"], []), cls, None)
            if rpos:
                relatives.append({"relation": r["relation"], "relation_fi": RELATION_FI.get(r["relation"]),
                                  "property": r["property"], "qid": r["qid"], "name": rp.get("name"),
                                  "url": f"{WD_ENTITY}{r['qid']}", "pep_phase": _best_phase(rpos), "positions": rpos[:6]})
        if not peps and not relatives:
            continue
        matches.append({"source": "Wikidata", "qid": q, "url": f"{WD_ENTITY}{q}", "name": p.get("name"),
                        "matched_text": p.get("matched_text"), "score": p.get("score"),
                        "description": p.get("description"), "birth_date": p.get("birth_date"),
                        "death_date": p.get("death_date"), "citizenship": p.get("citizenship"),
                        "pep": bool(peps), "pep_phase": _best_phase(peps), "positions": peps[:10],
                        "rca": bool(relatives) and not peps, "relatives": relatives})
    status = "pep" if any(m["pep"] for m in matches) else "rca" if matches else "clear"
    return {"status": status, "matches": matches,
            "sources": [{**src, "status": "ok", "detail": f"{len(kept)} human(s) named alike checked: "
                         + ", ".join(f"{q} {kept[q].get('name')}" for q in list(kept)[:5])}]}


# ---------------------------------------------------------------------------------------------
# OpenSanctions (config switch: OPENSANCTIONS_API_KEY; commercial use needs a paid licence)
# ---------------------------------------------------------------------------------------------
def opensanctions_configured() -> bool:
    return bool(os.environ.get("OPENSANCTIONS_API_KEY", "").strip())


async def opensanctions_check(name: str, country: str | None = None, birth_date: str | None = None, *, persona_business_id=None) -> dict:
    props = {"name": [name]}
    if country:
        props["country"] = [country.lower()]
    if birth_date:
        props["birthDate"] = [birth_date]
    body = {"queries": {"q": {"schema": "Person", "properties": props}}}
    src = {"source": "OpenSanctions", "licence": "commercial licence (CC BY-NC 4.0 for non-commercial)",
           "url": "https://www.opensanctions.org", "endpoint": OS_MATCH}
    from services.demo_world.boundary import pep_http
    params = [("topics", "role.pep"), ("topics", "role.rca")]
    r = await pep_http("POST", OS_MATCH, params=params, json=body, persona_business_id=persona_business_id)
    if r is None:
        async with httpx.AsyncClient(timeout=30, headers={**UA, "Authorization": f"ApiKey {os.environ['OPENSANCTIONS_API_KEY'].strip()}"}) as c:
            r = await c.post(OS_MATCH, params=params, json=body)
    if r.status_code != 200:
        raise RuntimeError(f"OpenSanctions answered HTTP {r.status_code}")
    results = ((r.json().get("responses") or {}).get("q") or {}).get("results") or []
    matches = []
    for x in results:
        if not x.get("match"):
            continue
        pr = x.get("properties") or {}
        topics = pr.get("topics") or []
        matches.append({"source": "OpenSanctions", "id": x.get("id"), "url": f"https://www.opensanctions.org/entities/{x.get('id')}/",
                        "name": x.get("caption"), "score": x.get("score"), "birth_date": (pr.get("birthDate") or [None])[0],
                        "citizenship": pr.get("nationality") or pr.get("country") or [], "topics": topics,
                        "pep": "role.pep" in topics, "rca": "role.rca" in topics and "role.pep" not in topics,
                        "pep_phase": None, "positions": [{"label": p} for p in pr.get("position") or []][:10],
                        "datasets": x.get("datasets")})
    status = "pep" if any(m["pep"] for m in matches) else "rca" if any(m["rca"] for m in matches) else "clear"
    return {"status": status, "matches": matches, "sources": [{**src, "status": "ok", "detail": f"{len(results)} result(s)"}]}


# ---------------------------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------------------------
def _key(name: str, country: str | None, birth_date: str | None, source: str) -> str:
    raw = "|".join([" ".join(sorted(_norm(name).split())), (country or "").upper(), (birth_date or "")[:4], source])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def check_person(name: str, *, country: str | None = None, birth_date: str | None = None,
                       refresh: bool = False, persona_business_id: str | None = None) -> dict:
    """PEP / RCA status of one natural person. Never raises for a source failure: status error instead."""
    from services.screening import country_iso2

    name = (name or "").strip()
    if not _norm(name):
        raise ValueError("A name is required for a PEP check")
    country = country_iso2(country) if country else None
    source = "OpenSanctions" if opensanctions_configured() else "Wikidata"
    key = _key(name, country, birth_date, source)
    from services.demo_world.boundary import pep_scope
    scope = await pep_scope(persona_business_id)
    if scope:
        key = f"world:{scope}:{key}"  # persona evidence must never clear a real person's cached check
    since = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not refresh and not scope:  # mutable actor facts must also invalidate results cached by older releases
        hit = await db.pep_checks.find_one({"key": key, "checked_at": {"$gte": since}, "status": {"$ne": "error"}},
                                           NO_ID, sort=[("checked_at", -1)])
        if hit:
            return {**hit, "cached": True}
    skipped = [] if source == "OpenSanctions" else [{
        "source": "OpenSanctions", "status": "not_configured",
        "detail": "OPENSANCTIONS_API_KEY not set (commercial use needs a paid OpenSanctions licence)"}]
    try:
        from services.demo_world.boundary import registry_pep_hit
        res = await registry_pep_hit(scope, name)
        if res is None:
            res = await (opensanctions_check if source == "OpenSanctions" else wikidata_check)(
                name, country, birth_date, **({"persona_business_id": scope} if scope else {}))
    except Exception as exc:  # noqa: BLE001 - a source outage is a result, not a crash
        log.warning("PEP check via %s failed: %s", source, exc)
        res = {"status": "error", "matches": [], "sources": [{"source": source, "status": "error",
                                                              "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}]}
    doc = {"key": key, "query": {"name": name, "country": country, "birth_date": birth_date}, "source": source,
           **res, "sources": res["sources"] + skipped, "checked_at": now_iso()}
    await db.pep_checks.insert_one(dict(doc))
    return {**{k: v for k, v in doc.items() if k != "_id"}, "cached": False}


def match_keys(result: dict) -> list[str]:
    """Stable identifiers of the PEP / RCA matches (a person's approval covers exactly these)."""
    return sorted(f"{m.get('source')}:{m.get('qid') or m.get('id')}" for m in result.get("matches") or [])


# ---------------------------------------------------------------------------------------------
# KYC check `pep` (services.kyc calls this; a hit is legal point `pep_relationship`)
# ---------------------------------------------------------------------------------------------
LEGAL_POINT = "pep_relationship"
LEGAL_BASIS = "AMLD (EU) 2015/849 art. 20-23; rahanpesulaki 444/2017 3:13 §"
PHASE_TEXT = {"current": ("tehtävässä nyt", "in office"),
              "former_within_12m": ("tehtävä päättyi alle 12 kk sitten", "left office less than 12 months ago"),
              "former_over_12m": ("entinen; riskiarvio tarvitaan (AMLD 22 art.)", "former; risk decision needed (AMLD art. 22)"),
              "undated": ("ajankohta ei tiedossa", "dates unknown")}


def kyc_persons(company: dict, owners: dict | None = None) -> list[dict]:
    out: dict[str, dict] = {}

    def add(name, role, role_fi, country=None, birth_date=None, declared=False):
        k = " ".join(sorted(_norm(name).split()))
        if not k:
            return
        p = out.setdefault(k, {"name": name.strip(), "roles": [], "roles_fi": [], "country": country,
                               "birth_date": birth_date, "pep_declared": False})
        if role not in p["roles"]:
            p["roles"].append(role)
            p["roles_fi"].append(role_fi)
        p["country"] = p["country"] or country
        p["birth_date"] = p["birth_date"] or birth_date
        p["pep_declared"] = p["pep_declared"] or declared

    for u in company.get("ubos") or []:
        if u.get("name"):
            add(u["name"], "beneficial owner", "tosiasiallinen edunsaaja", u.get("country"), u.get("birth_date"), bool(u.get("pep")))
    for o in (owners or {}).get("owners") or []:
        if o.get("kind") == "person" and o.get("name"):
            add(o["name"], f"beneficial owner ({o['source']})", f"edunsaaja ({o['source']})", o.get("country"), o.get("birth_date"))
    sig = company.get("signatory") or {}
    if sig.get("name"):
        add(sig["name"], "signatory", "allekirjoittaja")
    return list(out.values())


def _hit_line(p: dict, fi: bool) -> str:
    roles = ", ".join(p["roles_fi"] if fi else p["roles"])
    if p.get("status") in ("pep", "rca"):
        m = next(x for x in p["matches"] if x.get("pep") or x.get("rca"))
        if m.get("pep"):
            pos = m["positions"][0] if m.get("positions") else {}
            when = PHASE_TEXT.get(pos.get("phase"), ("", ""))[0 if fi else 1]
            what = f"{pos.get('label')} {pos.get('start') or ''}–{pos.get('end') or ''}".strip()
        else:
            r = m["relatives"][0]
            rp = (r.get("positions") or [{}])[0]
            what = (f"{r.get('relation_fi')}: {r.get('name')} ({rp.get('label')})" if fi
                    else f"{r.get('relation')} of {r.get('name')} ({rp.get('label')})")
            when = PHASE_TEXT.get(r.get("pep_phase"), ("", ""))[0 if fi else 1]
        ref = m.get("qid") or m.get("id")
        tag = ("PEP" if m.get("pep") else ("PEP:n perheenjäsen" if fi else "PEP family member"))
        return f"{p['name']} ({roles}): {tag}, {what}, {when} [{m['source']} {ref}]"
    return f"{p['name']} ({roles}): " + ("ilmoitettu itse poliittisesti vaikutusvaltaiseksi" if fi else "self-declared PEP")


async def kyc_check(company: dict, owners: dict | None = None) -> dict:
    persons = kyc_persons(company, owners)
    src = "Wikidata (CC0)" if not opensanctions_configured() else "OpenSanctions"
    if not persons:
        return {"status": "not_applicable", "checked_at": now_iso(), "source": src, "persons": [],
                "summary": "No natural person to check yet (no beneficial owner or signatory declared)",
                "summary_fi": "Ei tarkistettavia henkilöitä (edunsaajia tai allekirjoittajaa ei ole ilmoitettu)"}
    for p in persons:
        r = await check_person(p["name"], country=p.get("country"), birth_date=p.get("birth_date"),
                               persona_business_id=company.get("business_id"))
        p.update(status=r["status"], matches=r["matches"], sources=r["sources"], checked_at=r["checked_at"],
                 check_key=r.get("key"))
    hits = [p for p in persons if p["status"] in ("pep", "rca") or p["pep_declared"]]
    keys = sorted({f"{' '.join(sorted(_norm(p['name']).split()))}|{k}" for p in hits
                   for k in (match_keys(p) or ["declared"])})
    decision = (company.get("kyc") or {}).get("pep_decision") or {}
    errors = [p for p in persons if p["status"] == "error"]
    base = {"checked_at": now_iso(), "source": f"{src}: positions held (P39), family (P26/P40/P22/P25/P451)",
            "persons": [{k: p.get(k) for k in ("name", "roles", "country", "birth_date", "pep_declared", "status",
                                                "matches", "checked_at")} for p in persons], "hit_keys": keys}
    if hits and set(keys) <= set(decision.get("approved") or []):
        by = (decision.get("by") or {}).get("name") or "compliance"
        return {**base, "status": "pass", "approved": True,
                "summary": f"PEP relationship approved by {by} on {(decision.get('at') or '')[:10]}: "
                           + "; ".join(_hit_line(p, False) for p in hits) + ". Enhanced ongoing monitoring applies",
                "summary_fi": f"PEP-asiakkuuden hyväksyi {by} {(decision.get('at') or '')[:10]}: "
                              + "; ".join(_hit_line(p, True) for p in hits) + ". Tehostettu jatkuva seuranta"}
    if hits:
        return {**base, "status": "review", "legal_point": LEGAL_POINT, "legal_basis": LEGAL_BASIS,
                "summary": "; ".join(_hit_line(p, False) for p in hits)
                           + ". A person with KYC approval rights must approve the PEP relationship",
                "summary_fi": "; ".join(_hit_line(p, True) for p in hits)
                              + ". PEP-asiakkuuden hyväksyy henkilö, jolla on KYC-hyväksyntäoikeus"}
    if errors:
        return {**base, "status": "error",
                "summary": f"PEP check did not complete for {', '.join(p['name'] for p in errors)}; retry later",
                "summary_fi": f"PEP-tarkistus ei valmistunut: {', '.join(p['name'] for p in errors)}; yritä myöhemmin"}
    return {**base, "status": "pass",
            "summary": f"No politically exposed person or family member among {len(persons)} name(s)",
            "summary_fi": f"Ei poliittisesti vaikutusvaltaisia henkilöitä tai heidän perheenjäseniään ({len(persons)} nimeä)"}


async def mark_card(card: dict | None, company: dict, chk: dict):
    """Put the legal point on the company's kyc_review card (s2's owner-queue fields; read by people and agents)."""
    if not card:
        return
    try:
        from core.legal_points import LEGAL_POINTS  # type: ignore  # s2 feat/autopilot-ops
        lp = LEGAL_POINTS.get(LEGAL_POINT) or {}
    except ImportError:
        lp = {}
    sets = {"legal_point": LEGAL_POINT, "legal_basis": lp.get("basis") or LEGAL_BASIS,
            "legal_reason_fi": lp.get("reason_fi") or "Asiakkuus poliittisesti vaikutusvaltaisen henkilön kanssa: "
                                                       "ihminen hyväksyy, agentti ei voi",
            "legal_reason_en": lp.get("reason_en") or "Business relationship with a politically exposed person: "
                                                       "a person approves, never an agent",
            "title_fi": f"PEP-tarkistus: {company.get('name')}",
            "summary_fi": (chk.get("summary_fi") or "")[:900],
            "data.pep": {"summary": chk.get("summary"), "summary_fi": chk.get("summary_fi"), "hit_keys": chk.get("hit_keys"),
                         "persons": [{k: p.get(k) for k in ("name", "roles", "status")} for p in chk.get("persons") or []]},
            "agent_decides": False}
    await db.approvals.update_one({"id": card["id"]}, {"$set": sets})
