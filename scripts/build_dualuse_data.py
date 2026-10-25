#!/usr/bin/env python3
"""Build backend/data/dualuse/* from official sources (re-run whenever Annex I or the correlation table changes).

Sources (all official, no key, no login):
  1. Regulation (EU) 2021/821, latest consolidated version in force on --on (default today), English XHTML, fetched
     from the EU Publications Office Cellar (the same document EUR-Lex shows; eur-lex.europa.eu itself sits behind an
     AWS WAF browser challenge). The consolidated CELEX is found with the Publications Office SPARQL endpoint.
     Parsed: Annex I (list of dual-use items), Annex II (Union general export authorisations EU001-EU008 + Section I),
     Annex IV (items for which intra-Union transfers need an authorisation).
  2. The Commission's CN <-> dual-use correlation table (DG TAXUD, CIRCABC "Dual use codes" library, newest file
     for the --on year): CN 10-digit code -> TARIC footnote DUxxx -> Annex I code.
  3. EU Sanctions Map (www.sanctionsmap.eu, operated for the Council/Commission) regimes API: per country, which EU
     regimes carry an arms embargo / dual-use export ban (Art. 4(1)(b) catch-all and EU008 Part 2(c)).

Usage:
  /home/user/work/venv/bin/python scripts/build_dualuse_data.py [--on 2026-10-24] [--out backend/data/dualuse]
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import sys
import unicodedata

import httpx
from lxml import etree

SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
CELLAR = "http://publications.europa.eu/resource/celex/{celex}"
EURLEX = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"
CIRCABC_FOLDER = "c3d06bd7-6ef0-4771-bbd7-f92b976ae9a0"   # DG TAXUD library "Dual use codes"
CIRCABC_LIST = ("https://circabc.europa.eu/service/circabc/spaces/{id}/children?language=en&guest=true&page=1"
                "&limit=100&order=modified_DESC&folderOnly=false&fileOnly=false")
CIRCABC_DOWNLOAD = "https://circabc.europa.eu/rest/download/{id}"
SANCTIONS_MAP = "https://www.sanctionsmap.eu/api/v1/regime"
UA = {"User-Agent": "Huolitsija customs controls data build (+https://huolitsija.fi)"}

ENTRY_RE = re.compile(r"^(\d)([A-E])(\d{3})$")
ENTRY_AT_START = re.compile(r"^(\d[A-E]\d{3})\b\s*(.*)$", re.S)
CODE_IN_TEXT = re.compile(r"\b(\d[A-E]\d{3}(?:\.[a-z0-9]{1,4})*)\.?")
MARKERS = re.compile(r"[▼►]\s?[MCBA]\d{0,3}\b|◄")


# ---------------------------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------------------------
def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sparql(client: httpx.Client, query: str) -> list[dict]:
    r = client.get(SPARQL, params={"query": query}, headers={"Accept": "application/sparql-results+json"})
    r.raise_for_status()
    return [{k: v["value"] for k, v in b.items()} for b in r.json()["results"]["bindings"]]


def latest_consolidated(client: httpx.Client, base: str, on: str) -> str:
    """Newest consolidated CELEX (0YYYYRNNNN-YYYYMMDD) whose date is on or before `on`."""
    rows = sparql(client, f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?celex WHERE {{ ?w cdm:resource_legal_id_celex ?celex .
FILTER(STRSTARTS(STR(?celex), "{base}-")) }}""")
    day = on.replace("-", "")
    cands = sorted(r["celex"] for r in rows if re.fullmatch(rf"{base}-\d{{8}}", r["celex"]) and r["celex"][-8:] <= day)
    if not cands:
        raise SystemExit(f"no consolidated version of {base} on or before {on}")
    return cands[-1]


def fetch_celex(client: httpx.Client, celex: str) -> tuple[bytes, str]:
    r = client.get(CELLAR.format(celex=celex), headers={"Accept": "application/xhtml+xml", "Accept-Language": "eng"})
    r.raise_for_status()
    if b"<html" not in r.content[:3000]:
        raise SystemExit(f"{celex}: Cellar did not return XHTML")
    return r.content, str(r.url)


# ---------------------------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------------------------
def clean(s: str) -> str:
    s = MARKERS.sub(" ", s or "")
    s = s.replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def text(el) -> str:
    return clean("".join(el.itertext()))


