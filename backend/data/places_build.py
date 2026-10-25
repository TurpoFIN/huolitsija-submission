"""Builds data/places_geonames.json (world city gazetteer) and data/countries_i18n.json from real public datasets.

Sources
- GeoNames (CC BY 4.0, https://www.geonames.org/), columns documented in
  https://download.geonames.org/export/dump/readme.txt:
  - https://download.geonames.org/export/dump/cities15000.zip: every populated place with population >= 15 000
    (plus capitals); tab-separated geonameid, name, asciiname, alternatenames, lat, lon, feature class/code,
    country, cc2, admin1..4, population, elevation, dem, timezone, modification date.
  - https://download.geonames.org/export/dump/admin1CodesASCII.txt: "CC.admin1" -> English admin1 name, so the app
    can show "Portland, Oregon" vs "Portland, Maine".
  - https://download.geonames.org/export/dump/countryInfo.txt: ISO2, English country name, continent, languages.
  - https://download.geonames.org/export/dump/alternateNamesV2.zip (205 MB, build-only, cached): alternate names
    with language code and preferred / short / colloquial / historic flags. Used to rank aliases (the convenience
    column in cities15000 is unordered and full of machine transliterations) and to reject historic Wikidata labels.
- Wikidata (CC0), SPARQL endpoint https://query.wikidata.org/sparql:
  - Finnish labels (rdfs:label @fi) of items with a GeoNames ID (P1566) and population (P1082) > 100 000, mapped
    geonameid -> Finnish name (Tukholma, Lontoo, Pariisi, Kööpenhamina, Peking, Pietari, Tallinna, Riika, Varsova).
    Finland and the neighbouring countries (NEAR) get labels for every cities15000 place regardless of population,
    so Viipuri, Tartto, Luulaja, Uumaja, Tromssa and Kantalahti resolve too. The one-shot query sits near the
    endpoint's 60 s limit; when it fails the same lookup runs in batches of geonameids (VALUES), and when Wikidata is
    unreachable the name_fi column of the previous output is kept.
  - Municipalities of Finland (Q856076) with Finnish and Swedish labels: GeoNames names some Finnish towns in
    Swedish (Jakobstad -> Pietarsaari, Hyvinge -> Hyvinkää, Sibbo -> Sipoo, Korsholm -> Mustasaari).
  A label is kept only when it differs from the GeoNames name (ignoring case, accents, punctuation and a trailing
  City/Town/-shi), is not an urban-area / parish-centre / station item ("Tampereen keskustaajama"), is not listed
  by GeoNames only as a historic name ("Christiania" for Oslo), and GeoNames also lists it or it is a close
  transliteration of the GeoNames name (Tšeljabinsk). For Finnish and Åland places GeoNames must list it.
  Where Wikidata has no label for the geonameid (Lübeck's item carries another GeoNames ID), the place's
  Finnish-tagged GeoNames alternate name is used (Lyypekki, Napoli, Firenze, Maarianhamina).
- Unicode CLDR via the `babel` package (Unicode License): country names in Finnish, English, Swedish and German
  (Locale(..).territories), plus the hand-written COUNTRY_ALIASES below (USA, UK, Englanti, Holland, UAE ...).
  Aliases that would name two countries ("congo") are dropped.

Rows: every cities15000 place in every country, including RU and BY (the app must recognise sanctioned places to
refuse them with the right message). aliases = up to MAX_ALIASES Latin-script alternate names, deduplicated
ignoring case/accents/punctuation, without code-like entries (IATA/ICAO, digits) or machine transliterations,
ranked by language (en/fi/sv/de and the country's primary language first), GeoNames flags, then shorter first.
Above SIZE_TARGET the aliases of smaller places are trimmed first.

Run: ~/work/venv/bin/python /home/user/work/huolitsija/backend/data/places_build.py [cache_dir]
     ~/work/venv/bin/python /home/user/work/huolitsija/backend/data/places_build.py --check   (checks only)
"""
import io
import json
import re
import sys
import unicodedata
import zipfile
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
OUT = HERE / "places_geonames.json"
OUT_COUNTRIES = HERE / "countries_i18n.json"
GEONAMES = "https://download.geonames.org/export/dump/"
CITIES_URL = GEONAMES + "cities15000.zip"
ADMIN1_URL = GEONAMES + "admin1CodesASCII.txt"
COUNTRY_URL = GEONAMES + "countryInfo.txt"
ALT_URL = GEONAMES + "alternateNamesV2.zip"
SPARQL_URL = "https://query.wikidata.org/sparql"
UA = {"User-Agent": "Huolitsija-build/1.0 (ops@huolitsija.fi)"}

