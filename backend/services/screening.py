"""Sanctions and denied-party screening against five official lists (RULE K-1, domain-spec 10.2):

    eu    EU consolidated financial sanctions list (European Commission FSF XML)
    un    UN Security Council consolidated list (XML)
    ofac  US OFAC SDN list (sdn/alt/add CSV from Treasury)
    uk    UK Sanctions List (FCDO XML; the single UK list since 28 Jan 2026, OFSI asset-freeze targets included; the
          frozen OFSI Consolidated List is deliberately not loaded, see OFSI_CONLIST_URL)
    csl   US Consolidated Screening List (data.trade.gov bulk JSON): BIS Entity, Denied Persons, Unverified and Military
          End User lists, State ITAR debarred and nonproliferation sanctions, OFAC non-SDN lists. SDN rows are skipped
          because `ofac` already holds them; each hit names its sub-list ("BIS Entity List"). With TRADE_GOV_API_KEY set,
          csl_live_search() asks the live CSL search API for a second opinion.

The public lists are downloaded (at most once per 24 h), parsed and cached in MongoDB:

    db.sanctions_entries   one document per listed subject (person, entity, vessel, aircraft) and list version
    db.sanctions_lists     one meta document per list: source URL, fetched_at, generated_at, entries, sha256, status

Screening normalises the name (accents, Cyrillic transliteration, legal forms such as "Oy", "LLC",
"Joint Stock Company"), retrieves candidate aliases through an in-memory token index (exact word,
transliteration key and consonant skeleton, so "Sergey Shoygu" finds "SHOIGU, Sergei") plus a fuzzy
pass over the joined name (so "AlQaida" finds "Al Qa'ida"), and scores each candidate alias by
IDF-weighted word coverage in both directions. Common words ("bank", "trading", "international")
carry little weight, so a name that only shares them with a listed subject stays clear; a listed name
of two or more words that is fully contained in the screened name ("Rosneft Oil Company Finland Oy"
contains "Rosneft Oil Company") is flagged for review.

    res = await screen_name("Rosneft Oil Company", kind="entity")
    res["status"]  -> "potential_match"; res["hits"][0]["explanation"] names the list, ref and the matched alias

A result is never "clear" without data: if no list could be loaded the status is "error".
Storing a new list version emits `sanctions.lists_changed [lists, changes, versions]`; services.party_screening
re-screens open shipments and active customers on it.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import math
import re
import sys
import time
from array import array
import unicodedata
from collections import defaultdict
from functools import lru_cache

import httpx
from rapidfuzz import fuzz, process
from rapidfuzz.distance import JaroWinkler

from core.db import db, register_index
from core.lifecycle import background_loop, on_startup
from core.util import NO_ID, new_id, now, now_iso, parse_iso

log = logging.getLogger("screening")

EU_URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"
          "?token=dG9rZW4tMjAxNw")
UN_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
OFAC_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"
OFAC_ALT_URL = "https://www.treasury.gov/ofac/downloads/alt.csv"
OFAC_ADD_URL = "https://www.treasury.gov/ofac/downloads/add.csv"
# UK Sanctions List (FCDO). Since 28 Jan 2026 the only UK list: it carries every UK designation, OFSI's asset-freeze
# targets included. XML is the structured machine format (one <Designation> per subject with all its names); the CSV
# repeats a row per name and the .json path answers 403.
UK_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml"
# OFSI Consolidated List (2022 format). Not loaded: OFSI stopped updating it on 28 Jan 2026 when the UK Sanctions List
# became the single source. Checked 2026-10-24: file Last-Modified 3 Jun 2026, newest "Last Updated" row 3 Jun 2026,
# none of the 227 UK designations made since then is in it, and the 18 OFSI group IDs missing from the UK list are
# delistings or re-numbered entries. Loading it would add stale names and drop nothing.
OFSI_CONLIST_URL = "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv"
# US Consolidated Screening List (ITA, data.trade.gov): the keyless official bulk file, refreshed hourly from the
# Commerce (BIS), State and Treasury lists. JSON keeps aliases, programmes and addresses as arrays and names the date
# each source list was last updated.
CSL_URL = "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.json"
CSL_API_URL = "https://data.trade.gov/consolidated_screening_list/v1/search"

LIST_SOURCES: dict = {
    "eu": {"label": "EU consolidated financial sanctions list", "short": "EU", "url": EU_URL,
           "authority": "European Commission (Financial Sanctions Files)"},
    "un": {"label": "UN Security Council consolidated list", "short": "UN", "url": UN_URL,
           "authority": "United Nations Security Council"},
    "ofac": {"label": "OFAC Specially Designated Nationals (SDN) list", "short": "OFAC SDN", "url": OFAC_SDN_URL,
             "authority": "US Treasury, Office of Foreign Assets Control",
             "extra_urls": {"aliases": OFAC_ALT_URL, "addresses": OFAC_ADD_URL}},
    "uk": {"label": "UK Sanctions List", "short": "UK", "url": UK_URL,
           "authority": "UK Foreign, Commonwealth & Development Office (incl. HM Treasury OFSI financial sanctions)"},
    "csl": {"label": "US Consolidated Screening List", "short": "US CSL", "url": CSL_URL,
            "authority": "US International Trade Administration (Commerce BIS, State, Treasury OFAC non-SDN lists)"},
}

# CSL source -> label shown on a hit. SDN rows are skipped (the `ofac` list loads the same SDN file from Treasury).
CSL_SUB_LISTS = {
    "EL": "BIS Entity List", "DPL": "BIS Denied Persons List", "UVL": "BIS Unverified List",
    "MEU": "BIS Military End User List", "DTC": "State Dept ITAR Debarred List",
    "ACN": "State Dept Nonproliferation Sanctions", "SSI": "OFAC Sectoral Sanctions Identifications List",
    "NS-MBS": "OFAC Non-SDN Menu-Based Sanctions List", "CMIC": "OFAC Non-SDN Chinese Military-Industrial Complex List",
    "PLC": "OFAC Palestinian Legislative Council List", "CAP": "OFAC CAPTA List",
    "FSE": "OFAC Foreign Sanctions Evaders List", "SDN": "OFAC SDN List",
}


MAX_AGE_HOURS = 24

# Score at or above which a name is a potential match that a compliance officer must decide (RULE K-1).
# Calibrated 2026-10-24 on the live lists: 39 must-hit names (exact, transliterated, reordered, legal
# form changed, subsidiaries) and 36 random perturbed list samples all score >= 0.90; of 303 real
# Finnish company names from YTJ only two reach it, both carrying exactly the listed words
# ("Amak Oy" / AMAK, "Art House Oy" / HOUSE OF ART LIMITED); the next highest scores 0.85.
MATCH_THRESHOLD = 0.86
# Hits between REVIEW_FLOOR and MATCH_THRESHOLD are not matches; the closest one is returned as context.
REVIEW_FLOOR = 0.70

register_index("sanctions_entries", [("list", 1), ("version", 1)])
register_index("sanctions_lists", "list", unique=True)

_refresh_lock = asyncio.Lock()


# ---------------------------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------------------------
_SPECIAL = str.maketrans({"ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l",
                          "Ł": "L", "ı": "i", "œ": "oe", "Œ": "OE", "þ": "th", "Þ": "TH", "ð": "d", "Ð": "D",
                          "’": "", "'": "", "`": "", "´": "", "ʼ": "", "‘": ""})
_CYR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya", "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u"}

# Legal-form phrases removed anywhere in the name, and single legal-form tokens removed at either end.
_LEGAL_PHRASES = [
    "obshchestvo s ogranichennoy otvetstvennostyu", "obshchestvo s ogranichennoi otvetstvennostyu",
    "publichnoe aktsionernoe obshchestvo", "otkrytoe aktsionernoe obshchestvo", "zakrytoe aktsionernoe obshchestvo",
    "aktsionernoe obshchestvo", "obshchestvo s dopolnitelnoy otvetstvennostyu",
    "public joint stock company", "open joint stock company", "closed joint stock company", "joint stock company",
    "limited liability company", "limited liability partnership", "private limited company", "public limited company",
    "limited partnership", "general partnership", "sp z o o", "spolka z ograniczona odpowiedzialnoscia",
    "anonim sirketi", "limited sirketi", "ltd sti", "sdn bhd", "pte ltd", "pty ltd", "co ltd", "s a r l", "s a",
    "l l c", "fz llc", "fze llc", "unitary enterprise", "federal state unitary enterprise", "state unitary enterprise",
]
_LEGAL_TOKENS = {
    "oy", "oyj", "ab", "abp", "ky", "tmi", "ay", "ltd", "limited", "llc", "inc", "incorporated", "gmbh", "mbh", "ag",
    "kg", "sa", "sarl", "sas", "srl", "spa", "bv", "nv", "plc", "ooo", "oao", "ao", "pao", "zao", "jsc", "ojsc",
    "cjsc", "pjsc", "co", "company", "corp", "corporation", "ltda", "lda", "sl", "as", "aps", "ou", "uab", "sia",
    "kft", "zrt", "doo", "ad", "eood", "ood", "fze", "fzco", "fzc", "llp", "lp", "pte", "pty", "bhd", "sdn", "sti",
    "tic", "gie", "eirl", "eurl", "sprl", "scs", "snc", "oa", "tov", "pp", "dp", "kp", "fgup", "gup", "mup",
}
_TRAILING_FORM = {"group", "holding", "holdings"}
_STOP = {"the", "of", "and", "for", "de", "del", "la", "le", "du", "des", "et", "y", "und", "da", "do", "dos", "das"}
_PATRONYMIC = re.compile(r"(ovich|evich|ovna|evna|ichna|ogly|oglu|kyzy|qizi)$")


def _ascii(s: str) -> str:
    s = s.translate(_SPECIAL)
    s = "".join(_CYR.get(ch, ch) if "Ѐ" <= ch.lower() <= "ӿ" else ch for ch in s.lower())
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def normalize(name: str) -> str:
    """Lowercase ASCII form with punctuation, stop words and legal forms removed."""
    s = _ascii(name or "")
    s = re.sub(r"[^\w]+", " ", s, flags=re.U).replace("_", " ")
    s = " " + re.sub(r"\s+", " ", s).strip() + " "
    for ph in _LEGAL_PHRASES:
        s = s.replace(" " + ph + " ", " ")
    toks = [t for t in s.split() if t not in _STOP]
    while len(toks) > 1 and toks[0] in _LEGAL_TOKENS:
        toks.pop(0)
    while len(toks) > 1 and (toks[-1] in _LEGAL_TOKENS or toks[-1] in _TRAILING_FORM):
        if toks[-1] in _TRAILING_FORM and len("".join(toks[:-1])) < 4:
            break  # "MC Group" stays "mc group": a bare acronym is not a name
        toks.pop()
    return " ".join(toks)


def _tkey(tok: str) -> str:
    """Transliteration key: folds common Latin spellings of the same sound (Sergey/Sergei, Shoygu/Shoigu)."""
    t = tok
    for a, b in (("dzh", "j"), ("zh", "j"), ("kh", "h"), ("ph", "f"), ("sch", "sh"), ("tch", "ch"), ("ck", "k"),
                 ("ch", "\x01"), ("q", "k"), ("c", "k"), ("w", "v"), ("x", "ks"), ("y", "i"), ("ou", "u"),
                 ("ee", "i"), ("oo", "u"), ("ii", "i"), ("ie", "i"), ("ij", "i"), ("tz", "ts")):
        t = t.replace(a, b)
    t = t.replace("\x01", "ch")
    return re.sub(r"(.)\1+", r"\1", t)


def _skel(tkey: str) -> str:
    """Consonant skeleton of a transliteration key (Mohammed/Muhammad -> mhmd)."""
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[aeiouy]", "", tkey))


# ---------------------------------------------------------------------------------------------
# Countries (ISO 3166-1 alpha-2) for UN/OFAC country names
# ---------------------------------------------------------------------------------------------
_ISO = (
    "AF Afghanistan|AL Albania|DZ Algeria|AD Andorra|AO Angola|AG Antigua and Barbuda|AR Argentina|AM Armenia|"
    "AW Aruba|AU Australia|AT Austria|AZ Azerbaijan|BS Bahamas|BH Bahrain|BD Bangladesh|BB Barbados|BY Belarus|"
    "BE Belgium|BZ Belize|BJ Benin|BM Bermuda|BT Bhutan|BO Bolivia|BA Bosnia and Herzegovina|BW Botswana|BR Brazil|"
    "VG British Virgin Islands|BN Brunei|BG Bulgaria|BF Burkina Faso|BI Burundi|KH Cambodia|CM Cameroon|CA Canada|"
    "CV Cabo Verde|KY Cayman Islands|CF Central African Republic|TD Chad|CL Chile|CN China|CO Colombia|KM Comoros|"
    "CG Congo|CD Democratic Republic of the Congo|CR Costa Rica|CI Cote d Ivoire|HR Croatia|CU Cuba|CW Curacao|"
    "CY Cyprus|CZ Czechia|DK Denmark|DJ Djibouti|DM Dominica|DO Dominican Republic|EC Ecuador|EG Egypt|"
    "SV El Salvador|GQ Equatorial Guinea|ER Eritrea|EE Estonia|SZ Eswatini|ET Ethiopia|FJ Fiji|FI Finland|FR France|"
    "GA Gabon|GM Gambia|GE Georgia|DE Germany|GH Ghana|GI Gibraltar|GR Greece|GD Grenada|GT Guatemala|GG Guernsey|"
    "GN Guinea|GW Guinea-Bissau|GY Guyana|HT Haiti|HN Honduras|HK Hong Kong|HU Hungary|IS Iceland|IN India|"
    "ID Indonesia|IR Iran|IQ Iraq|IE Ireland|IM Isle of Man|IL Israel|IT Italy|JM Jamaica|JP Japan|JE Jersey|"
    "JO Jordan|KZ Kazakhstan|KE Kenya|KI Kiribati|KP North Korea|KR South Korea|XK Kosovo|KW Kuwait|KG Kyrgyzstan|"
    "LA Laos|LV Latvia|LB Lebanon|LS Lesotho|LR Liberia|LY Libya|LI Liechtenstein|LT Lithuania|LU Luxembourg|"
    "MO Macau|MG Madagascar|MW Malawi|MY Malaysia|MV Maldives|ML Mali|MT Malta|MH Marshall Islands|MR Mauritania|"
    "MU Mauritius|MX Mexico|FM Micronesia|MD Moldova|MC Monaco|MN Mongolia|ME Montenegro|MA Morocco|MZ Mozambique|"
    "MM Myanmar|NA Namibia|NR Nauru|NP Nepal|NL Netherlands|NZ New Zealand|NI Nicaragua|NE Niger|NG Nigeria|"
    "MK North Macedonia|NO Norway|OM Oman|PK Pakistan|PW Palau|PS Palestine|PA Panama|PG Papua New Guinea|"
    "PY Paraguay|PE Peru|PH Philippines|PL Poland|PT Portugal|QA Qatar|RO Romania|RU Russia|RW Rwanda|"
    "KN Saint Kitts and Nevis|LC Saint Lucia|VC Saint Vincent and the Grenadines|WS Samoa|SM San Marino|"
    "ST Sao Tome and Principe|SA Saudi Arabia|SN Senegal|RS Serbia|SC Seychelles|SL Sierra Leone|SG Singapore|"
    "SK Slovakia|SI Slovenia|SB Solomon Islands|SO Somalia|ZA South Africa|SS South Sudan|ES Spain|LK Sri Lanka|"
    "SD Sudan|SR Suriname|SE Sweden|CH Switzerland|SY Syria|TW Taiwan|TJ Tajikistan|TZ Tanzania|TH Thailand|"
    "TL Timor-Leste|TG Togo|TO Tonga|TT Trinidad and Tobago|TN Tunisia|TR Turkey|TM Turkmenistan|TV Tuvalu|"
    "UG Uganda|UA Ukraine|AE United Arab Emirates|GB United Kingdom|US United States|UY Uruguay|UZ Uzbekistan|"
    "VU Vanuatu|VA Holy See|VE Venezuela|VN Vietnam|YE Yemen|ZM Zambia|ZW Zimbabwe|AN Netherlands Antilles|"
    "PR Puerto Rico|NC New Caledonia|PF French Polynesia|GL Greenland|FO Faroe Islands|AI Anguilla|"
    "TC Turks and Caicos Islands|MS Montserrat|SX Sint Maarten|BQ Bonaire|EH Western Sahara|AX Aland Islands"
)
_COUNTRY_ALIASES = {
    "russian federation": "RU", "iran islamic republic of": "IR", "islamic republic of iran": "IR",
    "syrian arab republic": "SY", "korea democratic peoples republic of": "KP", "democratic peoples republic of korea": "KP",
    "korea north": "KP", "dprk": "KP", "korea republic of": "KR", "republic of korea": "KR", "korea south": "KR",
    "viet nam": "VN", "lao peoples democratic republic": "LA", "burma": "MM", "united states of america": "US", "usa": "US",
    "united kingdom of great britain and northern ireland": "GB", "great britain": "GB", "uk": "GB",
    "congo democratic republic of the": "CD", "congo democratic republic of": "CD", "congo democratic republic of was zaire": "CD",
    "congo republic of the": "CG", "congo peoples republic of": "CG", "republic of the congo": "CG",
    "cote divoire": "CI", "ivory coast": "CI", "czech republic": "CZ", "bahamas the": "BS", "gambia the": "GM",
    "moldova republic of": "MD", "republic of moldova": "MD", "tanzania united republic of": "TZ",
    "united republic of tanzania": "TZ", "bolivia plurinational state of": "BO", "venezuela bolivarian republic of": "VE",
    "palestinian territory occupied": "PS", "state of palestine": "PS", "palestinian": "PS", "gaza": "PS",
    "west bank": "PS", "north gaza": "PS", "northern gaza": "PS", "virgin islands british": "VG",
    "china hong kong special administrative region": "HK", "hong kong special administrative region": "HK",
    "macao": "MO", "man isle of": "IM", "north macedonia the republic of": "MK", "macedonia": "MK",
    "bosnia and herzegowina": "BA", "turkiye": "TR", "republic of turkiye": "TR", "swaziland": "SZ",
    "cape verde": "CV", "timor leste": "TL", "east timor": "TL", "crimea": "UA", "brunei darussalam": "BN",
    "micronesia federated states of": "FM", "holy see vatican city state": "VA", "kyrgyz republic": "KG",
    "region crimea": "UA", "region russia": "RU", "region iran": "IR", "region gaza": "PS",
}


def _ckey(s: str) -> str:
    s = _ascii(s)
    s = re.sub(r"^region\s*:\s*", "", s.strip())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]+", " ", s)).strip()


_COUNTRY = {}
for _part in _ISO.split("|"):
    _code, _nm = _part.split(" ", 1)
    _COUNTRY[_ckey(_nm)] = _code
_COUNTRY.update({_ckey(k): v for k, v in _COUNTRY_ALIASES.items()})


def country_iso2(name: str | None) -> str | None:
    if not name:
        return None
    s = name.strip()
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return s.upper()
    k = _ckey(s)
    if k in _COUNTRY:
        return _COUNTRY[k]
    k2 = re.sub(r"\b(the|of)\b", " ", k)
    k2 = re.sub(r"\s+", " ", k2).strip()
    return _COUNTRY.get(k2)


# ---------------------------------------------------------------------------------------------
# Parsers (run in a worker thread: lxml/csv parsing of 30 MB blocks the event loop otherwise)
# ---------------------------------------------------------------------------------------------
def _clean_names(names) -> list[str]:
    out, seen = [], set()
    for n in names:
        n = re.sub(r"\s+", " ", (n or "").strip())
        if len(n) < 2 or n in ("-0-",):
            continue
        k = n.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out


def parse_eu(data: bytes) -> tuple[list[dict], str | None]:
    from lxml import etree

    ns = "{http://eu.europa.ec/fpi/fsd/export}"
    entries, generated = [], None
    for event, el in etree.iterparse(io.BytesIO(data), events=("start", "end")):
        if event == "start":
            if el.tag == f"{ns}export":
                generated = el.get("generationDate")
            continue
        if el.tag != f"{ns}sanctionEntity":
            continue
        st = el.find(f"{ns}subjectType")
        code = (st.get("code") if st is not None else "") or ""
        typ = {"person": "person", "enterprise": "entity"}.get(code, "other")
        names, programmes, countries, regs = [], set(), set(), []
        for na in el.findall(f"{ns}nameAlias"):
            whole = na.get("wholeName") or " ".join(
                x for x in (na.get("firstName"), na.get("middleName"), na.get("lastName")) if x)
            names.append(whole)
        for rg in el.findall(f"{ns}regulation"):
            if rg.get("programme"):
                programmes.add(rg.get("programme"))
            if rg.get("publicationDate"):
                regs.append(rg.get("publicationDate"))
        for tag in ("citizenship", "address", "birthdate"):
            for c in el.findall(f"{ns}{tag}"):
                iso = (c.get("countryIso2Code") or "").upper()
                if re.fullmatch(r"[A-Z]{2}", iso):
                    countries.add(iso)
        rem = el.find(f"{ns}remark")
        names = _clean_names(names)
        if names:
            entries.append({
                "list": "eu", "ref": el.get("euReferenceNumber") or f"EU-{el.get('logicalId')}",
                "logical_id": el.get("logicalId"), "un_ref": el.get("unitedNationId") or None,
                "primary_name": names[0], "names": names, "type": typ, "programmes": sorted(programmes),
                "countries": sorted(countries), "listed_on": el.get("designationDate") or (min(regs) if regs else None),
                "remarks": ((rem.text or "").strip()[:300] if rem is not None else ""),
            })
        el.clear()
        while el.getprevious() is not None:
            del el.getparent()[0]
    return entries, generated


def parse_un(data: bytes) -> tuple[list[dict], str | None]:
    from lxml import etree

    entries, generated = [], None
    for event, el in etree.iterparse(io.BytesIO(data), events=("start", "end")):
        if event == "start":
            if el.tag == "CONSOLIDATED_LIST":
                generated = el.get("dateGenerated")
            continue
        if el.tag not in ("INDIVIDUAL", "ENTITY"):
            continue
        person = el.tag == "INDIVIDUAL"
        tx = lambda t: (el.findtext(t) or "").strip()  # noqa: E731
        primary = " ".join(x for x in (tx("FIRST_NAME"), tx("SECOND_NAME"), tx("THIRD_NAME"), tx("FOURTH_NAME")) if x)
        names, low = [primary], []
        if tx("NAME_ORIGINAL_SCRIPT"):
            names.append(tx("NAME_ORIGINAL_SCRIPT"))
        for al in el.findall("INDIVIDUAL_ALIAS" if person else "ENTITY_ALIAS"):
            an = (al.findtext("ALIAS_NAME") or "").strip()
            if an:
                (low if (al.findtext("QUALITY") or "").strip().lower() == "low" else names).append(an)
        countries = set()
        for path in ("NATIONALITY/VALUE", "INDIVIDUAL_ADDRESS/COUNTRY", "ENTITY_ADDRESS/COUNTRY",
                     "INDIVIDUAL_PLACE_OF_BIRTH/COUNTRY"):
            for c in el.findall(path):
                iso = country_iso2(c.text)
                if iso:
                    countries.add(iso)
        names = _clean_names(names)
        low = [n for n in _clean_names(low) if n.lower() not in {x.lower() for x in names}]
        if names:
            entries.append({
                "list": "un", "ref": tx("REFERENCE_NUMBER") or f"UN-{tx('DATAID')}", "logical_id": tx("DATAID"),
                "primary_name": names[0], "names": names, "low_quality_names": low,
                "type": "person" if person else "entity", "programmes": [tx("UN_LIST_TYPE")] if tx("UN_LIST_TYPE") else [],
                "countries": sorted(countries), "listed_on": tx("LISTED_ON") or None,
                "remarks": tx("COMMENTS1")[:300],
            })
        el.clear()
    return entries, generated


def _csv_rows(data: bytes):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return csv.reader(io.StringIO(text))


def _nul(v: str | None) -> str:
    v = (v or "").strip()
    return "" if v == "-0-" else v


def _ofac_name(name: str, typ: str) -> list[str]:
    """OFAC writes individuals as 'LAST, First Middle'. Keep the original and the natural order."""
    out = [name]
    if typ == "person" and "," in name:
        last, first = name.split(",", 1)
        out.append(f"{first.strip()} {last.strip()}")
    return out


def parse_ofac(sdn: bytes, alt: bytes | None, add: bytes | None) -> tuple[list[dict], str | None]:
    by_id: dict[str, dict] = {}
    for r in _csv_rows(sdn):
        if len(r) < 4 or not r[0].strip().isdigit():
            continue
        raw_type = _nul(r[2]).lower()
        typ = {"individual": "person", "vessel": "vessel", "aircraft": "aircraft", "": "entity"}.get(raw_type, "other")
        name = _nul(r[1])
        if not name:
            continue
        programmes = [p.strip(" []") for p in re.split(r"\]\s*\[", _nul(r[3])) if p.strip(" []")]
        remarks = _nul(r[11]) if len(r) > 11 else ""
        names = _ofac_name(name, typ)
        for aka in re.findall(r"a\.k\.a\.\s*'([^']+)'", remarks):
            names += _ofac_name(aka, typ)
        countries = set()
        if len(r) > 9 and _nul(r[9]):
            iso = country_iso2(_nul(r[9]))
            if iso:
                countries.add(iso)
        for m in re.findall(r"(?:nationality|Citizen|citizenship)\s+([A-Za-z ,'\-]+?)(?:;|\.|$)", remarks):
            iso = country_iso2(m)
            if iso:
                countries.add(iso)
        by_id[r[0].strip()] = {
            "list": "ofac", "ref": f"SDN-{r[0].strip()}", "logical_id": r[0].strip(), "primary_name": name,
            "names": names, "type": typ, "programmes": programmes, "countries": countries, "listed_on": None,
            "remarks": remarks[:300],
        }
    if alt:
        for r in _csv_rows(alt):
            if len(r) >= 4 and r[0].strip() in by_id and _nul(r[3]):
                e = by_id[r[0].strip()]
                e["names"] += _ofac_name(_nul(r[3]), e["type"])
    if add:
        for r in _csv_rows(add):
            if len(r) >= 5 and r[0].strip() in by_id:
                iso = country_iso2(_nul(r[4]))
                if iso:
                    by_id[r[0].strip()]["countries"].add(iso)
    entries = []
    for e in by_id.values():
        e["names"] = _clean_names(e["names"])
        e["countries"] = sorted(e["countries"])
        entries.append(e)
    return entries, None


def _dmy(s: str | None) -> str | None:
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", (s or "").strip())
    return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}" if m else None


def _regime(name: str) -> str:
    """'The Russia (Sanctions) (EU Exit) Regulations 2019' -> 'Russia'; other titles stay as written."""
    s = re.sub(r"^The\s+", "", (name or "").strip())
    m = re.match(r"(.+?)\s*\(Sanctions\)", s)
    return (m[1] if m else s).strip()


def parse_uk(data: bytes) -> tuple[list[dict], str | None]:
    """UK Sanctions List XML: one <Designation> per subject; Name1-5 are given names, Name6 the surname or whole name."""
    from lxml import etree

    entries, generated = [], None
    for event, el in etree.iterparse(io.BytesIO(data), events=("end",)):
        if el.tag == "DateGenerated":
            generated = _dmy(el.text)
            continue
        if el.tag != "Designation":
            continue
        tx = lambda t: (el.findtext(t) or "").strip()  # noqa: E731
        typ = {"individual": "person", "entity": "entity", "ship": "vessel"}.get(tx("IndividualEntityShip").lower(), "other")
        names, low = [], []
        for n in el.findall("Names/Name"):
            parts = [re.sub(r"\s+", " ", (n.findtext(f"Name{i}") or "").strip()) for i in range(1, 7)]
            whole = " ".join(p for p in parts if p)
            if not whole:
                continue
            target = low if "low" in (n.findtext("AliasStrength") or "").lower() else names
            if (n.findtext("NameType") or "").strip().lower() == "primary name":
                target.insert(0, whole)
            else:
                target.append(whole)
        names += [x.text for x in el.findall("NonLatinNames/NonLatinName/NameNonLatinScript") if x.text]
        countries = set()
        for path in ("Addresses/Address/AddressCountry", "IndividualDetails/Individual/Nationalities/Nationality",
                     "IndividualDetails/Individual/BirthDetails/Location/CountryOfBirth"):
            for c in el.findall(path):
                iso = country_iso2(c.text)
                if iso:
                    countries.add(iso)
        names = _clean_names(names)
        low = [n for n in _clean_names(low) if n.lower() not in {x.lower() for x in names}]
        if not names and low:
            names, low = low[:1], low[1:]
        if names:
            regime = tx("RegimeName")
            entries.append({
                "list": "uk", "ref": tx("UniqueID"), "logical_id": tx("OFSIGroupID") or tx("UniqueID"),
                "un_ref": tx("UNReferenceNumber") or None, "primary_name": names[0], "names": names,
                "low_quality_names": low, "type": typ, "programmes": [_regime(regime)] if regime else [],
                "regime": regime or None, "countries": sorted(countries), "listed_on": _dmy(tx("DateDesignated")),
                "remarks": (tx("SanctionsImposed") + ". " + tx("OtherInformation"))[:300].strip(". "),
                "designation_source": tx("DesignationSource") or None,
            })
        el.clear()
        while el.getprevious() is not None:
            del el.getparent()[0]
    return entries, generated


_CSL_TAIL = re.compile(r"[;,]?\s*and any successor,? sub-unit,? or subsidiary thereof;?\s*$", re.I)
_CSL_AKA = re.compile(r"\s*(?:\(\s*|-\s*|;\s*|,\s*)?\b(?:a\.?k\.?a|f\.?k\.?a)\b\.?,?:?-?\s*", re.I)
_CSL_ORG = re.compile(r"\b(co|ltd|limited|llc|inc|corp|corporation|company|gmbh|ooo|ao|jsc|oao|pjsc|fze|fzc|fzco|llp|plc|"
                      r"sa|srl|bv|ag|pte|pvt|bhd|institute|university|academy|ministry|bureau|center|centre|group|plant|"
                      r"factory|industries|industry|technology|technologies|trading|shipping|airline|airlines|bank|"
                      r"enterprise|enterprises|services|international|electronics|laboratory|research|department|"
                      r"organization|organisation|association|holding|holdings|logistics|systems|works)\b", re.I)


_CSL_FORMS = _LEGAL_TOKENS - {"as", "ad", "oa", "pp", "dp", "kp", "sl", "ao", "ay", "sa", "tic"}


def _csl_names(raw: str) -> tuple[list[str], str]:
    """Split a CSL name cell into names: 'X (aka Y) (China); and any successor...', 'A -AKA- B', 'P. Affiliated Entity: C'.

    Returns (names, note). A trailing parenthesis that is an acronym or short name of four or more characters becomes
    an alias ('Huawei Technologies Co., Ltd. (Huawei)'); a country tag ('(Russia)', '(PRC entity)') is dropped. Shorter
    acronyms ('(SSI)') are not aliases: three letters match too many unrelated companies."""
    note = ""
    s = re.sub(r"\s+", " ", (raw or "").replace("+B843:B853", "")).strip()
    s = _CSL_TAIL.sub("", s).strip()
    if "Affiliated Entity:" in s:
        parent, s = s.split("Affiliated Entity:", 1)
        note = "Affiliate of " + parent.strip(" .;")
        s = s.strip()
    m = re.search(r"\(\s*Parent Organization:\s*(.+)\)\s*$", s, re.I)
    if m:
        note = "Parent organisation: " + m[1].strip()
        s = s[:m.start()].strip()
    names: list[str] = []
    # "(aka Y)" / "(f.k.a., Y)" / "(AKA: Y)" inside parentheses
    for m in re.finditer(r"\(\s*(?:a\.?k\.?a|f\.?k\.?a)\.?,?:?\s*([^()]+)\)", s, re.I):
        names.append(m[1].strip(" .;,"))
    s = re.sub(r"\(\s*(?:a\.?k\.?a|f\.?k\.?a)\.?,?:?\s*[^()]+\)", " ", s, flags=re.I)
    # trailing parentheses: country tags, acronyms and short names
    while True:
        m = re.search(r"\(([^()]*)\)\s*$", s)
        if not m:
            break
        inner = m[1].strip(" .;,")
        s = s[:m.start()].strip()
        words = normalize(inner).split()
        tag = re.sub(r"\b(entity|national|nationals|company)\b", "", inner, flags=re.I).strip()
        if country_iso2(tag) or tag.upper() in ("PRC", "UAE", "DPRK", "USA", "UK"):
            continue
        if words and (len(words) > 1 or len(words[0]) >= 4) and words[0] not in _LEGAL_TOKENS:
            names.append(inner)
    # "A -AKA- B" / "A; a.k.a. B; a.k.a C" / "A, a.k.a. B"
    parts = [p.strip(" .;,()") for p in _CSL_AKA.split(s) if p and p.strip(" .;,()")]
    s_names = parts[:1] + parts[1:]
    return _clean_names(s_names + names), note


def _csl_abbr(source: str) -> str:
    m = re.search(r"\(([A-Z][A-Z\- ]*?)(?: List)?\)", source or "")
    return (m[1].strip() if m else (source or "?")[:12]).replace(" ", "-")


def parse_csl(data: bytes, today: str | None = None) -> tuple[list[dict], str | None]:
    """US Consolidated Screening List JSON (data.trade.gov bulk download).

    - SDN rows are skipped: the `ofac` list loads the same SDN file straight from Treasury (sdn/alt/add CSV) with the
      same entity numbers, so keeping them would report every SDN hit twice. Every other source is kept, the OFAC
      non-SDN lists (SSI, NS-MBS, CMIC, PLC, CAPTA, FSE) included, because the `ofac` list does not carry them.
    - Denied Persons List orders whose end_date has passed are skipped: the denial no longer applies.
    - The source of each row is kept in `sub_list` ("BIS Entity List", "BIS Denied Persons List", ...) and as the first
      programme, so a hit names the list a person has to look up."""
    import json

    doc = json.loads(data)
    today = today or now().date().isoformat()
    generated = max((s.get("source_last_updated") or "" for s in doc.get("sources_used") or []), default="") or \
        doc.get("search_performed_at")
    entries = []
    for r in doc.get("results") or []:
        abbr = _csl_abbr(r.get("source") or "")
        if abbr == "SDN":
            continue
        end = (r.get("end_date") or "").strip()
        if end and end < today:
            continue
        names, note = _csl_names(r.get("name") or "")
        names += [n for x in (r.get("alt_names") or []) for n in _csl_names(x)[0]]
        names = _clean_names(names)
        if not names:
            continue
        raw_type = (r.get("type") or "").lower()
        typ = {"individual": "person", "entity": "entity", "vessel": "vessel", "aircraft": "aircraft"}.get(raw_type)
        if typ is None:
            if any(_CSL_ORG.search(n) or set(re.findall(r"[a-z]+", _ascii(n))) & _CSL_FORMS for n in names) \
                    or re.search(r"\d", names[0]):
                typ = "entity"
            elif abbr in ("DPL", "DTC") and 2 <= len(names[0].split()) <= 5:
                typ = "person"  # these two lists name mostly individuals and carry no type
        countries = set()
        for a in r.get("addresses") or []:
            iso = country_iso2((a or {}).get("country"))
            if iso:
                countries.add(iso)
        for c in (r.get("nationalities") or []) + (r.get("citizenships") or []) + [r.get("country")]:
            iso = country_iso2(c) if isinstance(c, str) else None
            if iso:
                countries.add(iso)
        sub = CSL_SUB_LISTS.get(abbr, r.get("source") or abbr)
        programmes = [sub] + [p for p in (r.get("programs") or []) if p and p != sub]
        remarks = "; ".join(x for x in (note, r.get("license_policy") or "", r.get("federal_register_notice") or "",
                                         (r.get("remarks") or "")) if x)
        entries.append({
            "list": "csl", "ref": f"CSL-{abbr}-{r.get('entity_number') or str(r.get('id') or '')[:12]}",
            "logical_id": str(r.get("id") or ""), "primary_name": names[0], "names": names, "type": typ,
            "programmes": programmes, "sub_list": sub, "countries": sorted(countries),
            "listed_on": r.get("start_date") or None, "end_date": end or None, "remarks": remarks[:300],
            "source_list_url": r.get("source_list_url"),
        })
    return entries, generated


def csl_api_key() -> str | None:
    import os

    return (os.environ.get("TRADE_GOV_API_KEY") or "").strip() or None


async def csl_live_search(name: str, *, timeout: float = 15.0) -> dict | None:
    """Second opinion from the live CSL search API (fuzzy name search, score 0-100). Needs TRADE_GOV_API_KEY (free
    subscription key from developer.trade.gov); returns None without it. The key travels only in the request header
    and is never logged."""
    key = csl_api_key()
    if not key or not (name or "").strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(CSL_API_URL, params={"name": name, "fuzzy_name": "true", "size": 10},
                                 headers={"subscription-key": key, "User-Agent": "Huolitsija sanctions screening"})
        r.raise_for_status()
        body = r.json()
    except Exception as exc:  # noqa: BLE001 - the bulk file stays the source; record that the API did not answer
        return {"source": CSL_API_URL, "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}", "results": []}
    res = []
    for x in body.get("results") or []:
        res.append({"name": x.get("name"), "source": x.get("source"), "sub_list": CSL_SUB_LISTS.get(_csl_abbr(x.get("source") or "")),
                    "score": x.get("score"), "matched_name": x.get("matched_name"), "id": x.get("id"),
                    "programs": x.get("programs") or []})
    return {"source": CSL_API_URL, "status": "ok", "total": body.get("total"), "results": res, "checked_at": now_iso()}


# ---------------------------------------------------------------------------------------------
# Download + store
# ---------------------------------------------------------------------------------------------
_last_modified: dict[str, str] = {}
_file_sha: dict[str, str] = {}  # sha256 of the downloaded file (for csl `sha256` is the digest of the parsed entries)


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    r = await client.get(url)
    r.raise_for_status()
    if not r.content:
        raise ValueError(f"empty response from {url}")
    lm = r.headers.get("last-modified")
    if lm:
        try:
            from email.utils import parsedate_to_datetime

            from core.util import iso

            _last_modified[url] = iso(parsedate_to_datetime(lm))
        except (TypeError, ValueError):
            pass
    return r.content


async def _fetch_and_parse(key: str, client: httpx.AsyncClient) -> tuple[list[dict], str | None, str]:
    if key == "eu":
        data = await _download(client, EU_URL)
        entries, gen = await asyncio.to_thread(parse_eu, data)
        digest = hashlib.sha256(data).hexdigest()
    elif key == "un":
        data = await _download(client, UN_URL)
        entries, gen = await asyncio.to_thread(parse_un, data)
        digest = hashlib.sha256(data).hexdigest()
    elif key == "ofac":
        sdn, alt, add = await asyncio.gather(_download(client, OFAC_SDN_URL), _download(client, OFAC_ALT_URL),
                                             _download(client, OFAC_ADD_URL))
        entries, gen = await asyncio.to_thread(parse_ofac, sdn, alt, add)
        gen = gen or _last_modified.get(OFAC_SDN_URL)  # the CSV carries no date; the server's Last-Modified does
        digest = hashlib.sha256(sdn + alt + add).hexdigest()
    elif key == "uk":
        data = await _download(client, UK_URL)
        entries, gen = await asyncio.to_thread(parse_uk, data)
        gen = gen or _last_modified.get(UK_URL)
        digest = hashlib.sha256(data).hexdigest()
    elif key == "csl":
        data = await _download(client, CSL_URL)
        entries, gen = await asyncio.to_thread(parse_csl, data)
        # the hourly import stamp changes every hour; the digest covers the entries, so only a content change is stored
        digest = hashlib.sha256(repr(sorted((e["ref"], tuple(e["names"]), e.get("end_date") or "") for e in entries))
                                .encode()).hexdigest()
    else:
        raise ValueError(f"unknown list {key}")
    minimum = {"eu": 1000, "un": 500, "ofac": 5000, "uk": 3000, "csl": 3000}[key]
    if len(entries) < minimum:
        raise ValueError(f"{key}: only {len(entries)} subjects parsed (expected at least {minimum}); file format changed?")
    _file_sha[key] = hashlib.sha256(data).hexdigest() if key != "ofac" else digest
    return entries, gen, digest


def _prepare(entries: list[dict], version: str) -> list[dict]:
    for e in entries:
        e["id"] = new_id()
        e["version"] = version
        e["norm"] = sorted({x for x in (normalize(n) for n in e["names"] + e.get("low_quality_names", [])) if x})
    return entries


async def _store(key: str, entries: list[dict], generated: str | None, digest: str, started: float) -> str:
    """Store a new version of one list and drop the previous one. Returns the new version id."""
    version = new_id()
    entries = await asyncio.to_thread(_prepare, entries, version)
    for i in range(0, len(entries), 1000):
        await db.sanctions_entries.insert_many([dict(e) for e in entries[i:i + 1000]], ordered=False)
    await db.sanctions_lists.update_one({"list": key}, {"$set": {
        "list": key, "label": LIST_SOURCES[key]["label"], "source_url": LIST_SOURCES[key]["url"],
        "fetched_at": now_iso(), "generated_at": generated, "entries": len(entries), "sha256": digest,
        "file_sha256": _file_sha.get(key, digest),
        "status": "ok", "error": None, "version": version, "duration_s": round(time.monotonic() - started, 1),
    }}, upsert=True)
    await db.sanctions_entries.delete_many({"list": key, "version": {"$ne": version}})
    _meta_cache["at"] = 0.0  # the next screening rebuilds the index from the new version
    return version


async def refresh_lists(force: bool = False, only: list[str] | None = None) -> list[dict]:
    """Download lists whose last successful fetch is older than 24 h (or all with force=True)."""
    async with _refresh_lock:
        keys = [k for k in LIST_SOURCES if not only or k in only]
        metas = {m["list"]: m async for m in db.sanctions_lists.find({}, NO_ID)}
        due = []
        for k in keys:
            m = metas.get(k) or {}
            last_ok = parse_iso(m.get("fetched_at")) if m.get("version") else None
            if force or not last_ok or (now() - last_ok).total_seconds() > MAX_AGE_HOURS * 3600:
                due.append(k)
        changed: list[dict] = []
        if due:
            async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=20), follow_redirects=True,
                                         headers={"User-Agent": "Huolitsija sanctions screening (+https://huolitsija.fi)"}) as client:
                for k in due:
                    started = time.monotonic()
                    try:
                        entries, gen, digest = await _fetch_and_parse(k, client)
                        prev = metas.get(k) or {}
                        if prev.get("sha256") == digest and prev.get("version") and not force:
                            await db.sanctions_lists.update_one({"list": k}, {"$set": {
                                "fetched_at": now_iso(), "status": "ok", "error": None}})
                        else:
                            version = await _store(k, entries, gen, digest, started)
                            changed.append({"list": k, "version": version, "previous_version": prev.get("version"),
                                            "content_changed": prev.get("sha256") != digest, "entries": len(entries)})
                        log.info("sanctions list %s refreshed: %d subjects in %.1fs", k, len(entries),
                                 time.monotonic() - started)
                    except Exception as exc:  # noqa: BLE001 - keep the previous version, record the failure
                        log.warning("sanctions list %s refresh failed: %s", k, exc)
                        await db.sanctions_lists.update_one({"list": k}, {"$set": {
                            "list": k, "label": LIST_SOURCES[k]["label"], "source_url": LIST_SOURCES[k]["url"],
                            "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                            "last_attempt_at": now_iso()}}, upsert=True)
    if changed:
        await _lists_changed(changed)
    return await lists_status()


async def _lists_changed(changed: list[dict]):
    """A new list version was stored: services.party_screening re-screens open shipments and active customers."""
    from core.events import emit

    log.info("sanctions lists changed: %s", ", ".join(c["list"] for c in changed))
    await emit("sanctions.lists_changed", {"lists": [c["list"] for c in changed], "changes": changed,
                                           "versions": await _current_versions(force=True)})


async def lists_status() -> list[dict]:
    metas = {m["list"]: m async for m in db.sanctions_lists.find({}, NO_ID)}
    out = []
    for k, src in LIST_SOURCES.items():
        m = metas.get(k) or {}
        out.append({
            "list": k, "label": src["label"], "short": src["short"], "source_url": src["url"],
            "authority": src["authority"], "fetched_at": m.get("fetched_at"), "generated_at": m.get("generated_at"),
            "entries": m.get("entries", 0), "sha256": m.get("sha256"), "file_sha256": m.get("file_sha256"),
            "version": m.get("version"),
            "status": m.get("status") or "never", "error": m.get("error"),
            "loaded": bool(m.get("version")), "last_attempt_at": m.get("last_attempt_at"),
        })
    return out


# ---------------------------------------------------------------------------------------------
# In-memory index
# ---------------------------------------------------------------------------------------------
class _Index:
    def __init__(self):
        self.versions: dict[str, str] = {}
        self.entries: list[dict] = []
        self.alias_entry: list[int] = []
        self.alias_toks: list[tuple[str, ...]] = []
        self.alias_keys: list[tuple[str, ...]] = []
        self.alias_low: list[bool] = []
        self.alias_flat: list[str] = []
        self.alias_raw: list[str] = []
        self.postings: dict[str, list[int]] = defaultdict(list)
        self.df: dict[str, int] = defaultdict(int)
        self.n = 1
        self.built_at = None
        self.build_s = 0.0

    def idf(self, key: str) -> float:
        return math.log((self.n + 1) / (self.df.get(key, 0) + 1)) + 1.0


_index = _Index()
_index_lock = asyncio.Lock()
_meta_cache: dict = {"at": 0.0, "versions": {}}


async def _current_versions(force: bool = False) -> dict[str, str]:
    if not force and time.monotonic() - _meta_cache["at"] < 30:
        return _meta_cache["versions"]
    versions = {m["list"]: m["version"] async for m in db.sanctions_lists.find({"version": {"$ne": None}},
                                                                              {"_id": 0, "list": 1, "version": 1})
                if m.get("version")}
    _meta_cache.update(at=time.monotonic(), versions=versions)
    return versions


_ENTRY_FIELDS = ("list", "ref", "primary_name", "type", "programmes", "countries", "listed_on", "remarks", "sub_list")


class _Entry:
    """One listed subject in the index. Slots instead of a dict per subject (38k subjects), read like a dict."""
    __slots__ = _ENTRY_FIELDS

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        v = getattr(self, key, None)
        return default if v is None else v

    def keys(self):
        return _ENTRY_FIELDS


_shared: dict = {}


def _share(v):
    """One object for repeated values (list keys, types, programme and country lists) across all subjects."""
    if isinstance(v, str):
        return sys.intern(v) if len(v) <= 64 else v
    if isinstance(v, list):
        v = tuple(_share(x) for x in v)
        return _shared.setdefault(v, v)
    return v


def _add_docs(ix: _Index, docs: list[dict]) -> None:
    """Index a batch of list entries. Words and their keys are interned: the same word appears in thousands of aliases."""
    intern = sys.intern
    for d in docs:
        ei = len(ix.entries)
        e = _Entry()
        for k in _ENTRY_FIELDS:
            setattr(e, k, d.get(k) if k in ("ref", "primary_name", "remarks", "listed_on") else _share(d.get(k)))
        ix.entries.append(e)
        low_set = {n.lower() for n in d.get("low_quality_names") or []}
        seen = set()
        for raw in (d.get("names") or []) + (d.get("low_quality_names") or []):
            norm = normalize(raw)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            toks = tuple(intern(t) for t in norm.split())
            keys = tuple(intern(_tkey(t)) for t in toks)
            ai = len(ix.alias_entry)
            ix.alias_entry.append(ei)
            ix.alias_toks.append(toks)
            ix.alias_keys.append(keys)
            ix.alias_low.append(raw.lower() in low_set)
            ix.alias_flat.append("".join(keys))
            ix.alias_raw.append(raw)
            postkeys = set()
            for k in keys:
                postkeys.add("t:" + k)
                sk = _skel(k)
                if len(sk) >= 3:
                    postkeys.add("s:" + sk)
            for pk in postkeys:
                ix.postings[pk].append(ai)
            for k in set(keys):
                ix.df[k] += 1


def _finish(ix: _Index, started: float) -> _Index:
    ix.alias_entry = array("i", ix.alias_entry)
    ix.postings = {intern_key: array("i", ids) for intern_key, ids in ix.postings.items()}  # 4 bytes per id, not a list slot
    ix.df = dict(ix.df)
    ix.n = max(1, len(ix.alias_entry))
    ix.built_at = now_iso()
    ix.build_s = round(time.monotonic() - started, 2)
    return ix


def _build(docs: list[dict], versions: dict[str, str]) -> _Index:
    started = time.monotonic()
    ix = _Index()
    ix.versions = dict(versions)
    _add_docs(ix, docs)
    return _finish(ix, started)


_BUILD_BATCH = 2000


async def _get_index() -> _Index:
    global _index
    versions = await _current_versions()
    if versions and versions == _index.versions:
        return _index
    async with _index_lock:
        versions = await _current_versions()
        if versions == _index.versions:
            return _index
        proj = {"_id": 0, "list": 1, "ref": 1, "primary_name": 1, "names": 1, "low_quality_names": 1, "type": 1,
                "programmes": 1, "countries": 1, "listed_on": 1, "remarks": 1, "sub_list": 1}
        # Streamed in batches: the list documents are never all in memory next to the index being built. The old index
        # is dropped first, so a rebuild after a list refresh does not hold two indexes at once.
        started = time.monotonic()
        _index = _Index()
        ix = _Index()
        ix.versions = dict(versions)
        for lst, ver in versions.items():
            batch = []
            async for d in db.sanctions_entries.find({"list": lst, "version": ver}, proj).batch_size(_BUILD_BATCH):
                batch.append(d)
                if len(batch) >= _BUILD_BATCH:
                    await asyncio.to_thread(_add_docs, ix, batch)
                    batch = []
            if batch:
                await asyncio.to_thread(_add_docs, ix, batch)
        _index = _finish(ix, started)
        log.info("sanctions index built: %d subjects, %d aliases in %.2fs", len(_index.entries),
                 len(_index.alias_entry), _index.build_s)
        return _index


# ---------------------------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------------------------
_COMMON_DF = 2500  # tokens shared by more aliases than this do not drive candidate retrieval
_PRUNE_ABOVE = 1200  # above this many candidates, aliases sharing a single low-weight word are not scored


@lru_cache(maxsize=262144)  # pure; list words repeat across thousands of aliases ("institute", "research")
def _tok_sim(qt: str, at: str, qk: str, ak: str) -> float:
    """Similarity of two name words (raw tokens qt/at, transliteration keys qk/ak).

    Words of three letters or fewer (acronyms, initials) must be identical; longer words may differ by
    transliteration (key equality) or by a small spelling difference (Jaro-Winkler on the keys)."""
    if qt == at:
        return 1.0
    if len(qt) <= 3 or len(at) <= 3:
        return 0.0
    if qk == ak:
        return 0.98
    s = JaroWinkler.normalized_similarity(qk, ak)
    sq, sa = _skel(qk), _skel(ak)
    if len(sq) >= 4 and sq == sa and qk[0] == ak[0]:
        s = max(s, 0.92)
    need = 0.93 if min(len(qk), len(ak)) <= 5 else 0.88
    return s if s >= need else 0.0


def _align(qtoks, qkeys, atoks, akeys):
    """Greedy best-first alignment of query words to alias words.

    Besides word-to-word pairs it accepts two adjacent words written as one ("al qaida" / "alqaida",
    "gazprom bank" / "gazprombank") when the joined transliteration keys are identical.
    Returns [(q_indices, a_indices, sim)]."""
    cands = []
    for qi, (qt, qk) in enumerate(zip(qtoks, qkeys)):
        for ai, (at, ak) in enumerate(zip(atoks, akeys)):
            s = _tok_sim(qt, at, qk, ak)
            if s:
                cands.append((s, 1, (qi,), (ai,)))
    for ai in range(len(akeys) - 1):
        joined = akeys[ai] + akeys[ai + 1]
        for qi, qk in enumerate(qkeys):
            if qk == joined and len(qk) >= 5:
                cands.append((0.97, 2, (qi,), (ai, ai + 1)))
    for qi in range(len(qkeys) - 1):
        joined = qkeys[qi] + qkeys[qi + 1]
        for ai, ak in enumerate(akeys):
            if ak == joined and len(ak) >= 5:
                cands.append((0.97, 2, (qi, qi + 1), (ai,)))
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
    used_q, used_a, out = set(), set(), []
    for s, _n, qs, as_ in cands:
        if used_q.intersection(qs) or used_a.intersection(as_):
            continue
        used_q.update(qs)
        used_a.update(as_)
        out.append((qs, as_, s))
    return out


def _weight(ix: _Index, key: str, tok: str, person: bool = True) -> float:
    w = ix.idf(key)
    if len(tok) == 1 and person:
        w *= 0.3  # initials of a person; in a company name ("N Holding") the letter is the name
    elif _PATRONYMIC.search(tok):
        w *= 0.4  # patronymics are often missing from the screened name
    return w


def _score(ix: _Index, qtoks, qkeys, ai: int) -> tuple[float, dict]:
    akeys, atoks = ix.alias_keys[ai], ix.alias_toks[ai]
    al = _align(qtoks, qkeys, atoks, akeys)
    if not al:
        return 0.0, {}
    wq = [_weight(ix, k, t) for k, t in zip(qkeys, qtoks)]
    a_person = ix.entries[ix.alias_entry[ai]].get("type") == "person"
    wa = [_weight(ix, k, t, a_person) for k, t in zip(akeys, atoks)]
    cov_q = sum(sum(wq[i] for i in qs) * s for qs, _as, s in al) / sum(wq)
    cov_a = sum(sum(wa[i] for i in as_) * s for _qs, as_, s in al) / sum(wa)
    score = (cov_q ** 0.6) * (cov_a ** 0.4)
    reason = "token"
    # A listed name of two or more words fully contained in the screened name, e.g. the subsidiary
    # "Rosneft Oil Company Finland Oy" of the listed "Rosneft Oil Company". One-word listed names are not
    # used this way: ordinary words ("Helios", "Polar", "Energiya") are listed too and would flag half
    # of Finland.
    matched_a = {i for _qs, as_, _s in al for i in as_}
    informative = max((ix.idf(akeys[i]) for i in matched_a), default=0.0)
    if len(akeys) >= 2 and len(matched_a) >= 2 and cov_a >= 0.97 and informative >= 7.0:
        contained = 0.86 + 0.08 * cov_q
        if contained > score:
            score, reason = contained, "contained"
    # A distinctive one-word listed entity name followed only by a place or a generic trade word, e.g. "Rosneft
    # Finland Oy" or "Sovcomflot Europe" for the listed "Rosneft" / "Sovcomflot": local branches and subsidiaries are
    # how sanctioned names usually reach a Finnish importer. Ordinary listed words ("Helios") stay clear in names
    # like "Helios One Oy" because "One" is neither a place nor a generic word.
    if len(akeys) == 1 and not a_person and len(atoks[0]) >= 5 and ix.idf(akeys[0]) >= 8.0:
        exact_q = {i for qs, as_, sim in al if sim >= 0.999 for i in qs}
        rest = [qtoks[i] for i in range(len(qtoks)) if i not in exact_q]
        if exact_q and rest and len(exact_q) == 1 and all(_generic_token(t) for t in rest):
            if 0.88 > score:
                score, reason = 0.88, "contained"
    if ix.alias_low[ai]:
        score *= 0.95
    return min(score, 1.0), {"pairs": al, "cov_q": cov_q, "cov_a": cov_a, "reason": reason}


_GENERIC_WORDS = {"international", "intl", "trading", "trade", "europe", "european", "eu", "nordic", "nordics",
                  "scandinavia", "scandinavian", "baltic", "baltics", "global", "suomi", "eesti", "sverige", "norge",
                  "danmark", "deutschland", "branch", "filial", "sivuliike", "representative", "office"}


def _generic_token(tok: str) -> bool:
    """A place name or a generic trade word that does not change whose company it is."""
    return tok in _GENERIC_WORDS or (len(tok) > 2 and country_iso2(tok) is not None)


def list_label(entry: dict) -> str:
    """'EU consolidated financial sanctions list'; a CSL hit names its sub-list: 'US Consolidated Screening List: BIS
    Entity List'."""
    label = LIST_SOURCES[entry["list"]]["label"]
    return f"{label}: {entry['sub_list']}" if entry.get("sub_list") else label


