#!/usr/bin/env python3
"""Build backend/data/sanctions_goods/* : CN-coded goods prohibitions of the EU country sanctions regulations and
the Commission's list of Common High Priority Items (CHPI).

Sources (official, no key):
  * Latest consolidated versions in force on --on of
      RU  Council Regulation (EU) No 833/2014   (CELEX 02014R0833-*)
      BY  Council Regulation (EC) No 765/2006   (CELEX 02006R0765-*)
      IR  Council Regulation (EU) No 267/2012   (CELEX 02012R0267-*)
      KP  Council Regulation (EU) 2017/1509     (CELEX 02017R1509-*)
      SY  Council Regulation (EU) No 36/2012    (CELEX 02012R0036-*)
    fetched as English XHTML from the Publications Office Cellar (same text as EUR-Lex).
    Every annex table row whose first cell is a CN code becomes an item. The flow of each annex (export / import /
    transit / due diligence) is derived from the article paragraphs that cite the annex ("It shall be prohibited to
    sell, supply, transfer or export ... listed in Annex XXIII" -> export), and the paragraph is stored as the basis.
  * CHPI: Annex XL of Regulation 833/2014 (legal list) + the Commission's "List of Common High Priority Items"
    PDF (finance.ec.europa.eu, tiers 1-4).

Usage: /home/user/work/venv/bin/python scripts/build_goods_sanctions_data.py [--on 2026-10-24] [--out backend/data/sanctions_goods]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys

import httpx
from lxml import etree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_dualuse_data import UA, clean, cls, fetch_celex, latest_consolidated, text  # noqa: E402

REGULATIONS = {
    "RU": {"base": "02014R0833", "act": "Council Regulation (EU) No 833/2014", "legal_point": "ru_by_trade"},
    "BY": {"base": "02006R0765", "act": "Council Regulation (EC) No 765/2006", "legal_point": "ru_by_trade"},
    "IR": {"base": "02012R0267", "act": "Council Regulation (EU) No 267/2012", "legal_point": None},
    "KP": {"base": "02017R1509", "act": "Council Regulation (EU) 2017/1509", "legal_point": None},
    "SY": {"base": "02012R0036", "act": "Council Regulation (EU) No 36/2012", "legal_point": None},
}
CHPI_PDF = "https://finance.ec.europa.eu/document/download/5a2494db-d874-4e2b-bf2a-ec5a191d2dc0_en"
EURLEX = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"

CN_CELL = re.compile(r"^(?P<ex>ex\s*)?(?P<code>\d{2}(?:\s?\d{2}){0,4})$", re.I)
CHAPTER_CELL = re.compile(r"^(?:Chapter|CN chapter)\s+(\d{1,2})$", re.I)
ROMAN = r"[IVXLC]+[A-Z]?"


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def article_paragraphs(root) -> list[dict]:
    out = []
    for div in root.iter("div"):
        aid = div.get("id") or ""
        if not re.fullmatch(r"art_\w+", aid):
            continue
        t = text(div)
        art = aid[4:]
        # split "1. ... 2. ... 1a. ..." at paragraph numbers that start a sentence
        parts = re.split(r"(?:(?<=\.)|(?<=:)|(?<=;))\s+(?=\d{1,2}[a-z]{0,2}\.\s+[A-Z(])", t)
        for p in parts:
            m = re.match(r"^(\d{1,2}[a-z]{0,2})\.\s", p)
            out.append({"article": art, "paragraph": m.group(1) if m else None, "text": p})
    return out


def classify(par: str) -> set[str]:
    p = par.lower()
    flows = set()
    if "prohibit" in p or "shall not" in p:
        if re.search(r"(?:sell|sale)[,\s]+(?:or\s+)?supply[^.;]{0,60}\bexport|\bto export\b|\bexport, directly or indirectly", p):
            flows.add("export")
        if re.search(r"purchase[,\s]+(?:or\s+)?import|\bimport[,\s]+(?:or\s+)?(?:directly|purchase)|\bto import\b|"
                     r"import into the union", p):
            flows.add("import")
        if re.search(r"\btransit\b", p):
            flows.add("transit")
    if re.search(r"re-export|contractually prohibit|risks of exportation", p):
        flows.add("due_diligence")
    if re.search(r"authorisation|authorization|prior authoris", p) and not flows:
        flows.add("authorisation")
    return flows


def parse_annexes(root, paragraphs: list[dict]) -> dict:
    out = {}
    for div in root.iter("div"):
        aid = div.get("id") or ""
        m = re.fullmatch(rf"anx_({ROMAN})", aid)
        if not m:
            continue
        roman = m.group(1)
        heads = [text(p) for p in div.iter("p") if cls(p).startswith("title-annex")]
        title = " ".join(h for h in heads if not re.fullmatch(rf"ANNEX {roman}", h)) or ""
        if not title:
            first = [text(p) for p in div.iter("p") if text(p)][:3]
            title = " ".join(x for x in first if not x.startswith("ANNEX"))[:300]
        items, part = [], None
        for el in div.iter():
            if not isinstance(el.tag, str):
                continue
            if el.tag == "p" and not el.iterancestors("table"):
                pass
            if el.tag == "p" and (cls(el).startswith("title-gr-seq") or re.match(r"^(PART|Part)\s+[A-Z0-9]+\b", text(el))) \
                    and not any(a.tag == "table" for a in el.iterancestors()):
                part = text(el)[:160]
                continue
            if el.tag != "tr":
                continue
            cells = [text(td) for td in el.findall("td")]
            if not cells:
                continue
            for i, ctext in enumerate(cells[:2]):
                c = clean(ctext)
                mc = CN_CELL.match(c)
                mch = CHAPTER_CELL.match(c)
                if mc or mch:
                    code = re.sub(r"\s", "", mc.group("code")) if mc else f"{int(mch.group(1)):02d}"
                    desc = " ".join(x for x in cells[i + 1:] if x)[:600]
                    items.append({"cn": code, "ex": bool(mc and mc.group("ex")), "cn_text": c, "description": desc,
                                  "part": part})
                    break
        if not items:
            continue
        ref = re.compile(rf"Annex(?:es)?\s+(?:[IVXLC]+[A-Z]?\s*(?:,|and|or)\s*)*{roman}(?![IVXLC])")
        basis, flows = [], set()
        for p in paragraphs:
            if ref.search(p["text"]):
                f = classify(p["text"])
                if f:
                    flows |= f
                    basis.append({"article": p["article"], "paragraph": p["paragraph"], "flows": sorted(f),
                                  "text": p["text"][:900]})
        if re.match(r"^Prices\b", title):
            flows = {"information"}  # a price table (oil price cap), not a list of prohibited goods
        out[roman] = {"annex": roman, "title": title, "flows": sorted(flows), "basis": basis[:12], "items": items}
    return out


def parse_chpi_pdf(data: bytes) -> dict:
    import pypdf

    r = pypdf.PdfReader(io.BytesIO(data))
    txt = "\n".join(p.extract_text() for p in r.pages)
    version = re.search(r"\(Version of ([A-Za-z]+ \d{4})\)", txt)
    tier, items = None, []
    for line in txt.splitlines():
        line = clean(line)
        m = re.match(r"^TIER\s+(\d(?:\.[AB])?)\b", line)
        if m:
            tier = m.group(1)
            continue
        m = re.match(r"^(\d{4})\.(\d{2})\b\s*(.*)$", line)
        if m and tier:
            items.append({"hs6": m.group(1) + m.group(2), "tier": tier, "text": m.group(3)})
        elif items and tier and line and not re.match(r"^(HS code|\d+$)", line):
            if not items[-1]["text"] or len(items[-1]["text"]) < 400:
                items[-1]["text"] = (items[-1]["text"] + " " + line).strip()
    return {"version": version.group(1) if version else None, "items": items}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--on", default=dt.date.today().isoformat())
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "backend", "data", "sanctions_goods"))
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    fetched = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    summary = {}
    ru_annex_xl = None
    with httpx.Client(timeout=httpx.Timeout(600, connect=30), follow_redirects=True, headers=UA) as client:
        for iso, meta in REGULATIONS.items():
            celex = latest_consolidated(client, meta["base"], args.on)
            xhtml, url = fetch_celex(client, celex)
            root = etree.parse(io.BytesIO(xhtml), etree.HTMLParser()).getroot()
            pars = article_paragraphs(root)
            annexes = parse_annexes(root, pars)
            doc = {"country": iso, "act": meta["act"], "legal_point": meta["legal_point"],
                   "source": {"celex": celex, "eurlex_url": EURLEX.format(celex=celex), "cellar_url": url,
                              "fetched_at": fetched, "valid_on": args.on, "sha256": sha256(xhtml), "bytes": len(xhtml)},
                   "annexes": annexes}
            json.dump(doc, open(os.path.join(out, f"{iso.lower()}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            summary[iso] = {"celex": celex, "annexes": {k: {"flows": v["flows"], "items": len(v["items"]),
                                                            "basis_articles": sorted({b["article"] for b in v["basis"]})}
                                                        for k, v in annexes.items()}}
            if iso == "RU":
                ru_annex_xl = annexes.get("XL")
        pdf = client.get(CHPI_PDF)
        pdf.raise_for_status()
    chpi = parse_chpi_pdf(pdf.content)
    legal = [{"hs6": it["cn"][:6], "description": it["description"]} for it in (ru_annex_xl or {}).get("items", [])]
    tiers = {it["hs6"]: it["tier"] for it in chpi["items"]}
    merged = [{**x, "tier": tiers.get(x["hs6"])} for x in legal]
    json.dump({"source": {"legal": {"act": "Council Regulation (EU) No 833/2014, Annex XL",
                                    "celex": summary["RU"]["celex"],
                                    "eurlex_url": EURLEX.format(celex=summary["RU"]["celex"])},
                          "commission_list": {"url": CHPI_PDF, "version": chpi["version"], "fetched_at": fetched,
                                              "sha256": sha256(pdf.content), "bytes": len(pdf.content),
                                              "publisher": "European Commission (DG FISMA), List of Common High "
                                                           "Priority Items"}},
               "items": merged, "commission_items": chpi["items"],
               "only_in_commission_list": sorted(set(tiers) - {x["hs6"] for x in legal}),
               "only_in_annex_xl": sorted({x["hs6"] for x in legal} - set(tiers))},
              open(os.path.join(out, "chpi.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(os.path.join(out, "source_chpi.pdf"), "wb") as f:
        f.write(pdf.content)
    summary["CHPI"] = {"annex_xl_items": len(legal), "pdf_items": len(chpi["items"]), "version": chpi["version"]}
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