def cls(el) -> str:
    return el.get("class") or ""


def italic_share(el) -> float:
    total = len(re.sub(r"\s+", "", "".join(el.itertext())))
    if not total:
        return 0.0
    it = 0
    for sp in el.iter("span"):
        if "italics" in cls(sp).split() and not any("italics" in cls(a).split() for a in sp.iterancestors("span")):
            it += len(re.sub(r"\s+", "", "".join(sp.itertext())))
    return it / total


NOTE_RE = re.compile(r"^(?P<kind>Technical\s+Notes?|Notes?|N\.\s?B\.)(?P<head>[^:]{0,70}?):?\s*(?P<body>.*)$", re.S)


def note_kind(t: str) -> str | None:
    m = NOTE_RE.match(t)
    if not m:
        return None
    k = m.group("kind").lower().replace(" ", "")
    if k.startswith("technical"):
        return "technical_notes"
    if k.startswith("n."):
        return "nb"
    # "Note", "Notes", "Note 1:", "Note to the Cryptography Note:" -- but not ordinary sentences that start "Note"
    if ":" not in t[:90] and not re.match(r"^Notes?\s+\d", t):
        return None
    return "notes"


def normalize_code(code: str) -> str:
    """'5A002.a.1.' / '5A002a1' / '1C350 35' / '5a002.A.1' -> '5A002.a.1' (dots, no trailing dot)."""
    c = re.sub(r"\s+", " ", (code or "").strip())
    m = re.match(r"^(\d)\s*([A-Ea-e])\s*(\d{3})(.*)$", c)
    if not m:
        return c
    head = f"{m.group(1)}{m.group(2).upper()}{m.group(3)}"
    rest = m.group(4)
    parts = re.findall(r"[A-Za-z]+|\d+", rest)
    return ".".join([head] + [p.lower() if p.isalpha() else p for p in parts])


# ---------------------------------------------------------------------------------------------
# Annex I
# ---------------------------------------------------------------------------------------------
REGIMES = {0: "WA", 1: "MTCR", 2: "NSG", 3: "AG", 4: "CWC"}
REGIME_NAMES = {"WA": "Wassenaar Arrangement", "MTCR": "Missile Technology Control Regime",
                "NSG": "Nuclear Suppliers Group", "AG": "Australia Group", "CWC": "Chemical Weapons Convention",
                "EU-WA+": "Member States' additional commitments accepted also as members of the Wassenaar "
                          "Arrangement (Delegated Regulation (EU) 2025/2003, recital 3)"}


def regime_for(code: str) -> list[str]:
    cat, num = int(code[0]), int(code[2:5])
    if cat == 0:
        return ["NSG"]
    hundred = num // 100
    if hundred == 5:
        return ["EU-WA+"]
    return [REGIMES.get(hundred, "WA")]