def _explain(ix, entry, ai, qtoks, detail, score, kind, country) -> str:
    progs = ", ".join(p for p in entry.get("programmes") or [] if p != entry.get("sub_list"))
    raw = ix.alias_raw[ai]
    atoks = ix.alias_toks[ai]
    parts = [f"Matched “{raw}” on the {list_label(entry)} (ref {entry['ref']}{'; programme ' + progs if progs else ''})."]
    pairs = detail.get("pairs") or []
    if detail.get("reason") == "contained":
        parts.append("The whole listed name appears in the screened name.")
    words, n_q = [], 0
    for qs, as_, s in sorted(pairs):
        q_w, a_w = " ".join(qtoks[i] for i in qs), " ".join(atoks[i] for i in as_)
        words.append(q_w if s >= 0.999 else f"{q_w} ≈ {a_w}")
        n_q += len(qs)
    if words:
        parts.append(f"{n_q} of {len(qtoks)} screened words match ({', '.join(words)}); "
                     f"the listed name has {len(atoks)} word{'s' if len(atoks) != 1 else ''}.")
    parts.append(f"Score {score:.2f}.")
    typ = entry.get("type")
    if kind != "any" and typ and typ != kind:
        parts.append(f"Listed as {typ}, screened as {kind}.")
    if country and entry.get("countries"):
        parts.append(f"Country {country.upper()} {'matches' if country.upper() in entry['countries'] else 'differs from'} "
                     f"the listing ({', '.join(entry['countries'][:6])}).")
    return " ".join(parts)