COLUMNS = ["id", "name", "ascii", "name_fi", "country", "admin1", "lat", "lon", "population", "aliases", "tz"]
MAX_ALIASES = 12
SIZE_TARGET = 6_000_000  # bytes; aliases of smaller places are trimmed until the file fits
FI_MIN_POP = 100_000
HOME = {"FI", "AX"}  # Finland and Åland: Finnish names must also be listed by GeoNames
NEAR = HOME | {"SE", "NO", "DK", "EE", "LV", "LT", "RU"}  # Finnish labels at any population
CLDR_LOCALES = ["en", "fi", "sv", "de"]
LANG_RE = re.compile(r"(?:[a-z]{2,3}(?:-[A-Za-z0-9]+)?)?")  # ISO 639 (+ variant) or untagged
# Alias ranking tiers: the app's languages and the place's own languages, then other European trade languages.
TIER1 = {"en", "fi", "sv", "de"}
TIER2 = set("fr es it pt nl pl et da nb nn no cs sk sl hr lv lt ro hu tr ca is ga".split())

# Finnish display names where CLDR's standard form is the long official one.
# AN and CS are retired codes GeoNames still lists; CLDR has no name for them.
FI_DISPLAY = {"HK": "Hongkong", "MO": "Macao", "MM": "Myanmar", "AN": "Alankomaiden Antillit",
              "CS": "Serbia ja Montenegro"}

# Everyday Finnish/English names that CLDR (en/fi/sv/de) and GeoNames do not carry.
COUNTRY_ALIASES = {
    "US": ["USA", "U.S.A.", "US", "U.S.", "United States of America", "America", "Amerikka", "Amerikan yhdysvallat",
           "Jenkkilä", "Yhdysvallat"],
    "GB": ["UK", "U.K.", "Great Britain", "Britain", "England", "Englanti", "Iso-Britannia", "Britannia", "Scotland",
           "Skotlanti", "Wales", "Northern Ireland", "Pohjois-Irlanti", "Yhdistynyt kuningaskunta"],
    "NL": ["Holland", "Hollanti", "The Netherlands", "Alankomaat"],
    "KR": ["Etelä-Korea", "South Korea", "Republic of Korea", "Korean tasavalta"],
    "KP": ["Pohjois-Korea", "North Korea", "DPRK", "Korean demokraattinen kansantasavalta"],
    "AE": ["UAE", "U.A.E.", "Arabiemiirikunnat", "Yhdistyneet arabiemiirikunnat", "Emirates", "Emiraatit"],
    "CN": ["Kiina", "China", "PRC", "Kiinan kansantasavalta", "Manner-Kiina", "Mainland China"],
    "DE": ["Saksa", "Germany", "Deutschland"],
    "SE": ["Ruotsi", "Sweden", "Sverige"],
    "RU": ["Venäjä", "Russia", "Russian Federation", "Venäjän federaatio", "Rossija"],
    "BY": ["Valko-Venäjä", "Valkovenäjä", "Belarus", "Byelorussia", "Belorussia"],
    "NO": ["Norja", "Norway", "Norge"],
    "DK": ["Tanska", "Denmark", "Danmark"],
    "EE": ["Viro", "Eesti", "Estonia"],
    "LV": ["Latvia", "Latvija"],
    "LT": ["Liettua", "Lithuania", "Lietuva"],
    "PL": ["Puola", "Poland", "Polska"],
    "CZ": ["Tšekki", "Tsekki", "Czech Republic", "Czechia", "Česko"],
    "CH": ["Sveitsi", "Switzerland", "Schweiz", "Suisse"],
    "AT": ["Itävalta", "Austria", "Österreich"],
    "ES": ["Espanja", "Spain", "España"],
    "GR": ["Kreikka", "Greece", "Hellas"],
    "TR": ["Turkki", "Turkey", "Türkiye"],
    "IE": ["Irlanti", "Ireland", "Eire"],
    "JP": ["Japani", "Japan", "Nippon"],
    "TW": ["Taiwan", "Republic of China"],
    "HK": ["Hongkong", "Hong Kong"],
    "MO": ["Macao", "Macau"],
    "SA": ["Saudi-Arabia", "Saudi Arabia", "Saudi"],
    "MK": ["Macedonia", "North Macedonia", "Pohjois-Makedonia"],
    "BA": ["Bosnia", "Bosnia and Herzegovina"],
    "CI": ["Ivory Coast", "Cote d'Ivoire", "Norsunluurannikko"],
    "CD": ["DR Congo", "DRC", "Kongo-Kinshasa"],
    "CG": ["Kongo-Brazzaville", "Republic of the Congo"],
    "CV": ["Cape Verde", "Kap Verde"],
    "SZ": ["Swaziland", "Swazimaa"],
    "TL": ["East Timor", "Itä-Timor"],
    "MM": ["Burma"],
    "VA": ["Vatican", "Vatikaani"],
    "VN": ["Viet Nam"],
}