class AnnexI:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.order: list[str] = []
        self.group_notes: dict[str, list[dict]] = {}
        self.category = None
        self.category_title = None
        self.group = None
        self.group_title = None
        self.entry = None
        self.started = False
        self.warnings: list[str] = []

    # node creation -------------------------------------------------------------------------
    def _add(self, code: str, *, parent: str | None, label: str | None, txt: str) -> dict:
        if code in self.nodes:
            self.warnings.append(f"duplicate code {code}")
        node = {"code": code, "code_official": code + ("." if parent else ""), "entry": code[:5],
                "level": "entry" if parent is None else "item", "label": label, "parent": parent,
                "category": code[0], "category_title": self.category_title, "group": code[1],
                "group_title": self.group_title, "text": txt, "notes": [], "technical_notes": [], "nb": [],
                "children": []}
        if parent is None:
            node["regimes"] = regime_for(code)
        self.nodes[code] = node
        self.order.append(code)
        if parent:
            self.nodes[parent]["children"].append(code)
        return node

    def new_entry(self, code: str, txt: str) -> dict:
        if code[0] != (self.category or code[0]):
            self.warnings.append(f"{code} found under category {self.category}")
        self.entry = self._add(code, parent=None, label=None, txt=txt)
        return self.entry

    def add_note(self, target: dict | None, kind: str, t: str):
        rec = {"text": t}
        if target is None:
            key = f"{self.category}{self.group or ''}"
            self.group_notes.setdefault(key, []).append({"kind": kind, **rec})
            return
        target[kind].append(rec)
        target["_last_note"] = (kind, len(target[kind]) - 1)

    def extend_last_note(self, target: dict | None, t: str) -> bool:
        if target is None:
            key = f"{self.category}{self.group or ''}"
            if self.group_notes.get(key):
                self.group_notes[key][-1]["text"] += " " + t
                return True
            return False
        ln = target.get("_last_note")
        if not ln:
            return False
        target[ln[0]][ln[1]]["text"] += " " + t
        return True

    # walking ---------------------------------------------------------------------------------
    def heading(self, t: str):
        m = re.match(r"^CATEGORY\s+(\d)\s*[-–—]\s*(.*)$", t, re.I)
        if m:
            self.started = True
            self.category, self.category_title = m.group(1), m.group(2).strip()
            self.group = self.group_title = None
            self.entry = None
            return
        m = re.match(r"^(\d)([A-E])(\d?)\s+(.*)$", t)
        if m and self.started:
            self.group, self.group_title = m.group(2), m.group(4).strip()
            self.entry = None

    def walk(self, container, target: dict | None, top: bool):
        for el in container:
            if not isinstance(el.tag, str):
                continue
            c = cls(el)
            if el.tag == "p" and c.startswith("title-annex"):
                continue
            if el.tag == "p" and c in ("modref", "arrow"):
                continue
            t = text(el)
            if not t:
                continue
            if el.tag == "p" and c.startswith("title-gr-seq"):
                nk = note_kind(t)
                if nk and self.started:
                    self.add_note(target if not top else self.entry, nk, t)
                elif top:
                    self.heading(t)
                elif target is not None:
                    target["text"] += " " + t
                continue
            if not self.started:
                continue
            if el.tag == "div" and "grid-container" in c:
                self.grid(el, target, top)
                continue
            if el.tag == "table" or (el.tag == "div" and el.find(".//table") is not None and not el.findall("p")):
                tgt = self.entry if top else target
                if tgt is not None:
                    if italic_share(el) > 0.8 and self.extend_last_note(tgt, t):
                        continue
                    tgt.setdefault("tables", []).append(t)
                continue
            # paragraph or a margin div holding paragraphs
            tgt = self.entry if top else target
            m = ENTRY_AT_START.match(t)
            if top and m and ENTRY_RE.match(m.group(1)) and italic_share(el) < 0.5:
                self.new_entry(m.group(1), m.group(2).strip())
                continue
            nk = note_kind(t)
            if nk and (italic_share(el) > 0.5 or t.startswith("N.B")):
                self.add_note(tgt, nk, t)
                continue
            if italic_share(el) > 0.8 and self.extend_last_note(tgt, t):
                continue
            if tgt is not None:
                tgt["text"] = (tgt["text"] + " " + t).strip()
            else:
                key = f"{self.category}{self.group or ''}"
                self.group_notes.setdefault(key, []).append({"kind": "text", "text": t})

    def grid(self, el, target: dict | None, top: bool):
        col1 = el.find("./div[@class='list grid-list-column-1']")
        col2 = el.find("./div[@class='grid-list-column-2']")
        if col1 is None or col2 is None:
            return
        label = text(col1)
        lab = label.strip().rstrip(".").strip()
        if top and ENTRY_RE.match(lab):
            first = next((p for p in col2 if isinstance(p.tag, str) and text(p)), None)
            ent = self.new_entry(lab, text(first) if first is not None else "")
            rest = [x for x in col2 if x is not first]
            holder = etree.Element("div")
            for x in rest:
                holder.append(x)
            self.walk(holder, ent, top=False)
            return
        owner = self.entry if top else target
        if owner is None:
            return
        first_p = next((p for p in col2.iter("p") if text(p)), None)
        italic = first_p is not None and italic_share(first_p) > 0.8
        if italic and (owner.get("_last_note") or (top and self.entry is None)):
            # a lettered list inside a Note ("Note: 5A002 does not control: a. ...; b. ...")
            self.extend_last_note(owner, f"{label} {text(col2)}")
            return
        if not re.fullmatch(r"[a-z]{1,4}|\d{1,3}", lab):
            # an unlabelled or bullet list: part of the owner's text
            owner["text"] = (owner["text"] + f" {label} {text(col2)}").strip()
            return
        code = f"{owner['code']}.{lab}"
        if code in self.nodes:
            # a second "a." under the same parent is not a sub-item but part of a table note (e.g. 2E503's
            # "Technical Note to Table: Deposition Techniques"): keep its words with the parent
            owner.setdefault("tables", []).append(f"{label} {text(col2)}")
            return
        first = next((p for p in col2 if isinstance(p.tag, str) and text(p)), None)
        node = self._add(code, parent=owner["code"], label=label, txt=text(first) if first is not None else "")
        holder = etree.Element("div")
        for x in [x for x in col2 if x is not first]:
            holder.append(x)
        self.walk(holder, node, top=False)

    def result(self) -> dict:
        for n in self.nodes.values():
            n.pop("_last_note", None)
            n["text"] = clean(n["text"])
        entries = [self.nodes[c] for c in self.order]
        return {"entries": entries, "group_notes": self.group_notes, "warnings": self.warnings}