def _match(ix: _Index, name: str, kind: str, country: str | None, limit: int) -> tuple[str, list[dict], dict | None, float]:
    norm = normalize(name)
    qtoks = tuple(norm.split())
    qkeys = tuple(_tkey(t) for t in qtoks)
    if not qtoks:
        return norm, [], None, 0.0
    cand: set[int] = set()
    lookup = []
    for k in set(qkeys):
        lookup.append("t:" + k)
        sk = _skel(k)
        if len(sk) >= 3:
            lookup.append("s:" + sk)
    rare = {pk for pk in lookup if len(ix.postings.get(pk, ())) <= _COMMON_DF}
    for pk in (rare or lookup):
        cand.update(ix.postings.get(pk, ()))
    if len(cand) > _PRUNE_ABOVE and len(set(qkeys)) >= 2:
        # Many aliases share one of the words (Mohammed, Institute, Research): score only those that share two words or
        # whose shared word carries most of the name's weight. Keeps the 119k-alias index within ~100 ms per name.
        w = {k: ix.idf(k) for k in set(qkeys)}
        total = sum(w.values())
        hit_w: dict[int, float] = defaultdict(float)
        hit_n: dict[int, int] = defaultdict(int)
        for k in w:
            ids = set()
            for pk in ("t:" + k, "s:" + _skel(k)):
                if pk in rare or not rare:
                    ids.update(ix.postings.get(pk, ()))
            for ai in ids:
                hit_w[ai] += w[k]
                hit_n[ai] += 1
        cand = {ai for ai in cand if hit_n[ai] >= 2 or hit_w[ai] >= 0.6 * total}
    qflat = "".join(qkeys)
    if len(qflat) >= 6:
        for _m, _s, ai in process.extract(qflat, ix.alias_flat, scorer=fuzz.ratio, score_cutoff=88, limit=25):
            cand.add(ai)
    best: dict[int, tuple[float, int, dict]] = {}
    for ai in cand:
        s, detail = _score(ix, qtoks, qkeys, ai)
        if s < REVIEW_FLOOR * 0.9:
            continue
        ei = ix.alias_entry[ai]
        e = ix.entries[ei]
        typ = e.get("type")
        if kind != "any" and typ:
            if (kind == "person") != (typ == "person"):
                s *= 0.9 if typ in ("person", "entity") else 0.95
        if country and e.get("countries") and country.upper() in e["countries"]:
            s = min(1.0, s + 0.02)
        if ei not in best or s > best[ei][0]:
            best[ei] = (s, ai, detail)
    ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
    hits, closest = [], None
    for ei, (s, ai, detail) in ranked:
        e = ix.entries[ei]
        hit = {
            "list": e["list"], "list_label": list_label(e), "sub_list": e.get("sub_list"), "ref": e["ref"],
            "matched_name": ix.alias_raw[ai], "primary_name": e["primary_name"], "type": e.get("type"),
            "programmes": e.get("programmes") or [], "countries": e.get("countries") or [],
            "listed_on": e.get("listed_on"), "score": round(s, 3),
            "explanation": _explain(ix, e, ai, qtoks, detail, s, kind, country),
        }
        if s >= MATCH_THRESHOLD:
            if len(hits) < limit:
                hits.append(hit)
        elif closest is None and s >= REVIEW_FLOOR:
            hit["explanation"] = ("Below the match threshold " + f"{MATCH_THRESHOLD:.2f}. Closest listed name: "
                                  + hit["explanation"].removeprefix("Matched "))
            closest = hit
    top = ranked[0][1][0] if ranked else 0.0
    return norm, hits, closest, top


