"""Licences and certificates for customs declarations, from TARIC measure conditions, and the licence register.

Requirements come from the measure conditions of the EU TARIC consultation (services.taric): every import/export
restriction or prohibition measure whose condition lines say "presentation of a certificate/licence/document" with a
document code (C..., L..., N..., U..., X..., Y..., 9...) and whose last line (no document) is "Import/export not
allowed after control". The document codes and their descriptions are the official TARIC ones (English and Finnish,
measures_conditions.jsp?Lang=en|fi); these are also the document codes declared in DE 2/3.

How TARIC conditions are read (DG TAXUD, "TARIC web site - conditional measures", dds_condition_help.pdf):
conditions of the same type (letter) are mutually exclusive and read in sequence; conditions of different types apply
together (AND). So one condition type of one measure = one requirement; its document codes are alternatives.

Licence register: collection `customs_licences` {id, company_id, certificate_code, number, issuer, holder, goods[cn
prefixes], quantity_total, quantity_balance, unit (kg | p/st | l | EUR ...), valid_from, valid_to, document_id, status,
usage[{key, declaration_id, item_seq, amount, at}]}. `attribute(decl)` matches every requirement to a register entry,
an uploaded document carrying the certificate code, or a code a person declared on the item, and writes the DE 2/3
documents onto the item (`it["documents"]`). Immediately before a filing attempt the register balance is decremented
once per declaration item (idempotent).
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from core.audit import actor_ref, audit
from core.db import db, register_index
from core.util import NO_ID, new_id, now_iso
from services import taric

log = logging.getLogger("customs_licences")
HELSINKI = ZoneInfo("Europe/Helsinki")

register_index("customs_licences", "id", unique=True)
register_index("customs_licences", [("company_id", 1), ("certificate_code", 1), ("valid_to", -1)])

_CODE = re.compile(r"^[A-Z0-9][A-Z0-9]\d{2}$|^[A-Z0-9]\d{3}$")


def _error(status: int, message: str) -> HTTPException:
    fi = {400: "Lupatiedot ovat virheelliset. Tarkista asiakirjakoodi, määrä, yksikkö ja voimassaolo.",
          404: "Lupaa ei löytynyt.",
          409: "LIC-1: Lupa tai sen saldo ei kelpaa tähän ilmoitukseen. Tarkista lupa ja asiakirjat ennen lähettämistä."}
    return HTTPException(status, {"fi": fi.get(status, "Lupatietojen käsittely epäonnistui."), "en": message})


def _today() -> str:
    return datetime.now(HELSINKI).strftime("%Y-%m-%d")


def _day(on) -> str:
    if not on:
        return _today()
    d = re.sub(r"\D", "", str(on))[:8]
    datetime.strptime(d, "%Y%m%d")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


# ---------------------------------------------------------------------------------------------
# Classification of a document code by its official description
# ---------------------------------------------------------------------------------------------
_EXEMPT = re.compile(r"\b(goods other than|other than those|not concerned|not included|not subject|do not apply|does not "
                     r"apply|not covered|exempt|exemption|not listed|outside the scope)\b", re.I)
_LICENCE = re.compile(r"\b(licen[cs]e|authori[sz]ation|authori[sz]ed|permit|consent)\b", re.I)


def certificate_kind(code: str | None, text_en: str | None) -> str:
    """licence | certificate | exemption, from the official TARIC description of the document code."""
    t = text_en or ""
    if _EXEMPT.search(t):
        return "exemption"
    if _LICENCE.search(t):
        return "licence"
    return "certificate"


def _is_prohibition_measure(m: dict) -> bool:
    if m.get("kind") == "prohibition":
        return True
    texts = " ".join((c.get("text") or "") for c in m.get("conditions") or [])
    return bool(re.search(r"\bprohibitions?\b", texts, re.I))


# ---------------------------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------------------------
async def requirements(item: dict, *, flow: str, country: str | None, on=None) -> list[dict]:
    """Licences / certificates / exemptions / prohibitions TARIC requires for one goods item.
    -> [{certificate_code, kind, measure_type, measure_type_text, text_fi, text_en, regulation, alternatives[],
         condition_code, blocking, measure_sid, geo_code}]  (raises taric.TaricUnavailable)"""
    code = taric.item_code(item)
    if not code:
        return []
    r = await taric.lookup(code, country=country, flow=flow, on=on)
    out = []
    for m in r["measures"]:
        if m.get("kind") not in ("restriction", "prohibition"):
            continue
        conds = m.get("conditions") or []
        prohib = _is_prohibition_measure(m)
        if not conds:
            if m.get("kind") == "prohibition":
                out.append({"certificate_code": None, "kind": "prohibition", "measure_type": m.get("type_code"),
                            "measure_type_candidates": m.get("type_code_candidates"), "measure_type_text": m.get("type"),
                            "text_en": m.get("type"), "text_fi": None, "regulation": m.get("regulation"),
                            "alternatives": [], "condition_code": None, "blocking": True, "measure_sid": m.get("sid"),
                            "geo_code": m.get("geo_code"), "footnotes": m.get("footnotes")})
            continue
        fi = None
        groups: dict[str, list] = {}
        for c in conds:
            groups.setdefault(c["code"], []).append(c)
        for letter, lines in groups.items():
            certs = [c for c in lines if c.get("certificate") and c.get("action_kind") != "not_allowed"]
            if not certs:
                continue
            fallback = [c for c in lines if not c.get("certificate") and not c.get("requirement")]
            blocking = bool(fallback) and fallback[-1].get("action_kind") == "not_allowed"
            if not blocking:
                continue  # without the document the goods still pass (e.g. another duty applies): not a licence matter
            if fi is None:
                try:
                    fi = await taric.conditions(m["sid"], on=on, lang="fi")
                except taric.TaricUnavailable:
                    fi = {"certificates": {}}
            alts = []
            for c in certs:
                alts.append({"code": c["certificate"], "kind": certificate_kind(c["certificate"], c.get("text")),
                             "text_en": c.get("text"), "text_fi": (fi.get("certificates") or {}).get(c["certificate"]),
                             "action": c.get("action"), "sequence": c.get("sequence")})
            primary = next((a for a in alts if a["kind"] != "exemption"), alts[0])
            out.append({
                "certificate_code": primary["code"],
                "kind": "prohibition" if prohib else primary["kind"],
                "measure_type": m.get("type_code"), "measure_type_candidates": m.get("type_code_candidates"),
                "measure_type_text": m.get("type"), "text_en": primary["text_en"], "text_fi": primary["text_fi"],
                "regulation": m.get("regulation"), "alternatives": [a for a in alts if a is not primary],
                "condition_code": letter, "blocking": True, "measure_sid": m.get("sid"), "geo_code": m.get("geo_code"),
                "geo_name": m.get("geo_name"), "fallback_action": fallback[-1].get("action"),
                "footnotes": m.get("footnotes"),
            })
    return out


# ---------------------------------------------------------------------------------------------
# Licence register
# ---------------------------------------------------------------------------------------------
LICENCE_FIELDS = ("company_id", "certificate_code", "number", "issuer", "holder", "goods", "quantity_total",
                  "quantity_balance", "unit", "valid_from", "valid_to", "document_id", "status", "note")


def _clean(body: dict, *, partial: bool) -> dict:
    out = {}
    for k in LICENCE_FIELDS:
        if k not in body:
            continue
        v = body[k]
        if k == "certificate_code":
            v = re.sub(r"\s+", "", str(v or "")).upper()
            if not _CODE.match(v):
                raise _error(400, "certificate_code must be a TARIC document code such as C052, X002 or L100")
        elif k == "goods":
            v = [re.sub(r"\D", "", str(x)) for x in (v or []) if re.sub(r"\D", "", str(x))]
        elif k in ("quantity_total", "quantity_balance"):
            try:
                v = None if v in (None, "") else float(v)
            except (TypeError, ValueError):
                raise _error(400, "Quantity must be a non-negative finite number")
            if v is not None and (not math.isfinite(v) or v < 0):
                raise _error(400, "Quantity must be a non-negative finite number")
        elif k in ("valid_from", "valid_to"):
            try:
                v = _day(v) if v else None
            except ValueError:
                raise _error(400, "Validity must be a calendar date")
        elif k == "status":
            if v not in ("active", "suspended", "expired", "revoked"):
                raise _error(400, "status must be active, suspended, expired or revoked")
        out[k] = v
    if not partial:
        for k in ("company_id", "certificate_code", "number"):
            if not out.get(k):
                raise _error(400, f"{k} is required")
    return out


async def create_licence(body: dict, actor: dict) -> dict:
    doc = _clean(body, partial=False)
    _validate_register(doc)
    if doc.get("quantity_total") is not None and doc.get("quantity_balance") is None:
        doc["quantity_balance"] = doc["quantity_total"]
    doc = {"id": new_id(), "status": "active", "goods": [], "usage": [], **doc, "created_at": now_iso(),
           "created_by": actor_ref(actor), "updated_at": now_iso()}
    await db.customs_licences.insert_one(dict(doc))
    await audit(actor, "customs_licence_created", "customs_licence", doc["id"],
                {"certificate_code": doc["certificate_code"], "number": doc["number"]}, company_id=doc["company_id"])
    doc.pop("_id", None)
    return doc


async def update_licence(licence_id: str, body: dict, actor: dict) -> dict:
    sets = _clean(body, partial=True)
    if not sets:
        raise _error(400, "nothing to change")
    current = await db.customs_licences.find_one({"id": licence_id}, NO_ID)
    if not current:
        raise _error(404, "Licence not found")
    if current.get("usage") and any(k in sets for k in ("company_id", "certificate_code", "number", "unit",
                                                       "quantity_balance", "quantity_total")):
        raise _error(409, "A used licence's identity and balance cannot be overwritten")
    _validate_register({**current, **sets})
    sets["updated_at"] = now_iso()
    r = await db.customs_licences.update_one({"id": licence_id}, {"$set": sets})
    if not r.matched_count:
        raise _error(404, "Licence not found")
    await audit(actor, "customs_licence_updated", "customs_licence", licence_id, {"fields": sorted(sets)})
    return await db.customs_licences.find_one({"id": licence_id}, NO_ID)


def _validate_register(doc):
    if doc.get("valid_from") and doc.get("valid_to") and doc["valid_to"] < doc["valid_from"]:
        raise _error(400, "valid_to precedes valid_from")
    if doc.get("quantity_total") is not None:
        if not doc.get("unit"):
            raise _error(400, "A quantity-limited licence requires a unit")
        if doc.get("quantity_balance") is not None and doc["quantity_balance"] > doc["quantity_total"]:
            raise _error(400, "Balance exceeds the licensed quantity")


async def delete_licence(licence_id: str, actor: dict) -> dict:
    """Revoke rather than erase the audit and consumption history."""
    return await update_licence(licence_id, {"status": "revoked"}, actor)


async def list_licences(*, company_id: str | None = None, certificate_code: str | None = None, limit: int = 200) -> list:
    q = {}
    if company_id:
        q["company_id"] = company_id
    if certificate_code:
        q["certificate_code"] = certificate_code.upper()
    return await db.customs_licences.find(q, NO_ID).sort("valid_to", -1).to_list(limit)


def _amount_for(lic: dict, item: dict) -> float | None:
    """How much of a licence an item uses, in the licence's unit."""
    unit = (lic.get("unit") or "").strip().lower()
    if lic.get("quantity_total") is None and lic.get("quantity_balance") is None:
        return 0.0
    if unit in ("kg", "kgm", "kilogram"):
        return item.get("net_kg")
    if unit in ("t", "tonne", "tne"):
        return None if item.get("net_kg") is None else item["net_kg"] / 1000.0
    if unit in ("eur", "value"):
        return None if item.get("customs_value_cents") is None else item["customs_value_cents"] / 100.0
    iu = (item.get("unit") or "").strip().lower()
    if unit in ("p/st", "pcs", "pce", "pa", "l", "hl") and (not iu or iu == unit or unit == "p/st" and iu in ("pcs", "pce", "kpl")):
        return item.get("quantity")
    return None