def parse_annex_i(root) -> dict:
    anx = root.find(".//div[@id='anx_I']")
    if anx is None:
        raise SystemExit("Annex I (div#anx_I) not found")
    a = AnnexI()
    a.walk(anx, None, top=True)
    out = a.result()
    n_entries = sum(1 for e in out["entries"] if e["level"] == "entry")
    if n_entries < 300:
        raise SystemExit(f"Annex I parse produced only {n_entries} entries; format changed?")
    return out


# ---------------------------------------------------------------------------------------------
# Countries
# ---------------------------------------------------------------------------------------------
FRENCH_OCT = ["NC", "PF", "TF", "WF", "PM", "BL"]  # Annex II TFEU overseas countries and territories (French)


def iso_for(name: str) -> list[str]:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
    from services.screening import country_iso2  # noqa: E402  (pure function, no DB access)

    n = clean(name).strip(" ,;.—-")
    n = re.sub(r"^(and|including)\s+", "", n)
    if not n:
        return []
    if n.lower().startswith("french overseas territories"):
        return list(FRENCH_OCT)
    extra = []
    if "including Hong Kong and Macao" in n:
        extra = ["HK", "MO"]
        n = clean(n.replace("(including Hong Kong and Macao)", ""))
    special = {"Myanmar/Burma": "MM", "South Korea": "KR", "North Korea": "KP", "Turkey": "TR", "Congo": "CG",
               "Democratic Republic of the Congo": "CD"}
    iso = special.get(n) or country_iso2(n)
    if not iso:
        raise SystemExit(f"unmapped country name in Annex II: {name!r}")
    return [iso] + extra


def split_countries(t: str) -> list[str]:
    t = re.sub(r"\(\s*\d+\s*\)", " ", t)                                   # footnote markers "( 11 )"
    t = re.sub(r"\(without prejudice.*Protocol\)", " ", t, flags=re.S)      # the Northern Ireland proviso
    out: list[str] = []
    for p in re.split(r"\s*—\s*|,\s+(?![^()]*\))|;\s*", t):
        p = p.strip(" .,;:")
        if not p or set(p) <= {"—", "-"}:
            continue
        out += iso_for(p)
    seen, res = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            res.append(c)
    return res