async def screen_name(name: str, *, kind: str = "any", country: str | None = None, limit: int = 5) -> dict:
    """Screen one party name against all loaded lists. Raises ValueError for an empty name."""
    if not name or not normalize(name):
        raise ValueError("A name is required for sanctions screening")
    if kind not in ("entity", "person", "vessel", "any"):
        raise ValueError("kind must be entity, person, vessel or any")
    status_lists = await lists_status()
    if not any(s["loaded"] for s in status_lists):
        status_lists = await refresh_lists()
    lists = [{"list": s["list"], "label": s["label"], "generated_at": s["generated_at"], "fetched_at": s["fetched_at"],
              "entries": s["entries"], "status": s["status"], "loaded": s["loaded"]} for s in status_lists]
    complete = all(s["loaded"] for s in status_lists)
    base = {"query": name, "kind": kind, "country": country, "lists": lists, "complete": complete,
            "screened_at": now_iso(), "threshold": MATCH_THRESHOLD}
    if not any(s["loaded"] for s in status_lists):
        errors = "; ".join(f"{s['short']}: {s['error'] or 'not loaded'}" for s in status_lists)
        return {**base, "normalized": normalize(name), "status": "error", "top_score": None, "hits": [],
                "closest": None, "reason": f"No sanctions list could be loaded ({errors}). Screening was not performed."}
    ix = await _get_index()
    started = time.monotonic()
    norm, hits, closest, top = _match(ix, name, kind, country, limit)
    out = {**base, "normalized": norm, "status": "potential_match" if hits else "clear", "top_score": round(top, 3),
           "hits": hits, "closest": None if hits else closest, "latency_ms": int((time.monotonic() - started) * 1000)}
    if not complete:
        missing = [s["short"] for s in status_lists if not s["loaded"]]
        out["reason"] = f"Screened without {', '.join(missing)} (list not loaded); re-screen when it is available."
    return out