# Expected resolutions: (query, expected country, expected GeoNames name or None).
CHECKS = [("Ouagadougou", "BF", None), ("Lagos", "NG", None), ("Lima", "PE", None), ("Belo Horizonte", "BR", None),
          ("Cochabamba", "BO", None), ("Ulaanbaatar", "MN", None), ("Nairobi", "KE", None), ("Hobart", "AU", None),
          ("Christchurch", "NZ", None), ("Winnipeg", "CA", None), ("Kuopio", "FI", None),
          ("Tukholma", "SE", "Stockholm"), ("Lontoo", "GB", "London"), ("Pietari", "RU", "Saint Petersburg"),
          ("Kööpenhamina", "DK", "Copenhagen")]


def fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower().strip()


def fetch(url: str, cache: Path | None, binary: bool = False):
    fname = url.rsplit("/", 1)[-1]
    if cache:
        p = cache / fname
        if p.exists():
            return p.read_bytes() if binary else p.read_text(encoding="utf-8")
    r = httpx.get(url, timeout=120, follow_redirects=True, headers=UA)
    r.raise_for_status()
    if cache:
        (cache / fname).write_bytes(r.content)
    return r.content if binary else r.text


def fetch_zip(url: str, cache: Path | None) -> zipfile.ZipFile:
    """Large archives: streamed to the cache directory instead of held in memory."""
    if not cache:
        return zipfile.ZipFile(io.BytesIO(fetch(url, None, binary=True)))
    p = cache / url.rsplit("/", 1)[-1]
    if not p.exists():
        part = p.with_suffix(".part")
        with httpx.stream("GET", url, timeout=120, follow_redirects=True, headers=UA) as r:
            r.raise_for_status()
            with part.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        part.replace(p)
    return zipfile.ZipFile(p)


# ---------------------------------------------------------------- GeoNames
def load_countries(cache) -> dict[str, dict]:
    cols = ["iso", "iso3", "isonum", "fips", "name", "capital", "area", "population", "continent", "tld", "currency",
            "currency_name", "phone", "postal_fmt", "postal_re", "languages", "geonameid", "neighbours", "fips_eq"]
    out = {}
    for line in fetch(COUNTRY_URL, cache).splitlines():
        if not line or line.startswith("#"):
            continue
        rec = dict(zip(cols, line.split("\t")))
        out[rec["iso"]] = rec  # note: continent "NA" (North America) and ISO "NA" (Namibia) are plain strings here
    return out


def load_admin1(cache) -> dict[str, str]:
    out = {}
    for line in fetch(ADMIN1_URL, cache).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def load_cities(cache) -> list[dict]:
    with fetch_zip(CITIES_URL, cache) as z:
        text = z.read("cities15000.txt").decode("utf-8")
    cols = ["id", "name", "ascii", "alternatenames", "lat", "lon", "fclass", "fcode", "country", "cc2", "admin1",
            "admin2", "admin3", "admin4", "population", "elevation", "dem", "tz", "modified"]
    rows = []
    for line in text.splitlines():
        if line:
            rows.append(dict(zip(cols, line.split("\t"))))
    return rows