# ---------------------------------------------------------------------------------------------
# Annex II (EUGEA) and Annex IV
# ---------------------------------------------------------------------------------------------
def parse_annex_ii(root) -> dict:
    anx = root.find(".//div[@id='anx_II']")
    seg = text(anx)
    sec_i = seg.find("I. LIST REFERRED TO IN POINT (A) OF ARTICLE 12(6)")
    if sec_i < 0:
        raise SystemExit("Annex II Section I not found")
    section_i_text = seg[sec_i:]
    head = seg[:sec_i]
    parts = re.split(r"UNION GENERAL EXPORT AUTHORISATION No (EU00\d)", head)
    out = {}
    for k in range(1, len(parts), 2):
        code, body = parts[k], parts[k + 1]
        title = clean(re.sub(r"^\s*\(referred to in point \(d\) of Article 12\(1\) of this Regulation\)", "", body)
                      .split("Issuing authority")[0])
        m1 = re.search(r"Part 1\s*[—–-]\s*Items(.*?)(?=Part 2\s*[—–-]\s*Destinations|Part 3\s*[—–-])", body, re.S)
        m2 = re.search(r"Part 2\s*[—–-]\s*Destinations(.*?)(?=Part 3\s*[—–-]|$)", body, re.S)
        m3 = re.search(r"Part 3\s*[—–-]\s*Conditions and requirements for use(.*)$", body, re.S)
        items_text = clean(m1.group(1)) if m1 else ""
        dest_text = clean(m2.group(1)) if m2 else ""
        cond_text = clean(m3.group(1)) if m3 else ""
        codes = [normalize_code(c) for c in CODE_IN_TEXT.findall(items_text)]
        all_items = bool(re.search(r"covers all dual-use items specified in any entry in Annex I", items_text))
        dest: dict = {"text": dest_text}
        mx = re.search(r"to all destinations, excluding:(.*?)(?:\(c\)|$)", dest_text, re.S)
        if mx:
            listed = re.search(r"\(b\)(.*)$", mx.group(1), re.S)
            dest.update(mode="all_except", excluded=split_countries(listed.group(1)) if listed else [],
                        excludes_eu001=bool(re.search(r"\(a\) destinations eligible for export under Union general "
                                                      r"export authorisation No EU001", mx.group(1))),
                        excludes_embargoed=bool(re.search(r"subject to an arms embargo", dest_text)))
        else:
            ml = re.search(r"to the following destinations:(.*)$", dest_text, re.S)
            dest.update(mode="listed", countries=split_countries(ml.group(1)) if ml else [])
        out[code] = {"code": code, "title": title, "items_text": items_text, "items_mode":
                     "all_except_section_i" if all_items else ("listed" if codes else "described"),
                     "items": codes, "destinations": dest, "conditions_text": cond_text,
                     "declare_in_customs_declaration": bool(re.search(rf"exported under Union general export "
                                                                       rf"authorisation No {code} in the customs "
                                                                       rf"declaration", body))}
    if sorted(out) != [f"EU00{i}" for i in range(1, 9)]:
        raise SystemExit(f"Annex II: expected EU001-EU008, got {sorted(out)}")
    sec_codes = []
    for m in re.finditer(r"—\s*(\d[A-E]\d{3}(?:\.[a-z0-9]{1,4})*\.?)\s", section_i_text):
        sec_codes.append(normalize_code(m.group(1)))
    return {"eugea": out, "section_i": {"text": section_i_text, "codes": sec_codes,
                                        "includes_annex_iv": "all items specified in Annex IV" in section_i_text}}


def parse_annex_iv(root) -> dict:
    """Annex IV: PART I / PART II, section headings, then tables (code | description) or code-labelled lists."""
    anx = root.find(".//div[@id='anx_IV']")
    part, sub = None, None
    items: list[dict] = []
    code_re = re.compile(r"^(\d[A-E]\d{3}(?:\.[a-z0-9]{1,4})*\.?)$")
    for el in anx.iter():
        if not isinstance(el.tag, str):
            continue
        c = cls(el)
        if el.tag == "p" and c.startswith("title-gr-seq"):
            t = text(el)
            m = re.match(r"^PART\s+(I{1,3})\b", t)
            if m:
                part, sub = m.group(1), None
            elif part and not t.startswith("("):
                sub = t
            continue
        if el.tag == "tr" and part:
            tds = el.findall("td")
            if len(tds) >= 2:
                k = text(tds[0])
                if code_re.match(k):
                    items.append({"code": normalize_code(k), "code_official": k, "part": part, "section": sub,
                                  "text": text(tds[1])})
            continue
        if el.tag == "div" and "grid-container" in c and part:
            col1 = el.find("./div[@class='list grid-list-column-1']")
            col2 = el.find("./div[@class='grid-list-column-2']")
            if col1 is not None and col2 is not None and code_re.match(text(col1)):
                items.append({"code": normalize_code(text(col1)), "code_official": text(col1), "part": part,
                              "section": sub, "text": text(col2)})
    if len(items) < 30:
        raise SystemExit(f"Annex IV parse produced only {len(items)} items; format changed?")
    return {"items": items}