async def screen_parties(parties: list[dict]) -> dict:
    """parties: [{name, kind, country, role}] -> worst status over all parties plus per-party results."""
    results, statuses = [], []
    for p in parties or []:
        if not (p.get("name") or "").strip():
            continue
        try:
            r = await screen_name(p["name"], kind=p.get("kind") or "any", country=p.get("country"))
        except ValueError as exc:
            r = {"query": p.get("name"), "status": "error", "reason": str(exc), "hits": []}
        r["role"] = p.get("role")
        results.append(r)
        statuses.append(r["status"])
    status = "error" if "error" in statuses else "potential_match" if "potential_match" in statuses else "clear"
    if not results:
        status = "error"
    return {"status": status, "results": results, "screened_at": now_iso()}


async def index_stats() -> dict:
    ix = await _get_index()
    return {"subjects": len(ix.entries), "aliases": len(ix.alias_entry), "built_at": ix.built_at, "build_s": ix.build_s}


# ---------------------------------------------------------------------------------------------
# Lifecycle: refresh daily; first load in the background so startup never waits for a download.
# ---------------------------------------------------------------------------------------------
_bg_tasks: set = set()


@on_startup(order=45)
async def _initial_load():
    if not await db.sanctions_lists.find_one({"version": {"$ne": None}}):
        task = asyncio.create_task(refresh_lists())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)


@background_loop(seconds=3600)
async def _daily_refresh():
    await refresh_lists()