def _goods_match(lic: dict, code: str | None) -> bool:
    goods = lic.get("goods") or []
    return not goods or bool(code and any(code.startswith(g) for g in goods))


async def _register_match(company_id: str, codes: list[str], item: dict, day: str, decl_id: str) -> tuple[dict | None, str | None]:
    if not company_id or not codes:
        return None, None
    lics = await db.customs_licences.find({"company_id": company_id, "certificate_code": {"$in": codes},
                                           "status": "active"}, NO_ID).to_list(50)
    code = taric.item_code(item)
    why = None
    for lic in lics:
        if lic.get("valid_from") and day < lic["valid_from"] or lic.get("valid_to") and day > lic["valid_to"]:
            why = why or f"{lic['certificate_code']} {lic['number']} is not valid on {day}"
            continue
        if not _goods_match(lic, code):
            why = why or f"{lic['certificate_code']} {lic['number']} does not cover {code}"
            continue
        need = _amount_for(lic, item)
        used_here = sum(u.get("amount") or 0 for u in lic.get("usage") or []
                        if u.get("declaration_id") == decl_id and u.get("item_seq") == item.get("seq"))
        if need is None:
            why = why or f"{lic['certificate_code']} {lic['number']}: the item quantity in {lic.get('unit')} is missing"
            continue
        if lic.get("quantity_balance") is not None and need > (lic["quantity_balance"] + used_here) + 1e-9:
            why = why or (f"{lic['certificate_code']} {lic['number']}: balance {lic['quantity_balance']:g} {lic.get('unit')} "
                          f"is less than {need:g}")
            continue
        return lic, None
    return None, why