# ---------------------------------------------------------------------------------------------
# Correlation table
# ---------------------------------------------------------------------------------------------
def fetch_correlation(client: httpx.Client, on: str) -> tuple[bytes, dict]:
    r = client.get(CIRCABC_LIST.format(id=CIRCABC_FOLDER), headers={"Accept": "application/json"})
    r.raise_for_status()
    files = r.json().get("data") or []
    year = on[:4]
    cands = [f for f in files if re.match(rf"^{year} - Dual use correlation table", f.get("name") or "")
             and (f.get("name") or "").endswith(".xlsx")]
    if not cands:
        cands = [f for f in files if (f.get("name") or "").endswith(".xlsx")]
    cands.sort(key=lambda f: (f.get("properties") or {}).get("modified") or "", reverse=True)
    f = cands[0]
    d = client.get(CIRCABC_DOWNLOAD.format(id=f["id"]))
    d.raise_for_status()
    return d.content, {"name": f["name"], "id": f["id"], "modified": (f.get("properties") or {}).get("modified"),
                       "url": CIRCABC_DOWNLOAD.format(id=f["id"]),
                       "folder_url": f"https://circabc.europa.eu/ui/group/0e5f18c2-4b2f-42e9-aed4-dfe50ae1263b/library/{CIRCABC_FOLDER}"}


def parse_correlation(data: bytes, annex_codes: set[str]) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [clean(str(c or "")) for c in rows[0]]
    if not (hdr[0].lower().startswith("cn codes") and "footnote" in hdr[1].lower()):
        raise SystemExit(f"unexpected correlation table header {hdr}")
    by_cn: dict[str, list[dict]] = {}
    unknown = set()
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        cn = re.sub(r"\D", "", str(r[0]))
        fn = clean(str(r[1] or ""))
        du = clean(str(r[2] or ""))
        if len(cn) != 10 or not du:
            continue
        code = normalize_code(du)
        e = code
        while e not in annex_codes and "." in e:
            e = e.rsplit(".", 1)[0]
        if e not in annex_codes:
            unknown.add(du)
        rec = {"footnote": fn, "du_code": du, "code": code, "annex_i": e if e in annex_codes else None}
        lst = by_cn.setdefault(cn, [])
        if rec not in lst:
            lst.append(rec)
    return {"cn": by_cn, "rows": len(rows) - 1, "unknown_codes": sorted(unknown)}


# ---------------------------------------------------------------------------------------------
# EU Sanctions Map: arms embargoes and dual-use bans per country
# ---------------------------------------------------------------------------------------------
ARMS_TYPES = {2: "arms_embargo", 4: "arms_export"}
DU_TYPES = {7: "dual_use_embargo", 8: "dual_use_export", 62: "dual_use_entities"}


def parse_sanctions_map(data: bytes) -> dict:
    regimes = json.loads(data)["data"]
    out: dict[str, dict] = {}
    for r in regimes:
        c = ((r.get("country") or {}).get("data") or {})
        iso = c.get("code")
        if not iso:
            continue
        for m in (r.get("measures") or {}).get("data") or []:
            tid = ((m.get("type") or {}).get("data") or {}).get("id")
            kind = ARMS_TYPES.get(tid) or DU_TYPES.get(tid)
            if not kind:
                continue
            desc = clean(m.get("description") or "")
            countrywide = re.search(r"\bto (?:the )?(?!listed|designated|non-governmental|all\b)[A-Z]", desc)
            targeted = bool(re.search(r"\bto (?:all |the )?(?:listed|designated|non-governmental)", desc, re.I)) \
                and not countrywide
            political = desc.lower().startswith("political declaration")
            acts = [{"title": a.get("title"), "number": a.get("number"), "url": a.get("url")}
                    for a in ((r.get("legal_acts") or {}).get("data") or [])][:4]
            rec = out.setdefault(iso, {"country": c.get("title"), "measures": []})
            rec["measures"].append({"kind": kind, "targeted": targeted, "political_declaration": political,
                                    "regime": r.get("specification"), "regime_amended_at": r.get("amendment"),
                                    "regime_id": r.get("id"), "programme": r.get("programme"),
                                    "description": desc[:600], "legal_acts": acts})
    for iso, rec in out.items():
        ms = rec["measures"]
        rec["arms_embargo"] = any(m["kind"] in ARMS_TYPES.values() and not m["targeted"] and not m["political_declaration"]
                                  for m in ms)
        rec["arms_embargo_political"] = any(m["kind"] in ARMS_TYPES.values() and m["political_declaration"] for m in ms)
        rec["arms_embargo_targeted"] = any(m["kind"] in ARMS_TYPES.values() and m["targeted"] for m in ms)
        rec["dual_use_ban"] = any(m["kind"] in ("dual_use_embargo", "dual_use_export") and not m["targeted"] for m in ms)
    return out