Alt = tuple[str, str, bool, bool, bool, bool]  # language, name, preferred, short, colloquial, historic


def load_alternates(cache, ids: set[str]) -> dict[str, list[Alt]] | None:
    """geonameid -> language-tagged alternate names (alternateNamesV2); None when the download fails."""
    try:
        z = fetch_zip(ALT_URL, cache)
    except httpx.HTTPError as e:
        print(f"alternateNamesV2 unavailable ({type(e).__name__}); aliases ranked shortest-first", file=sys.stderr)
        return None
    out: dict[str, list[Alt]] = {}
    with z, z.open("alternateNamesV2.txt") as f:
        for line in io.TextIOWrapper(f, encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            if len(p) < 8 or p[1] not in ids or not LANG_RE.fullmatch(p[2]):  # drops post/iata/icao/link/wkdt/abbr
                continue
            out.setdefault(p[1], []).append((p[2].split("-")[0], p[3], p[4] == "1", p[5] == "1", p[6] == "1",
                                             p[7] == "1"))
    return out


# ---------------------------------------------------------------- Wikidata
Labels = dict[str, dict[str, int]]  # geonameid -> {Finnish label: largest population of an item carrying it}


def _sparql(query: str) -> list[dict]:
    r = httpx.post(SPARQL_URL, data={"query": query}, timeout=120,
                   headers={**UA, "Accept": "application/sparql-results+json"})
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def _collect(bindings, labels: Labels, min_pop: int):
    for b in bindings:
        gn, fi = b["gn"]["value"], b["fi"]["value"]
        try:
            pop = int(float(b["pop"]["value"])) if "pop" in b else 0
        except ValueError:
            pop = 0
        if pop > min_pop and gn.isdigit():
            got = labels.setdefault(gn, {})
            got[fi] = max(pop, got.get(fi, 0))


def _batched(ids: list[str], min_pop: int, labels: Labels, size: int = 300):
    q = """SELECT ?gn ?fi ?pop WHERE {{ VALUES ?gn {{ {vals} }}
  ?item wdt:P1566 ?gn ; rdfs:label ?fi . FILTER(LANG(?fi) = "fi")
  OPTIONAL {{ ?item wdt:P1082 ?pop }} }}"""
    todo = [ids[i:i + size] for i in range(0, len(ids), size)]
    while todo:
        chunk = todo.pop(0)
        try:
            _collect(_sparql(q.format(vals=" ".join(f'"{g}"' for g in chunk))), labels, min_pop)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            if len(chunk) <= 25:
                raise
            print(f"  sparql batch of {len(chunk)} failed ({type(e).__name__}); splitting", file=sys.stderr)
            half = len(chunk) // 2
            todo[:0] = [chunk[:half], chunk[half:]]


def finnish_labels(cities: list[dict], cache: Path | None) -> tuple[Labels, dict[str, str]] | None:
    """(geonameid -> raw Finnish labels, Swedish municipality name -> Finnish name); None if Wikidata is down."""
    store = cache / "wikidata_fi.json" if cache else None
    if store and store.exists():
        got = json.loads(store.read_text(encoding="utf-8"))
        if got.get("near") == sorted(NEAR) and got.get("min_pop") == FI_MIN_POP:
            return got["labels"], got["municipalities"]
    labels: Labels = {}
    full = """SELECT ?gn ?fi ?pop WHERE {
  hint:Query hint:optimizer "None" .
  ?item wdt:P1082 ?pop . FILTER(?pop > %d)
  ?item wdt:P1566 ?gn .
  ?item rdfs:label ?fi . FILTER(LANG(?fi) = "fi") }""" % FI_MIN_POP
    # Municipalities of Finland (Q856076): GeoNames names some Finnish towns in Swedish (Jakobstad, Hyvinge, Sibbo).
    muni_q = """SELECT ?fi ?sv WHERE { ?m wdt:P31 wd:Q856076 ; rdfs:label ?fi, ?sv .
  FILTER(LANG(?fi) = "fi") FILTER(LANG(?sv) = "sv") }"""
    try:
        try:
            _collect(_sparql(full), labels, FI_MIN_POP)
            print("wikidata: one-shot query ok,", len(labels), "geonameids")
        except (httpx.HTTPError, ValueError, KeyError) as e:
            print(f"wikidata: one-shot query failed ({type(e).__name__}); batching by geonameid", file=sys.stderr)
            labels.clear()
            _batched([c["id"] for c in cities], FI_MIN_POP, labels)
        _batched([c["id"] for c in cities if c["country"] in NEAR], -1, labels)
        munis = {key(b["sv"]["value"]): b["fi"]["value"] for b in _sparql(muni_q)
                 if key(b["sv"]["value"]) != key(b["fi"]["value"])}
        print("wikidata: near-abroad + municipality queries ok;", len(labels), "geonameids,", len(munis),
              "Swedish-named municipalities")
    except (httpx.HTTPError, ValueError, KeyError) as e:
        print(f"wikidata unreachable ({type(e).__name__}: {e}); keeping name_fi from the previous output",
              file=sys.stderr)
        return None
    if store:
        store.write_text(json.dumps({"near": sorted(NEAR), "min_pop": FI_MIN_POP, "labels": labels,
                                     "municipalities": munis}, ensure_ascii=False), encoding="utf-8")
    return labels, munis


def previous_fi() -> Labels:
    if not OUT.exists():
        return {}
    old = json.loads(OUT.read_text(encoding="utf-8"))
    i, j = old["columns"].index("id"), old["columns"].index("name_fi")
    return {str(r[i]): {r[j]: 0} for r in old["rows"] if r[j]}


# Wikidata often hangs the GeoNames ID of a town on its urban-area, parish-centre, district or station item
# ("Tampereen keskustaajama", "Liedon kirkonkylä", "Sokolin metroasema"): those labels are not place names.
NOT_A_NAME = {"keskustaajama", "taajama", "keskusta", "keskus", "kirkonkylä", "kunnanvirasto", "kunnallispiiri",
              "kunta", "piirikunta", "kaupunkipiiri", "maakunta", "lääni", "piiri", "alue", "hallintoalue",
              "kaupunginosa", "arrondissementti", "prefektuuri", "provinssi", "osavaltio", "asema", "metroasema",
              "rautatieasema", "lentoasema",
              "lungsod"}  # Tagalog "city of", mis-tagged as Finnish in GeoNames ("Lungsod ng Caloocan")


GENERIC = re.compile(r"[\s-]+(city|town|shi|si|gun|municipality)$", re.I)  # "Xinle City", "Tottori-shi"


def choose_fi(c: dict, cands: dict[str, int], munis: dict[str, str], tagged: list[Alt] | None) -> str | None:
    """Finnish name for a GeoNames place, or None when Finnish uses the GeoNames name.

    Wikidata sometimes hangs a town's GeoNames ID on a neighbouring item (the county-level city, the historical
    city, the parish centre). A label is therefore kept only when GeoNames also lists it for the place (not only
    as a historic name) or it is a close transliteration of the GeoNames name (Tšeljabinsk, Gulkevitši); for
    Finnish places GeoNames must list it. Without a Wikidata label, GeoNames' own Finnish name is used."""
    own = {key(c["name"]), key(c["ascii"])}
    known: dict[str, bool] = {}  # key -> GeoNames lists it only as a historic name
    if tagged is None:
        known = {key(a): False for a in c["alternatenames"].split(",")}
    for _lang, a, _pref, _short, _colloq, hist in tagged or []:
        k = key(a)
        known[k] = known.get(k, True) and hist
    best = None
    for label, pop in cands.items():
        label = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip()  # "Molde (taajama)", "Kupino (Novosibirskin alue)"
        head = label.split(", ")[0]
        # "Owo, Nigeria", "Xinle City": Finnish uses the GeoNames name. ("New York" for "New York City" is kept.)
        if key(GENERIC.sub("", label)) in own or key(head) in own:
            return None
        k = key(label)
        if not label or re.fullmatch(r"Q\d+", label) or NOT_A_NAME & set(re.findall(r"[^\W\d_]+", label.lower())):
            continue
        if known.get(k) is True:  # "Christiania" for Oslo
            continue
        listed = k in known
        if not listed and (c["country"] in HOME or max(SequenceMatcher(None, k, o).ratio() for o in own) < 0.55):
            continue
        if best is None or pop > best[0]:
            best = (pop, label)
    if best:
        return best[1]
    # No Wikidata label for this geonameid (Lübeck's item carries another GeoNames ID): GeoNames' own Finnish name.
    gn_fi = sorted((not pref, len(a), a) for lang, a, pref, _short, colloq, hist in tagged or []
                   if lang == "fi" and not hist and not colloq and key(a) not in own
                   and not NOT_A_NAME & set(re.findall(r"[^\W\d_]+", a.lower())))
    if gn_fi:
        return gn_fi[0][2]
    if c["country"] in HOME and key(c["name"]) in munis:  # Jakobstad -> Pietarsaari, Sibbo -> Sipoo
        return munis[key(c["name"])]
    return None


# ---------------------------------------------------------------- aliases
_LETTERS = str.maketrans({"ł": "l", "Ł": "l", "ø": "o", "Ø": "o", "đ": "d", "Đ": "d", "ß": "ss", "æ": "ae",
                          "Æ": "ae", "œ": "oe", "Œ": "oe", "ı": "i", "þ": "th", "Þ": "th", "ð": "d", "Ð": "d"})


def key(s: str) -> str:
    """Dedupe key: case, accents, spaces, hyphens and apostrophes ignored ("Ulan-Bator" == "Ułan Bator")."""
    return re.sub(r"[\W_]+", "", fold((s or "").translate(_LETTERS)))


def latin_only(s: str) -> bool:
    for c in s:
        if c.isalpha():
            if "LATIN" not in unicodedata.name(c, ""):
                return False
        elif c.isdigit() or c not in " -'’.()":
            return False
    return True


def code_like(s: str) -> bool:
    letters = s.replace(".", "").replace(" ", "")
    return len(s) <= 2 or (len(letters) <= 4 and letters.isupper())  # IATA / ICAO / "N.Y."; keeps "Åbo", "Ulm"


def usable(a: str, prefix: str) -> bool:
    # GeoNames' convenience column adds lowercase machine transliterations of non-Latin names ("lndn", "ywrk"):
    # real names are capitalised. "Kuopio linn" / "Nokia stad" add nothing a prefix search on the name misses.
    return bool(a) and len(a) <= 40 and a[0].isupper() and not code_like(a) and latin_only(a) \
        and not fold(a).startswith(prefix)


def pick_aliases(c: dict, taken: set[str], tagged: list[Alt] | None, local: list[str]) -> list[str]:
    """Latin-script alternate names, best first. With alternateNamesV2: tier (en/fi/sv/de and the country's primary
    language > other European and local languages > untagged > rest), preferred, not historic, not colloquial,
    used by more languages, shorter. Without it: shortest first."""
    prefix = fold(c["name"]) + " "
    rank: dict[str, tuple] = {}
    if tagged is not None:
        uses: dict[str, int] = {}
        for lang, a, pref, _short, colloq, hist in tagged:
            a = a.strip()
            if not usable(a, prefix):
                continue
            tier = (0 if lang in TIER1 or (local and lang == local[0]) else 1 if lang in TIER2 or lang in local
                    else 2 if not lang else 3)
            r = (tier, not pref, hist, colloq)
            uses[a] = uses.get(a, 0) + 1
            rank[a] = min(rank.get(a, r), r)
        rank = {a: (*r, -uses[a], len(a), a.isascii()) for a, r in rank.items()}
    else:
        for a in c["alternatenames"].split(","):
            a = a.strip()
            if usable(a, prefix):
                rank[a] = (len(a), a.isascii())  # shortest first; the accented spelling wins a tie
    seen = set(taken)
    out = []
    for a in sorted(rank, key=lambda a: (rank[a], a)):
        k = key(a)
        if k not in seen:
            seen.add(k)
            out.append(a)
    return out


# ---------------------------------------------------------------- build
def build_rows(cities, admin1, countries, alternates, fi_labels: Labels, munis: dict[str, str]) -> list[list]:
    local = {iso: [lang.split("-")[0] for lang in rec["languages"].split(",") if lang]
             for iso, rec in countries.items()}
    rows = []
    for c in cities:
        tagged = None if alternates is None else alternates.get(c["id"], [])
        name_fi = choose_fi(c, fi_labels.get(c["id"], {}), munis, tagged)
        taken = {key(c["name"]), key(c["ascii"])} | ({key(name_fi)} if name_fi else set())
        rows.append([
            int(c["id"]), c["name"], c["ascii"], name_fi, c["country"],
            admin1.get(f"{c['country']}.{c['admin1']}") or None,
            round(float(c["lat"]), 5), round(float(c["lon"]), 5), int(c["population"] or 0),
            pick_aliases(c, taken, tagged, local.get(c["country"], [])), c["tz"] or None,
        ])
    rows.sort(key=lambda r: (r[4], -r[8], r[0]))
    return rows


def fit_size(rows: list[list], header: dict) -> tuple[str, dict[int, int]]:
    """Alias caps by population floor. Places >= 1M keep MAX_ALIASES; when the file is over SIZE_TARGET the
    < 100k tier is trimmed first, then the 100k-1M tier, one alias at a time."""
    caps = {1_000_000: MAX_ALIASES, 100_000: MAX_ALIASES, 0: MAX_ALIASES}
    full = [list(r[9]) for r in rows]
    while True:
        for r, al in zip(rows, full):
            r[9] = al[:next(cap for floor, cap in caps.items() if r[8] >= floor)]
        text = json.dumps({**header, "rows": rows}, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) <= SIZE_TARGET or caps[100_000] == 0:
            return text, caps
        floor = 0 if caps[0] > max(0, caps[100_000] - 6) else 100_000
        caps[floor] -= 1


def country_doc(countries: dict[str, dict]) -> dict:
    from babel import Locale

    names = {loc: Locale(loc).territories for loc in CLDR_LOCALES}
    doc = {}
    for iso, rec in sorted(countries.items()):
        en = rec["name"]
        fi = FI_DISPLAY.get(iso) or names["fi"].get(iso) or en
        forms = [en, fi] + [names[loc].get(iso) for loc in CLDR_LOCALES] + COUNTRY_ALIASES.get(iso, [])
        aliases: list[str] = []
        for f in forms:
            if not f:
                continue
            # "Hongkong – Kiinan erityishallintoalue" -> + "Hongkong"; "Myanmar (Burma)" -> + "Myanmar", "Burma"
            parts = [f, re.split(r"\s+[–-]\s+|\s*\(", f)[0]] + re.findall(r"\(([^)]+)\)", f)
            for p in parts:
                p = p.strip().replace("’", "'")
                for v in (p.lower(), p.lower().replace(" & ", " and "), fold(p).replace(" & ", " and ")):
                    if v and v not in aliases:
                        aliases.append(v)
        doc[iso] = {"en": en, "fi": fi, "continent": rec["continent"], "aliases": aliases}
    owners: dict[str, set[str]] = {}
    for iso, d in doc.items():
        for a in d["aliases"]:
            owners.setdefault(a, set()).add(iso)
    shared = {a for a, s in owners.items() if len(s) > 1}  # "congo" (CD and CG) names neither
    for d in doc.values():
        d["aliases"] = [a for a in d["aliases"] if a not in shared]
    if shared:
        print("country aliases dropped as ambiguous:", sorted(shared))
    return doc


# ---------------------------------------------------------------- checks
def run_checks(data: dict, countries: dict, path: Path = OUT):
    cols = data["columns"]
    ix = {c: cols.index(c) for c in cols}
    index: dict[str, list[tuple[int, int, list]]] = {}
    for r in data["rows"]:
        for kind, keys in ((0, [r[ix["name"]], r[ix["ascii"]], r[ix["name_fi"]]]), (1, r[ix["aliases"]])):
            for k in keys:
                if k:
                    index.setdefault(fold(k), []).append((kind, -r[ix["population"]], r))
    print(f"rows: {len(data['rows'])}; with name_fi: {sum(1 for r in data['rows'] if r[ix['name_fi']])}; "
          f"countries with places: {len({r[ix['country']] for r in data['rows']})}")
    print(f"sizes: {path.name} {path.stat().st_size:,} B; {OUT_COUNTRIES.name} {OUT_COUNTRIES.stat().st_size:,} B")
    ok_all = True
    for q, cc, want in CHECKS:
        hits = sorted(index.get(fold(q), []), key=lambda t: (t[0], t[1]))
        if not hits:
            print(f"FAIL {q}: no match")
            ok_all = False
            continue
        kind, _, r = hits[0]
        ok = r[ix["country"]] == cc and (want is None or r[ix["name"]] == want)
        ok_all &= ok
        how = "name" if kind == 0 else "alias"
        print(f"{'PASS' if ok else 'FAIL'} {q} -> {r[ix['name']]}, {r[ix['admin1']]}, {r[ix['country']]} "
              f"(id {r[ix['id']]}, by {how}, name_fi={r[ix['name_fi']]})")
    clash: dict[str, set] = {}
    for iso, c in countries.items():
        for a in c["aliases"]:
            clash.setdefault(a, set()).add(iso)
    bad = {a: sorted(s) for a, s in clash.items() if len(s) > 1}
    print(f"country aliases: {sum(len(c['aliases']) for c in countries.values())} for {len(countries)} codes; "
          f"ambiguous: {bad or 'none'}")
    for q, cc in [("USA", "US"), ("Yhdysvallat", "US"), ("UK", "GB"), ("Iso-Britannia", "GB"), ("Englanti", "GB"),
                  ("Holland", "NL"), ("Etelä-Korea", "KR"), ("Arabiemiirikunnat", "AE"), ("UAE", "AE"),
                  ("Kiina", "CN"), ("Saksa", "DE"), ("Ruotsi", "SE"), ("Venäjä", "RU"), ("Suomi", "FI")]:
        got = sorted(clash.get(q.lower(), set()))
        ok = got == [cc]
        ok_all &= ok
        print(f"{'PASS' if ok else 'FAIL'} country {q} -> {got}")
    return ok_all


def main():
    if "--check" in sys.argv:
        data = json.loads(OUT.read_text(encoding="utf-8"))
        countries = json.loads(OUT_COUNTRIES.read_text(encoding="utf-8"))["countries"]
        sys.exit(0 if run_checks(data, countries) else 1)
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    countries = load_countries(cache)
    admin1 = load_admin1(cache)
    cities = load_cities(cache)
    wd = finnish_labels(cities, cache)
    fi_labels, munis = wd if wd is not None else (previous_fi(), {})
    alternates = load_alternates(cache, {c["id"] for c in cities})
    rows = build_rows(cities, admin1, countries, alternates, fi_labels, munis)
    now = datetime.now(timezone.utc)
    header = {"version": f"{now:%Y%m%d}-{len(rows)}", "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "sources": [CITIES_URL + " (GeoNames, CC BY 4.0)", ADMIN1_URL + " (GeoNames, CC BY 4.0)",
                          COUNTRY_URL + " (GeoNames, CC BY 4.0)",
                          ALT_URL + " (GeoNames, CC BY 4.0: alias ranking)",
                          SPARQL_URL + " (Wikidata, CC0: Finnish labels via P1566 GeoNames ID; Finnish municipalities)"],
              "columns": COLUMNS}
    text, caps = fit_size(rows, header)
    OUT.write_text(text, encoding="utf-8")
    cdoc = country_doc(countries)
    OUT_COUNTRIES.write_text(json.dumps({"generated_at": header["generated_at"],
                                         "sources": [COUNTRY_URL + " (GeoNames, CC BY 4.0)",
                                                     "Unicode CLDR via babel (Unicode License)",
                                                     "data/places_build.py COUNTRY_ALIASES"],
                                         "countries": cdoc}, ensure_ascii=False, separators=(",", ":")),
                             encoding="utf-8")
    missing = sorted({r[4] for r in rows} - set(cdoc))
    print("wrote", OUT, len(rows), "places; alias caps by population floor:", caps)
    print("wrote", OUT_COUNTRIES, len(cdoc), "countries; place countries missing from countryInfo:", missing or "none")
    sys.exit(0 if run_checks(json.loads(text), cdoc) else 1)


if __name__ == "__main__":
    main()