async def attribute(decl: dict, *, consume: bool = False, flow: str | None = None, on=None) -> dict:
    """Match every TARIC licence/certificate requirement of every item to the register, uploaded documents or codes a
    person declared on the item; write DE 2/3 documents onto the items. consume=True (on filing) decrements register
    balances once per declaration item. -> {items: [{seq, requirements, unmet}], consumed: [...]}"""
    flow = flow or decl.get("flow") or "import"
    day = _day(on)
    company_id = decl.get("company_id")
    shipment_docs = []
    if decl.get("shipment_id"):
        shipment_docs = await db.documents.find({"shipment_id": decl["shipment_id"], "certificate_code": {"$exists": True}},
                                                {"_id": 0, "id": 1, "certificate_code": 1, "certificate_number": 1,
                                                 "filename": 1}).to_list(100)
    summary = {"items": [], "consumed": [], "status": "ok"}
    for it in decl.get("items") or []:
        country = it.get("origin_country") if flow == "import" else (decl.get("destination_country") or it.get("destination_country"))
        if not taric.item_code(it):
            it["licence_requirements"] = []
            it["licence_check"] = {"status": "needs_code", "at": now_iso()}
            continue
        try:
            reqs = await requirements(it, flow=flow, country=country, on=on)
        except (taric.TaricUnavailable, ValueError) as e:
            it["licence_check"] = {"status": "unavailable", "error": str(e)[:300], "at": now_iso()}
            summary["status"] = "unavailable"
            continue
        docs = [d for d in (it.get("documents") or []) if d.get("source") not in ("register", "document")]
        declared = {d.get("code"): d for d in docs if d.get("code") and str(d.get("reference") or "").strip()}
        for req in reqs:
            codes = [c for c in [req.get("certificate_code")] + [a["code"] for a in req.get("alternatives") or []] if c]
            req["satisfied_by"] = None
            if req["kind"] == "prohibition" and not codes:
                continue
            hit = next((declared[c] for c in codes if c in declared), None)
            if hit:
                req["satisfied_by"] = {"source": hit.get("source") or "declared", "code": hit["code"],
                                       "reference": hit.get("reference")}
                continue
            sdoc = next((d for d in shipment_docs if d.get("certificate_code") in codes), None)
            if sdoc:
                req["satisfied_by"] = {"source": "document", "code": sdoc["certificate_code"], "document_id": sdoc["id"],
                                       "reference": sdoc.get("certificate_number") or sdoc.get("filename")}
                docs.append({"code": sdoc["certificate_code"], "reference": sdoc.get("certificate_number") or sdoc.get("filename"),
                             "source": "document", "document_id": sdoc["id"]})
                continue
            lic, why = await _register_match(company_id, [c for c in codes if certificate_kind(c, None) != "exemption"],
                                             it, day, decl.get("id"))
            if lic:
                req["satisfied_by"] = {"source": "register", "code": lic["certificate_code"], "licence_id": lic["id"],
                                       "reference": lic["number"], "valid_to": lic.get("valid_to")}
                docs.append({"code": lic["certificate_code"], "reference": lic["number"], "source": "register",
                             "licence_id": lic["id"], "issuer": lic.get("issuer"), "valid_to": lic.get("valid_to")})
            elif why:
                req["register_note"] = why
        it["documents"] = _dedupe(docs)
        it["licence_requirements"] = reqs
        it["licence_check"] = {"status": "ok", "flow": flow, "country": country, "date": day, "at": now_iso()}
        summary["items"].append({"seq": it.get("seq"), "requirements": len(reqs),
                                 "unmet": [r["certificate_code"] or r["measure_type_text"] for r in reqs if not r["satisfied_by"]]})
    if consume:
        if any(not r["ok"] and r["severity"] == "blocking" for r in rules(decl)):
            raise _error(409, "LIC-1: resolve licence and certificate requirements before filing")
        try:
            for it in decl.get("items") or []:
                for lid in {d["licence_id"] for d in it.get("documents") or [] if d.get("source") == "register"}:
                    lic = await db.customs_licences.find_one({"id": lid}, NO_ID)
                    used = await _consume(lic, it, decl)
                    if used:
                        summary["consumed"].append(used)
        except Exception:
            # Roll back only this call's reservations; retries' earlier usage is preserved.
            for used in summary["consumed"]:
                upd = {"$pull": {"usage": {"key": used["key"]}}}
                if used["limited"]:
                    upd["$inc"] = {"quantity_balance": used["amount"]}
                await db.customs_licences.update_one({"id": used["licence_id"], "usage.key": used["key"]}, upd)
            raise
    return summary