# ---------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--on", default=dt.date.today().isoformat())
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "backend", "data", "dualuse"))
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    os.makedirs(os.path.join(out, "source"), exist_ok=True)
    fetched = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    with httpx.Client(timeout=httpx.Timeout(300, connect=30), follow_redirects=True, headers=UA) as client:
        celex = latest_consolidated(client, "02021R0821", args.on)
        xhtml, cellar_url = fetch_celex(client, celex)
        corr_bytes, corr_meta = fetch_correlation(client, args.on)
        smap = client.get(SANCTIONS_MAP, headers={"Accept": "application/json"})
        smap.raise_for_status()
    root = etree.parse(io.BytesIO(xhtml), etree.HTMLParser()).getroot()
    head = text(root)[:400]
    head_txt = text(root)[:6000]
    amended_block = head_txt[head_txt.find("Amended by"):head_txt.find("Corrected by") if "Corrected by" in head_txt else 6000]
    amended = [(str(i + 1), a) for i, a in enumerate(re.findall(r"COMMISSION DELEGATED REGULATION \(EU\) (\d{4}/\d+)",
                                                                   amended_block))]
    annex1 = parse_annex_i(root)
    annex2 = parse_annex_ii(root)
    annex4 = parse_annex_iv(root)
    codes = {e["code"] for e in annex1["entries"]}
    corr = parse_correlation(corr_bytes, codes)
    embargo = parse_sanctions_map(smap.content)

    src = {"celex": celex, "title": "Regulation (EU) 2021/821 (dual-use), consolidated text",
           "eurlex_url": EURLEX.format(celex=celex), "cellar_url": cellar_url,
           "fetched_at": fetched, "valid_on": args.on, "sha256": sha256(xhtml), "bytes": len(xhtml),
           "header": head[:160], "amended_by": [{"m": f"M{m}", "act": f"Commission Delegated Regulation (EU) {a}"}
                                                 for m, a in amended]}
    gz = os.path.join(out, "source", f"{celex}.en.xhtml.gz")
    with gzip.GzipFile(gz, "wb", mtime=0) as g:
        g.write(xhtml)
    corr_path = os.path.join(out, "source", re.sub(r"[^\w.\- ]", "_", corr_meta["name"]))
    with open(corr_path, "wb") as f:
        f.write(corr_bytes)

    n_entries = sum(1 for e in annex1["entries"] if e["level"] == "entry")
    write = lambda name, obj: json.dump(obj, open(os.path.join(out, name), "w", encoding="utf-8"),  # noqa: E731
                                        ensure_ascii=False, indent=1, sort_keys=False)
    write("annex1.json", {"source": src, "regime_names": REGIME_NAMES, "count_entries": n_entries,
                          "count_nodes": len(annex1["entries"]), **annex1})
    write("annex2_eugea.json", {"source": src, **annex2})
    write("annex4.json", {"source": src, **annex4})
    write("cn_correlation.json", {"source": {**corr_meta, "fetched_at": fetched, "sha256": sha256(corr_bytes),
                                             "bytes": len(corr_bytes), "rows": corr["rows"],
                                             "publisher": "European Commission, DG TAXUD (CIRCABC)",
                                             "note": "CN 10-digit (TARIC) code -> TARIC footnote DUxxx -> Annex I code"},
                                  "unknown_codes": corr["unknown_codes"], "cn": corr["cn"]})
    write("embargo_destinations.json", {"source": {"url": SANCTIONS_MAP, "fetched_at": fetched,
                                                   "sha256": sha256(smap.content), "bytes": len(smap.content),
                                                   "publisher": "EU Sanctions Map (www.sanctionsmap.eu)"},
                                        "countries": embargo})
    print(json.dumps({"celex": celex, "annex_i_entries": n_entries, "annex_i_nodes": len(annex1["entries"]),
                      "warnings": annex1["warnings"][:10], "eugea": sorted(annex2["eugea"]),
                      "section_i_codes": len(annex2["section_i"]["codes"]), "annex_iv_items": len(annex4["items"]),
                      "correlation_cn_codes": len(corr["cn"]), "correlation_unknown": corr["unknown_codes"][:20],
                      "embargo_countries": sorted(k for k, v in embargo.items() if v["arms_embargo"]),
                      "sha256": src["sha256"], "corr_file": corr_meta["name"]}, indent=1))


if __name__ == "__main__":
    main()
