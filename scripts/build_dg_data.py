#!/usr/bin/env python3
"""Build the dangerous-goods dataset (backend/data/dg/un_numbers.json + segregation.json) from official sources.

Sources (all downloaded by this script; nothing is typed in by hand except the ADR 8.6.4 tunnel table, which is
transcribed and then verified word for word against the downloaded ADR text):

  1. US 49 CFR 172.101 Hazardous Materials Table (eCFR versioner API, US government work, public domain):
     UN/NA number, proper shipping name, class/division, label codes (-> subsidiary hazards), packing group,
     special provisions, column 9A (passenger aircraft/rail per package), 9B (cargo aircraft only), 10A/10B vessel stowage.
     Paragraph (k) of the same section gives the meaning of the stowage categories A-E / 01-05.
  2. 49 CFR 176.83(b) General Segregation Table for vessels (mirrors IMDG Code 7.2.4) -> segregation.json.
  3. 49 CFR 176.84 stowage/handling codes (the meaning of column 10B codes).
  4. ADR 2025 (ECE/TRANS/352, Vol. I & II, UNECE; official PDF from the UN Digital Library), Table A of chapter 3.2:
     classification code (3b), labels (5), limited quantity (7a), excepted quantity (7b), transport category and
     tunnel restriction code (15), hazard identification number (20). Parsed with pdfplumber from the column rulings.

Output is deterministic for the same input files: records sorted by UN number and packing group, JSON with sorted
keys. Source URL, retrieval date and sha256 of every input are written into the JSON `meta` block and SOURCES.md.

Usage:
  ~/work/venv/bin/python scripts/build_dg_data.py --cache ~/work/scratch-dg/raw [--ecfr-date 2026-10-22] [--offline]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "backend", "data", "dg")
ECFR = "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-49.xml?part={part}&section={section}"
ADR_URL = "https://digitallibrary.un.org/record/4068196/files/ECE_TRANS_352-EN.pdf"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
SECTIONS = {"172.101": "172", "176.83": "176", "176.84": "176"}

# ADR 8.6.4 table "Tunnel restriction code of the whole load -> Restriction". Each row: code, forbidden categories
# for tank (and bulk where the ADR says so) carriage, forbidden categories for other carriage, and the ADR wording
# the build checks against the downloaded PDF text. Explosive-mass codes (B1000C, C5000D) list the stricter set
# (above the mass limit) first and the lighter set second.
TUNNEL_864 = [
    ("B", "BCDE", "BCDE", "B Passage forbidden through tunnels of category B, C, D and E"),
    ("B1000C", "BCDE", "CDE", "exceeds 1000 kg: Passage forbidden through tunnels of category"),
    ("B/D", "BCDE", "DE", "B/D Tank carriage: Passage forbidden through tunnels of category B, C, D"),
    ("B/E", "BCDE", "E", "B/E Tank carriage: Passage forbidden through tunnels of category B, C, D"),
    ("C", "CDE", "CDE", "C Passage forbidden through tunnels of category C, D and E"),
    ("C5000D", "CDE", "DE", "exceeds 5000 kg: Passage forbidden through tunnels of category"),
    ("C/D", "CDE", "DE", "C/D Tank carriage: Passage forbidden through tunnels of category C, D and"),
    ("C/E", "CDE", "E", "C/E Tank carriage: Passage forbidden through tunnels of category C, D"),
    ("D", "DE", "DE", "D Passage forbidden through tunnels of category D and E"),
    ("D/E", "DE", "E", "D/E Bulk or tank carriage: Passage forbidden through tunnels of category D"),
    ("E", "E", "E", "E Passage forbidden through tunnels of category E"),
    ("-", "", "", "Passage allowed through all tunnels"),
]


# ------------------------------------------------------------------------------------------------
# Download with a sidecar (url, retrieved_at, sha256) so a rebuild from the cache is reproducible
# ------------------------------------------------------------------------------------------------
def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, path: str, *, offline: bool) -> dict:
    meta_path = path + ".source.json"
    if os.path.exists(path) and os.path.exists(meta_path):
        meta = json.load(open(meta_path))
        if meta.get("sha256") == sha256(path):
            return meta
    if offline:
        raise SystemExit(f"{path} is not in the cache and --offline was given")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=600) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
    with open(path, "wb") as f:
        f.write(raw)
    meta = {"url": url, "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sha256": sha256(path), "bytes": len(raw)}
    json.dump(meta, open(meta_path, "w"), indent=1)
    return meta


def ecfr_latest_date() -> str:
    with urllib.request.urlopen("https://www.ecfr.gov/api/versioner/v1/titles.json", timeout=60) as r:
        titles = json.load(r)["titles"]
    return next(t for t in titles if t["number"] == 49)["up_to_date_as_of"]


# ------------------------------------------------------------------------------------------------
# 49 CFR 172.101
# ------------------------------------------------------------------------------------------------
def _cell(c: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip()


def _roman(c: str) -> str:
    """Roman-type (non-italic) part of a name cell: the proper shipping name without the italic qualifiers."""
    c = re.sub(r"<E T=\"0[34]\">.*?</E>", " ", c, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip(" ,")


def parse_qty(text: str) -> dict:
    """'5 L' / '25 kg' / '30 kg gross' / 'Forbidden' / 'No limit' / '50 mL or 50 g' -> structured limit."""
    t = (text or "").strip()
    out = {"text": t or None, "forbidden": t.lower() == "forbidden", "no_limit": t.lower() == "no limit",
           "value": None, "unit": None, "gross": "gross" in t.lower()}
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(L|kg|mL|g)(?: gross)?(?: or .*)?", t)
    if m:
        v, u = float(m.group(1)), m.group(2)
        out["value"], out["unit"] = (v / 1000, "L") if u == "mL" else ((v / 1000, "kg") if u == "g" else (v, u))
    return out


def parse_hmt(path: str) -> tuple[list[dict], dict]:
    s = open(path, encoding="utf-8").read()
    head = s[:s.find("§ 172.101 Hazardous Materials Table")]
    k = html.unescape(re.sub(r"<[^>]+>", " ", head))
    stowage = {}
    for m in re.finditer(r"Stowage category “([A-E]|0[1-5])” means(.*?)(?=\(\d+\) Stowage category|\(l\) )", k, re.S):
        stowage[m.group(1)] = re.sub(r"\s+", " ", m.group(2)).strip(" —-")
    i = s.find("§ 172.101 Hazardous Materials Table")
    body = s[s.find("</THEAD>", i):s.find("</TABLE>", i)]
    rows = []
    cur = None
    for tr in re.findall(r"<TR>(.*?)</TR>", body, re.S):
        raw = re.findall(r"<TD[^>]*>(.*?)</TD>", tr, re.S)
        c = [_cell(x) for x in raw]
        if len(c) != 14:
            continue
        sym, name, cls, ident, pg, labels, sp, exc, nonbulk, bulk, q9a, q9b, s10a, s10b = c
        if name:
            cur = None
            if not ident or cls in ("", "Forbidden"):
                continue  # "see" cross references and forbidden materials without an id number
            cur = {"symbols": sym, "description": name, "name": _roman(raw[1]) or name, "class": cls, "id": ident.replace(" ", "")}
        elif cur is None or not (pg or labels or q9a or q9b):
            continue
        rows.append({
            "id": cur["id"], "symbols": [x for x in re.split(r"[,\s]+", cur["symbols"]) if x],
            "description": cur["description"], "name": cur["name"], "class": cur["class"],
            "pg": pg or None, "labels": [x.strip() for x in labels.split(",") if x.strip()] if labels else [],
            "special_provisions": [x.strip() for x in sp.split(",") if x.strip()],
            "packaging": {"exceptions": exc or None, "non_bulk": nonbulk or None, "bulk": bulk or None},
            "air": {"passenger": parse_qty(q9a), "cargo": parse_qty(q9b)},
            "vessel": {"stowage": s10a or None, "other": [x.strip() for x in s10b.split(",") if x.strip()]},
        })
    return rows, stowage


def parse_176_83(path: str) -> dict:
    s = open(path, encoding="utf-8").read()
    i = s.find("General Segregation Requirements for Hazardous Materials")
    tbl = s[s.rfind("<TABLE", 0, i):s.find("</TABLE>", i)]
    head = [_cell(x) for x in re.findall(r"<TH[^>]*>(.*?)</TH>", tbl, re.S)]
    cols = [h for h in head if re.fullmatch(r"[\d.,\s]+", h)]
    matrix = {}
    for tr in re.findall(r"<TR>(.*?)</TR>", tbl, re.S):
        c = [_cell(x) for x in re.findall(r"<T[DH][^>]*>(.*?)</T[DH]>", tr, re.S)]
        if len(c) != len(cols) + 1 or not re.search(r"\d", c[0]):
            continue
        key = re.search(r"([\d.]+(?:, [\d.]+)*)\s*$", c[0]).group(1)
        matrix[key] = dict(zip(cols, c[1:]))
    text = html.unescape(re.sub(r"<[^>]+>", " ", s))
    legend = {}
    for code, term in re.findall(r"(\d|X|\*)—“?([^”\n]+?)”?\.?\s*(?=\d—|X—|\*—|\(c\))", text):
        legend.setdefault(code, term.strip(" “”."))
    return {"columns": cols, "matrix": matrix, "legend": legend}


def parse_176_84(path: str) -> dict:
    s = open(path, encoding="utf-8").read()
    codes = {}
    for tr in re.findall(r"<TR>(.*?)</TR>", s, re.S):
        c = [_cell(x) for x in re.findall(r"<TD[^>]*>(.*?)</TD>", tr, re.S)]
        if len(c) == 2 and re.fullmatch(r"\d+[A-Z]?", c[0]) and c[1]:
            codes.setdefault(c[0], c[1])
    return codes


# ------------------------------------------------------------------------------------------------
# ADR 2025 Table A (chapter 3.2)
# ------------------------------------------------------------------------------------------------
ADR_COLS = "1 2 3a 3b 4 5 6 7a 7b 8 9a 9b 10 11 12 13 14 15 16 17 18 19 20".split()


def parse_adr(path: str) -> tuple[list[dict], dict]:
    import logging

    import pdfplumber
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    pdf = pdfplumber.open(path)
    width = pdf.pages[0].width
    rows, pages, full_text = [], [], []
    for n, page in enumerate(pdf.pages):
        txt = page.extract_text() or ""
        if "8.6.4" in txt or "Tunnel restriction code" in txt:
            full_text.append(txt)
        if not txt.startswith("UN No. Name and description Class Classifi-"):
            continue
        words = page.extract_words()
        if not words or min(w["x0"] for w in words) < 0:
            continue  # right half of a spread: the same words again, shifted left
        nxt = pdf.pages[n + 1] if n + 1 < len(pdf.pages) else None
        xs = {round(r["x0"], 1) for r in page.rects if r["width"] < 2 and r["height"] > 200}
        if nxt is not None:
            xs |= {round(r["x0"] + width, 1) for r in nxt.rects if r["width"] < 2 and r["height"] > 200}
        bounds = sorted(xs)
        labels = {f"({c})": c for c in ADR_COLS}
        hdr_top = min(w["top"] for w in words if w["text"] == "(3a)")
        hdr = [w for w in words if w["text"] in labels and abs(w["top"] - hdr_top) < 2]
        intervals = []
        for a, b in zip(bounds, bounds[1:]):
            lab = [labels[w["text"]] for w in hdr if a <= (w["x0"] + w["x1"]) / 2 < b]
            if lab and not any(iv[2] == lab[0] for iv in intervals):  # the repeated (1)/(2) at the right edge is skipped
                intervals.append((a, b, lab[0]))
        if [iv[2] for iv in intervals] != ADR_COLS:
            raise SystemExit(f"ADR page {n + 1}: unexpected column layout {[iv[2] for iv in intervals]}")
        pages.append(n + 1)
        body = sorted((w for w in words if hdr_top + 5 < w["top"] < 785), key=lambda w: (round(w["top"]), w["x0"]))

        def col(w):
            x = (w["x0"] + w["x1"]) / 2
            return next((iv[2] for iv in intervals if iv[0] <= x < iv[1]), None)
        starts = [w for w in body if col(w) == "1" and re.fullmatch(r"\d{4}", w["text"])]
        for i, st in enumerate(starts):
            top0 = st["top"] - 1
            top1 = starts[i + 1]["top"] - 1 if i + 1 < len(starts) else 10_000
            cells = defaultdict(list)
            for w in body:
                if top0 <= w["top"] < top1:
                    c = col(w)
                    if c:
                        cells[c].append(w["text"])
            rows.append(_adr_row(st["text"], {k: v for k, v in cells.items()}, n + 1))
    return rows, {"pages": pages, "tunnel_text": "\n".join(full_text)}


def _adr_row(un: str, cells: dict, page: int) -> dict:
    def j(c):
        return " ".join(cells.get(c, [])).strip()
    t15 = cells.get("15", [])
    tunnel = next((x.strip("()") for x in t15 if x.startswith("(")), None)
    if tunnel is not None:
        tunnel = re.sub(r"[─—–-]+", "-", tunnel)
    everything = " ".join(" ".join(v) for v in cells.values())
    squeezed = everything.replace(" ", "")
    prohibited, not_subject = "CARRIAGEPROHIBITED" in squeezed, "NOTSUBJECTTOADR" in squeezed
    cat = " ".join(x for x in t15 if not x.startswith("(")) or None
    if prohibited or not_subject or (cat or "").startswith(("CARRIAGE", "NOT")):
        cat = None
    labels = [x.lstrip("+") for x in re.split(r"\s+", j("5")) if re.fullmatch(r"\+?\d(\.\d)?[A-Z]?", x)]
    return {"un": f"UN{un}", "name": j("2"), "class": j("3a") or None, "classification_code": j("3b") or None,
            "pg": j("4") or None, "labels": labels, "special_provisions": [x for x in j("6").split() if x],
            "limited_quantity": j("7a") or None, "excepted_quantity": j("7b") or None,
            "transport_category": cat, "tunnel_code": tunnel, "hazard_id": j("20") or None,
            "carriage_prohibited": prohibited, "not_subject_to_adr": not_subject, "page": page}


# ------------------------------------------------------------------------------------------------
# Merge
# ------------------------------------------------------------------------------------------------
def class_of(division: str) -> str:
    return division.split(".")[0] if division else division


def subsidiary(labels: list[str], primary: str) -> list[str]:
    """49 CFR label codes -> subsidiary hazards: every label that is not the primary class label."""
    prim = re.sub(r"[A-Z]$", "", primary)  # 1.1D -> 1.1
    out = []
    for lab in labels:
        lab = lab.replace(" ", "")
        if lab in (primary, prim) or (prim.startswith("1.") and lab.startswith("1.") and lab[:3] == prim[:3]):
            continue
        out.append(lab)
    return out


def build(cache: str, ecfr_date: str, offline: bool) -> dict:
    os.makedirs(cache, exist_ok=True)
    src = {}
    for sec, part in SECTIONS.items():
        src[sec] = fetch(ECFR.format(date=ecfr_date, part=part, section=sec), os.path.join(cache, f"{sec}.xml"), offline=offline)
    src["adr"] = fetch(ADR_URL, os.path.join(cache, "ECE_TRANS_352-EN.pdf"), offline=offline)

    hmt, stowage_cats = parse_hmt(os.path.join(cache, "172.101.xml"))
    seg = parse_176_83(os.path.join(cache, "176.83.xml"))
    codes_10b = parse_176_84(os.path.join(cache, "176.84.xml"))
    adr, adr_info = parse_adr(os.path.join(cache, "ECE_TRANS_352-EN.pdf"))

    flat = re.sub(r"\s+", " ", adr_info["tunnel_text"])
    tunnel_rules = {}
    for code, tank, other, wording in TUNNEL_864:
        if wording not in flat:
            raise SystemExit(f"ADR 8.6.4 wording for {code!r} not found in the downloaded ADR text: {wording!r}")
        tunnel_rules[code] = {"tank": list(tank), "other": list(other), "adr_text": wording}

    by_key: dict[tuple, dict] = {}
    for r in hmt:
        if not re.fullmatch(r"(UN|NA|ID)\d{4}", r["id"]):
            continue
        key = (r["id"], r["pg"] or "")
        rec = by_key.setdefault(key, {"un": r["id"], "pg": r["pg"], "hmt": [], "adr": []})
        rec["hmt"].append({
            "name": r["name"], "description": r["description"], "division": r["class"], "class": class_of(r["class"]),
            "labels": r["labels"], "subsidiary": subsidiary(r["labels"], r["class"]), "symbols": r["symbols"],
            "special_provisions": r["special_provisions"], "packaging": r["packaging"], "air": r["air"], "vessel": r["vessel"]})
    for a in adr:
        key = (a["un"], a["pg"] or "")
        rec = by_key.setdefault(key, {"un": a["un"], "pg": a["pg"], "hmt": [], "adr": []})
        rec["adr"].append({k: v for k, v in a.items() if k not in ("un", "pg")})
    records = [by_key[k] for k in sorted(by_key)]
    for r in records:
        r["hmt"].sort(key=lambda x: (x["name"], x["description"]))
    un_ids = {r["un"] for r in records}
    meta = {
        "built_from": {
            "49cfr_172_101": {**src["172.101"], "ecfr_date": ecfr_date, "licence": "US Government work, public domain (17 U.S.C. 105)"},
            "49cfr_176_83": {**src["176.83"], "ecfr_date": ecfr_date, "licence": "US Government work, public domain (17 U.S.C. 105)"},
            "49cfr_176_84": {**src["176.84"], "ecfr_date": ecfr_date, "licence": "US Government work, public domain (17 U.S.C. 105)"},
            "adr_2025": {**src["adr"], "document": "ECE/TRANS/352 (ADR 2025, Vol. I & II), Table A of chapter 3.2 and 8.6.4",
                         "licence": "(c) United Nations; official free publication. Only factual entries (codes) are extracted."},
        },
        "counts": {"records": len(records), "ids": len(un_ids), "un_ids": len({u for u in un_ids if u.startswith("UN")}),
                   "hmt_rows": len(hmt), "adr_rows": len(adr), "adr_pages": len(adr_info["pages"]),
                   "records_with_hmt": sum(1 for r in records if r["hmt"]),
                   "records_with_adr": sum(1 for r in records if r["adr"]),
                   "records_with_both": sum(1 for r in records if r["hmt"] and r["adr"])},
        "not_included": {"imdg_ems": "IMDG EmS schedules are IMO copyright (paid); not shipped.",
                         "iata_dgr": "IATA DGR tables are licensed; air limits come from 49 CFR 172.101 columns 9A/9B. "
                                     "Set IATA_DGR_DATA_PATH to a licensed table to use it instead."},
    }
    segregation = {"meta": {"source": meta["built_from"]["49cfr_176_83"], "note": "49 CFR 176.83(b) General Segregation "
                            "Table; aligned with IMDG Code 7.2.4. Rows and columns are the class/division groups."},
                   "columns": seg["columns"], "matrix": seg["matrix"], "legend": seg["legend"],
                   "stowage_categories": stowage_cats, "stowage_codes": codes_10b, "tunnel_restrictions": tunnel_rules}
    return {"meta": meta, "records": records}, segregation


def write_sources_md(meta: dict, path: str):
    b = meta["built_from"]
    lines = ["# Dangerous goods dataset: sources", "",
             "Generated by `scripts/build_dg_data.py`; do not edit by hand. Rebuild with",
             "`~/work/venv/bin/python scripts/build_dg_data.py --cache <dir>`.", ""]
    for key, s in b.items():
        lines += [f"## {key}", "", f"- URL: {s['url']}", f"- Retrieved: {s['retrieved_at']}", f"- sha256: `{s['sha256']}`",
                  f"- Size: {s['bytes']} bytes"]
        if s.get("ecfr_date"):
            lines.append(f"- eCFR point-in-time date: {s['ecfr_date']}")
        if s.get("document"):
            lines.append(f"- Document: {s['document']}")
        lines += [f"- Licence: {s['licence']}", ""]
    lines += ["## Counts", ""] + [f"- {k}: {v}" for k, v in meta["counts"].items()] + [""]
    lines += ["## Not included", ""] + [f"- {k}: {v}" for k, v in meta["not_included"].items()] + [""]
    lines += ["## Field mapping", "",
              "- `hmt[]`: 49 CFR 172.101 columns 1 (symbols), 2 (name/description), 3 (division), 4 (id), 5 (pg), 6 (labels),",
              "  7 (special provisions), 8A-8C (packaging), 9A/9B (`air.passenger` / `air.cargo`, per package, net unless gross),",
              "  10A/10B (`vessel.stowage` / `vessel.other`).",
              "- `adr[]`: ADR 2025 Table A columns 2, 3a, 3b (classification code), 4, 5, 6, 7a (limited quantity),",
              "  7b (excepted quantity), 15 (transport category + tunnel restriction code), 20 (hazard identification number).",
              "- `segregation.json`: 49 CFR 176.83(b) matrix + legend, 172.101(k) stowage categories, 176.84 codes,",
              "  ADR 8.6.4 tunnel restriction table (transcribed, verified against the PDF text at build time).", ""]
    open(path, "w").write("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True, help="directory for the downloaded source files")
    ap.add_argument("--ecfr-date", default=None, help="eCFR point-in-time date (default: latest up_to_date_as_of)")
    ap.add_argument("--offline", action="store_true", help="use cached files only")
    ap.add_argument("--out", default=OUT_DIR)
    a = ap.parse_args(argv)
    d = a.ecfr_date or ecfr_latest_date()
    date.fromisoformat(d)
    data, seg = build(os.path.expanduser(a.cache), d, a.offline)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "un_numbers.json"), "w") as f:
        json.dump(data, f, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(a.out, "segregation.json"), "w") as f:
        json.dump(seg, f, sort_keys=True, ensure_ascii=False, indent=1)
    write_sources_md(data["meta"], os.path.join(a.out, "SOURCES.md"))
    print(json.dumps(data["meta"]["counts"]))


if __name__ == "__main__":
    sys.exit(main())