def _dedupe(docs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for d in docs:
        k = (d.get("code"), d.get("reference"))
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
    return out


async def _consume(lic: dict, item: dict, decl: dict) -> dict | None:
    """Decrement the licence balance once for this declaration item (atomic; a second call is a no-op)."""
    if not lic or lic.get("status") != "active":
        raise _error(409, "LIC-1: licence is no longer active")
    amount = _amount_for(lic, item)
    if amount is None or not math.isfinite(amount) or amount < 0:
        raise _error(409, "LIC-1: licence quantity is missing or invalid")
    key = f"{decl.get('id')}:{item.get('seq')}"
    q = {"id": lic["id"], "status": "active", "usage.key": {"$ne": key}}
    upd = {"$push": {"usage": {"key": key, "declaration_id": decl.get("id"), "lrn": decl.get("lrn"),
                               "item_seq": item.get("seq"), "amount": amount, "at": now_iso()}},
           "$set": {"updated_at": now_iso()}}
    if lic.get("quantity_balance") is not None and amount:
        q["quantity_balance"] = {"$gte": amount}
        upd["$inc"] = {"quantity_balance": -amount}
    r = await db.customs_licences.update_one(q, upd)
    if not r.modified_count:
        previous = await db.customs_licences.find_one({"id": lic["id"], "usage.key": key}, NO_ID)
        if previous and any(u["key"] == key and u["amount"] == amount for u in previous["usage"]):
            return None
        raise _error(409, "LIC-1: licence balance or previous filing quantity changed")
    return {"licence_id": lic["id"], "number": lic["number"], "amount": amount, "unit": lic.get("unit"), "key": key,
            "limited": lic.get("quantity_balance") is not None}


# ---------------------------------------------------------------------------------------------
# Validation rules (customs.validate)
# ---------------------------------------------------------------------------------------------
def rules(decl: dict) -> list[dict]:
    """LIC-1: a required licence/certificate is neither in the register, nor uploaded, nor declared; a TARIC
    prohibition applies. Blocking, with the official TARIC text in Finnish and English."""
    out = []
    for it in decl.get("items") or []:
        n = it.get("seq")
        chk = it.get("licence_check") or {}
        if chk.get("status") in (None, "unavailable", "needs_code"):
            out.append(_rule("LIC-1", False, "Licence and certificate requirements could not be read from the EU TARIC "
                                             "consultation; filing waits until TARIC answers",
                             "Lupa- ja todistusvaatimuksia ei saatu EU:n TARIC-palvelusta; ilmoitusta ei lähetetä ennen "
                             "kuin TARIC vastaa", item=n))
            continue
        for req in it.get("licence_requirements") or []:
            if req.get("satisfied_by"):
                s = req["satisfied_by"]
                out.append(_rule("LIC-1", True, f"{s['code']} {s.get('reference') or ''} ({s['source']}) for "
                                                f"{req.get('measure_type_text')} ({req.get('regulation')})".replace("  ", " "),
                                 f"{s['code']} {s.get('reference') or ''} ({s['source']}): {req.get('measure_type_text')} "
                                 f"({req.get('regulation')})".replace("  ", " "), item=n, severity="advisory"))
                continue
            codes = [c for c in [req.get("certificate_code")] + [a["code"] for a in req.get("alternatives") or []] if c]
            lead_en = {"prohibition": "Prohibited unless an exemption or authorisation applies",
                       "licence": "Licence required", "certificate": "Certificate required",
                       "exemption": "Exemption statement required"}[req["kind"]]
            lead_fi = {"prohibition": "Kielletty, ellei poikkeus tai lupa koske tavaraa", "licence": "Lupa vaaditaan",
                       "certificate": "Todistus vaaditaan", "exemption": "Poikkeusilmoitus vaaditaan"}[req["kind"]]
            en = (f"{lead_en}: {req.get('measure_type_text')} ({req.get('regulation')}, {req.get('geo_name') or req.get('geo_code')}). "
                  f"{req.get('certificate_code') or ''} {req.get('text_en') or ''}".strip())
            fi = (f"{lead_fi}: {req.get('measure_type_text')} ({req.get('regulation')}). "
                  f"{req.get('certificate_code') or ''} {req.get('text_fi') or req.get('text_en') or ''}".strip())
            if codes:
                en += f" Accepted document codes (DE 2/3): {', '.join(codes)}."
                fi += f" Hyväksytyt asiakirjakoodit (DE 2/3): {', '.join(codes)}."
            if req.get("register_note"):
                en += f" Register: {req['register_note']}."
            v = _rule("LIC-1", False, en, fi, item=n)
            v["licence_requirement"] = {k: req.get(k) for k in ("certificate_code", "kind", "measure_type", "regulation",
                                                                  "condition_code", "measure_sid")}
            out.append(v)
    return out


def _rule(rule, ok, en, fi=None, *, item=None, severity="blocking", owner="broker"):
    v = {"rule": rule, "ok": bool(ok), "severity": severity, "owner": owner, "message": en, "item": item, "field": None}
    v["message_i18n"] = {"en": en, "fi": fi or en}
    return v
