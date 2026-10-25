"""Customs AEO-style controls: who may file, four-eyes thresholds, segregation of duties, the AEO audit trail,
customs compliance KPIs and record retention. Works the same for people and for the company running itself
(Autopilot); export, transit and ENS filings call the same `filing_controls(decl, actor, kind=...)`.

Who may file (all must hold, `may_file`):
  * a person: permission customs.file, an active internal account, and settings.customs_authorised_filers empty or
    naming them (user id or e-mail);
  * an agent: only under the director's recorded standing delegation (s2 settings customs_filing_delegated /
    customs_delegation_scope). These stay s2 legal human points (customs_filing_not_delegated,
    customs_indirect_representation): services.agents.customs_agent.gate_filing adds them as `legal_point`;
  * the customer's representation authorisation (power of attorney) exists, is signed, not revoked, in force today
    and covers this representation type (direct / indirect) and this declaration kind (import / export / transit / ens);
  * the declarant's EORI and the representative's EORI are valid in the EU EOS registry. The result KYC stored is
    used while it is younger than settings.customs_rescreen_days; older results are checked again live (EU EOS
    validateEORI; sandbox register for sandbox test companies) and stored back;
  * the importer / exporter KYC is approved and its sanctions screening is clear (and no party screening on the
    declaration is a potential match).

Four-eyes (`four_eyes.required`): duty + import VAT >= customs_four_eyes_duty_eur, customs value >=
customs_four_eyes_value_eur, any dual-use / licence / sanctions / trade-defence / preference-claim flag, the first
declaration filed for a customer, or filing again after Tulli rejected the declaration. Then the preparer (who asked
for the filing) and whoever classified or confirmed the CN codes (person or agent) cannot be the sole approver.
For a person this is enforced in core.approvals.four_eyes_block (existing codes four_eyes_required /
four_eyes_single_user and the audited single-user override); for the agent the reasons go to gate_filing's
extra_reasons so the card lands in the owner queue instead of auto-filing.

Every control decision is written to the audit log (action customs_aeo_controls; refusals customs_aeo_refused) and,
for a person's action, to agent_decisions through core.agent_runtime.decide(rule_check=True); the agent's own
filing decision is gate_filing's decide() row, which carries the AEO reasons.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import statistics
from datetime import date, timedelta

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from core import config
from core.audit import actor_ref, audit
from core.db import db, register_index
from core.jobs import job, schedule
from core.permissions import permissions_for
from core.settings import get_settings
from core.util import NO_ID, now, now_iso, parse_iso

log = logging.getLogger("customs_aeo")
AGENT = "customs_aeo"

# Declaration kinds and where they live (s11 interfaces: separate collections so the import sweeper never sees them).
KINDS = {
    "import": {"collection": "customs_declarations", "entity": "customs_declaration", "fi": "tuonti", "en": "import",
               "mode_env": "TULLI_IMPORT_MODE"},
    "export": {"collection": "export_declarations", "entity": "export_declaration", "fi": "vienti", "en": "export",
               "mode_env": "TULLI_EXPORT_MODE"},
    "transit": {"collection": "transit_declarations", "entity": "transit_declaration", "fi": "passitus", "en": "transit",
                "mode_env": "TULLI_TRANSIT_MODE"},
    "ens": {"collection": "ens_filings", "entity": "ens_filing", "fi": "saapumisen yleisilmoitus (ENS)",
            "en": "entry summary (ENS)", "mode_env": "ICS2_MODE"},
}
ENTITIES = {k["entity"]: kind for kind, k in KINDS.items()}
LIVE_MODES = {"test", "production", "sti", "conformance"}
# Statuses that mean the declaration reached Tulli (any kind).
FILED = ["submitted", "accepted", "under_control", "released", "not_released", "closed", "exited", "arrived",
         "transit_released", "transit_closed", "registered"]
FINAL = {"released", "not_released", "invalidated", "closed", "exited", "transit_closed", "do_not_load"}
CLASSIFICATION_FIELDS = {"hs_code", "cn_code", "taric_code", "taric_additional_codes"}
DUAL_USE_KINDS = {"dual_use"}
LICENCE_KINDS = {"military", "licence", "licence_required", "licence_needed", "certificate_required", "cites"}
SANCTIONS_KINDS = {"ru_by_origin", "sanctions", "sanctions_goods", "sanctions_party"}
TRADE_DEFENCE_KINDS = {"trade_defence"}

register_index("aeo_eori_checks", "eori", unique=True)


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
def eur_fi(cents: int | float | None) -> str:
    return f"{(cents or 0) / 100:,.2f}".replace(",", " ").replace(".", ",") + " €"


def eur_en(cents: int | float | None) -> str:
    return f"EUR {(cents or 0) / 100:,.2f}"


def _r(code: str, fi: str, en: str, **extra) -> dict:
    return {"code": code, "fi": fi, "en": en, **extra}


def _check(code: str, ok: bool, fi: str, en: str, *, scope: str = "declaration", severity: str = "blocking",
           **extra) -> dict:
    return {"code": code, "ok": bool(ok), "severity": severity, "scope": scope, "fi": fi, "en": en, **extra}


def actor_kind(actor: dict | None) -> str:
    if not actor:
        return "system"
    if actor.get("type") == "agent" or str(actor.get("id") or "").startswith("agent:"):
        return "agent"
    if actor.get("type") == "system":
        return "system"
    return "person"


def kind_of(decl: dict, kind: str | None = None) -> str:
    if kind in KINDS:
        return kind
    t = (decl.get("kind") or decl.get("type") or "import").lower()
    return t if t in KINDS else "import"


def _age_days(at: str | None) -> float | None:
    t = parse_iso(at)
    return None if t is None else (now() - t).total_seconds() / 86400


def _rep(decl: dict) -> str:
    return "indirect" if (decl.get("representation_type") or "direct") == "indirect" else "direct"


def _rep_fi(rep: str) -> str:
    return "epäsuora" if rep == "indirect" else "suora"


async def _fresh_user(actor: dict) -> dict:
    """The person's current account (role, extra permissions, disabled) from the database."""
    if not actor.get("id"):
        return actor
    u = await db.raw.users.find_one({"id": actor["id"]}, {"_id": 0, "password_hash": 0})
    return u or {"id": actor["id"], "disabled": True}


def _filer_listed(user: dict, filers: list) -> bool:
    keys = {str(x).strip().lower() for x in filers or [] if str(x).strip()}
    return not keys or str(user.get("id") or "").lower() in keys or str(user.get("email") or "").lower() in keys


async def authorised_filers(settings: dict | None = None, cache: dict | None = None) -> list[dict]:
    """Active internal people who may file: customs.file and (no list, or on the list)."""
    if cache is not None and "filers" in cache:
        return cache["filers"]
    settings = settings or await get_settings()
    out = []
    async for u in db.raw.users.find({"org_type": "internal", "disabled": {"$ne": True}},
                                     {"_id": 0, "id": 1, "name": 1, "email": 1, "role": 1, "extra_permissions": 1}):
        if "customs.file" in permissions_for(u) and _filer_listed(u, settings.get("customs_authorised_filers")):
            out.append({"id": u["id"], "name": u.get("name"), "email": u.get("email"), "role": u.get("role")})
    if cache is not None:
        cache["filers"] = out
    return out


class ControlRefusal(HTTPException):
    """A filing refused by the AEO controls. FastAPI answers {detail, code, fi, en, reasons} (handler below)."""

    def __init__(self, status: int, code: str, fi: str, en: str, reasons: list[dict] | None = None,
                 override_allowed: bool = False):
        super().__init__(status, en)
        self.code, self.fi, self.en, self.reasons, self.override_allowed = code, fi, en, reasons or [], override_allowed

    def body(self) -> dict:
        return {"detail": self.en, "code": self.code, "fi": self.fi, "en": self.en, "reasons": self.reasons,
                "override_allowed": self.override_allowed}


async def control_refusal_handler(_request, exc: ControlRefusal):
    return JSONResponse(status_code=exc.status_code, content=exc.body())


def refusal(status: int, code: str, reasons: list[dict]) -> ControlRefusal:
    fi_list = "; ".join(r["fi"] for r in reasons) or "-"
    en_list = "; ".join(r["en"] for r in reasons) or "-"
    if code == "customs_not_authorised":
        return ControlRefusal(status, code, f"Et voi jättää tulli-ilmoitusta: {fi_list}",
                              f"You are not authorised to file this customs declaration: {en_list}", reasons)
    if code == "four_eyes_required":
        return ControlRefusal(status, code, f"Neljän silmän periaate: toisen henkilön on hyväksyttävä ilmoitus ({fi_list})",
                              f"Four-eyes rule: another person must approve this declaration ({en_list})", reasons)
    return ControlRefusal(status, code, f"Ilmoitusta ei voi jättää ennen kuin tarkistukset ovat kunnossa: {fi_list}",
                          f"The declaration cannot be filed until these controls pass: {en_list}", reasons)


# ---------------------------------------------------------------------------------------------
# Who may file: the actor
# ---------------------------------------------------------------------------------------------
async def _actor_checks(actor: dict | None, decl: dict, settings: dict) -> list[dict]:
    k = actor_kind(actor)
    rep = _rep(decl)
    if k == "agent":
        out = [_check("agent_delegation", bool(settings.get("customs_filing_delegated")),
                      "Johdon pysyvä valtuutus jättää ilmoituksia on kirjattu" if settings.get("customs_filing_delegated")
                      else "Johto ei ole kirjannut Autopilotille pysyvää valtuutusta jättää tulli-ilmoituksia",
                      "The director's standing delegation to lodge declarations is recorded" if settings.get(
                          "customs_filing_delegated") else
                      "The director has not recorded a standing delegation for Autopilot to lodge declarations",
                      scope="actor", legal_point="customs_filing_not_delegated")]
        if rep == "indirect":
            ok = settings.get("customs_delegation_scope") == "direct_and_indirect"
            out.append(_check("agent_delegation_scope", ok,
                              "Valtuutus kattaa epäsuoran edustuksen" if ok else
                              "Valtuutus kattaa vain suoran edustuksen; ilmoitus on epäsuoralla edustuksella",
                              "The delegation covers indirect representation" if ok else
                              "The delegation covers direct representation only; this declaration is indirect",
                              scope="actor", legal_point="customs_indirect_representation"))
        return out
    if k != "person":
        return [_check("filer_unknown", False, "Ilmoituksen jättäjää ei tunnistettu", "The filer is not identified",
                       scope="actor")]
    u = await _fresh_user(actor)
    perms = permissions_for(u)
    name = u.get("name") or u.get("email") or u.get("id")
    active = u.get("org_type") == "internal" and not u.get("disabled")
    has = active and "customs.file" in perms
    out = [_check("filer_permission", has,
                  f"{name}: oikeus jättää tulli-ilmoituksia (customs.file)" if has else
                  f"{name} ei ole aktiivinen sisäinen käyttäjä, jolla on oikeus jättää tulli-ilmoituksia (customs.file)",
                  f"{name} holds customs.file" if has else
                  f"{name} is not an active internal user with permission to file declarations (customs.file)",
                  scope="actor")]
    filers = settings.get("customs_authorised_filers") or []
    listed = _filer_listed(u, filers)
    out.append(_check("filer_authorised", listed,
                      ("Kaikki customs.file-oikeuden haltijat saavat jättää ilmoituksia" if not filers else
                       f"{name} on valtuutettujen ilmoittajien listalla") if listed else
                      f"{name} ei ole yrityksen valtuutettujen tulli-ilmoittajien listalla",
                      ("Everyone with customs.file may file" if not filers else f"{name} is on the authorised filers list")
                      if listed else f"{name} is not on the company's list of authorised customs filers",
                      scope="actor"))
    return out


# ---------------------------------------------------------------------------------------------
# Who may file: the declaration (POA, EORI, KYC, sanctions)
# ---------------------------------------------------------------------------------------------
def _poa_check(company: dict, rep: str, kind: str) -> dict:
    poa = company.get("poa") or {}
    today = now_iso()[:10]
    kfi, ken = KINDS[kind]["fi"], KINDS[kind]["en"]
    if not poa.get("signed_at"):
        return _check("poa", False, "Asiakas ei ole allekirjoittanut tulliasioinnin valtakirjaa",
                      "The customer has not signed a power of attorney for customs representation")
    if poa.get("revoked_at"):
        return _check("poa", False, f"Asiakas perui valtakirjan {str(poa['revoked_at'])[:10]}",
                      f"The customer revoked the power of attorney on {str(poa['revoked_at'])[:10]}")
    if (poa.get("valid_from") or "") > today:
        return _check("poa", False, f"Valtakirja tulee voimaan vasta {poa.get('valid_from')}",
                      f"The power of attorney is valid only from {poa.get('valid_from')}")
    if not poa.get("valid_to") or poa["valid_to"] < today:
        return _check("poa", False, f"Valtakirja päättyi {poa.get('valid_to') or '-'}",
                      f"The power of attorney expired on {poa.get('valid_to') or '-'}")
    if poa.get("type") != rep:
        return _check("poa", False, f"Valtakirja koskee {_rep_fi(poa.get('type') or 'direct')}a edustusta, ilmoitus tehdään "
                                    f"{_rep_fi(rep)}lla edustuksella",
                      f"The power of attorney covers {poa.get('type')} representation; this declaration uses {rep} "
                      f"representation")
    if kind not in (poa.get("scope") or []):
        return _check("poa", False, f"Valtakirja ei kata {kfi}ilmoituksia", f"The power of attorney does not cover {ken} "
                                                                            f"declarations")
    return _check("poa", True, f"Valtakirja: {_rep_fi(rep)} edustus, kattaa {kfi}ilmoitukset, voimassa {poa['valid_to']} asti",
                  f"Power of attorney: {rep} representation, covers {ken} declarations, valid to {poa['valid_to']}",
                  data={"type": poa.get("type"), "scope": poa.get("scope"), "valid_to": poa.get("valid_to"),
                        "document_id": poa.get("document_id")})


async def eori_status(eori: str | None, *, company: dict | None = None, max_age_days: float = 1,
                      refresh: bool = True, allow_sandbox: bool = False) -> dict:
    """The EU EOS answer for `eori`: the stored one while it is younger than `max_age_days`, else checked again live
    (sandbox register for a sandbox test company) and stored back (company.kyc.eori or aeo_eori_checks)."""
    from core.sandbox import sandbox_eori, sandbox_registers
    from services.customs import eori_normalise
    from services.kyc import eori_check, fi_summary, manual_eori_applies

    e = eori_normalise(eori)
    if not e:
        return {"status": "missing", "eori": None}
    own = bool(company and eori_normalise(company.get("eori")) == e)
    if own:
        cached = (company.get("kyc") or {}).get("eori") or None
        if cached and eori_normalise(cached.get("eori")) != e:
            cached = None
    else:
        cached = await db.raw.aeo_eori_checks.find_one({"eori": e}, NO_ID)
    sandbox = bool(own and sandbox_registers(company))
    if cached and cached.get("sandbox") and not (sandbox or allow_sandbox):
        cached = None  # a sandbox register result never authorises a real customer's filing
    age = _age_days((cached or {}).get("checked_at") or (cached or {}).get("at"))
    usable = cached and (cached.get("status") in ("pass", "fail") or manual_eori_applies(cached, e))
    if usable and age is not None and 0 <= age <= max_age_days:
        return {**cached, "rechecked": False, "age_days": age}
    if not refresh:
        return {"status": "stale" if cached else "unchecked", "eori": e, "previous_result": cached,
                "rechecked": False, "age_days": age}
    res = sandbox_eori(company["business_id"]) if sandbox else await eori_check(e)
    if sandbox and eori_normalise(res.get("eori")) != e:
        res = {**res, "status": "fail", "valid": False,
               "summary": f"{e} is not the sandbox register's EORI for this company ({res.get('eori')})"}
    if res.get("status") == "error" and cached:
        # Keep the historical evidence, but an unavailable refresh cannot establish current validity.
        return {**res, "eori": e, "rechecked": True, "previous_result": cached, "age_days": age}
    res = {**res, "eori": res.get("eori") or e}
    if own:
        res["summary_fi"] = fi_summary("eori", res, company)
        sets = {"kyc.eori": res}
        if res.get("status") == "pass":
            sets["eori_validated_at"] = now_iso()
        await db.raw.companies.update_one({"id": company["id"]}, {"$set": sets})
        await audit({"type": "agent", "id": f"agent:{AGENT}", "name": "Customs controls (AEO)"}, "eori_rechecked", "company",
                    company["id"], {"eori": e, "status": res.get("status"), "previous_checked_at": (cached or {}).get(
                        "checked_at"), "source": res.get("source")}, company_id=company["id"])
    else:
        await db.raw.aeo_eori_checks.update_one({"eori": e}, {"$set": {**res, "eori": e}}, upsert=True)
    return {**res, "rechecked": True, "age_days": 0.0}


def _eori_check_row(code: str, role_fi: str, role_en: str, eori: str | None, st: dict, *, missing_blocking: bool,
                    missing_fi: str, missing_en: str) -> dict:
    s = st.get("status")
    when = (st.get("checked_at") or st.get("at") or "")[:10]
    data = {"eori": eori, "status": s, "checked_at": st.get("checked_at") or st.get("at"), "rechecked": st.get("rechecked"),
            "source": st.get("source"), "recheck_error": st.get("recheck_error")}
    if s == "missing":
        return _check(code, False, missing_fi, missing_en, severity="blocking" if missing_blocking else "advisory", data=data)
    if s == "pass":
        extra_fi = " (tarkistettu nyt)" if st.get("rechecked") else f" (tarkistettu {when})"
        extra_en = " (checked now)" if st.get("rechecked") else f" (checked {when})"
        if st.get("recheck_error"):
            extra_fi += "; uusintatarkistus epäonnistui, rekisteri ei vastannut"
            extra_en += "; the re-check failed, the registry did not answer"
        registry_fi = "sandbox-testirekisterissä" if st.get("sandbox") else "EU:n EORI-rekisterissä"
        registry_en = "sandbox test register" if st.get("sandbox") else "EU EORI registry"
        return _check(code, True, f"{role_fi} EORI {eori} on voimassa {registry_fi}{extra_fi}",
                      f"{role_en} EORI {eori} is valid in the {registry_en}{extra_en}", data=data)
    if s == "manual":
        return _check(code, True, f"{role_fi} EORI {eori} on vahvistettu käsin ({when}): {st.get('note') or ''}".strip(),
                      f"{role_en} EORI {eori} was verified manually ({when}): {st.get('note') or ''}".strip(), data=data)
    if s == "fail":
        return _check(code, False, f"{role_fi} EORI {eori} ei ole voimassa EU:n EORI-rekisterissä",
                      f"{role_en} EORI {eori} is not valid in the EU EORI registry", data=data)
    state_fi = {"unchecked": "ei tarkistettu", "stale": "tarkistus vanhentunut", "error": "tarkistus epäonnistui"}.get(s, "tila puuttuu")
    return _check(code, False, f"{role_fi} EORI {eori} ei ole tarkistettu EU:n EORI-rekisteristä: {st.get('summary_fi') or state_fi}",
                  f"{role_en} EORI {eori} could not be checked in the EU EORI registry: {st.get('summary') or s}", data=data)


def _party_screenings(decl: dict) -> list[dict]:
    """Party screenings stored on the declaration (seller screening; s11 customs_screening of every party)."""
    out = []
    for key in ("screening", "party_screening", "customs_screening", "parties_screening"):
        v = decl.get(key)
        if isinstance(v, dict) and v.get("status"):
            out.append({"key": key, **v})
    return out


def _live_gateway(decl: dict, kind: str, sandbox: bool) -> bool:
    if sandbox:
        return False
    mode = (config.env(KINDS[kind]["mode_env"]) or (config.TULLI_MODE if kind != "ens" else "") or "").lower()
    return mode in LIVE_MODES


async def _declaration_checks(decl: dict, kind: str, settings: dict, *, refresh: bool = True) -> list[dict]:
    from core.sandbox import sandbox_registers
    from services.customs import eori_normalise

    company = await db.raw.companies.find_one({"id": decl.get("company_id")}, NO_ID) or {}
    rep = _rep(decl)
    max_age = float(settings.get("customs_rescreen_days") or 0)
    out = [_poa_check(company, rep, kind)]
    kyc = company.get("kyc_status")
    out.append(_check("kyc", kyc == "approved", "Asiakkaan tuntemistarkistus (KYC) on hyväksytty" if kyc == "approved" else
                      f"Asiakkaan tuntemistarkistus (KYC) on tilassa {kyc or 'ei aloitettu'}; se on hyväksyttävä ennen jättämistä",
                      "Customer KYC is approved" if kyc == "approved" else
                      f"Customer KYC is {kyc or 'not started'}; it must be approved before filing"))
    sanc = company.get("sanctions_status")
    out.append(_check("sanctions", sanc == "clear", "Asiakkaan pakotelistaseulonta: ei osumia" if sanc == "clear" else
                      f"Asiakkaan pakotelistaseulonnan tila on {sanc or 'seulomatta'}; pakotevastaava ratkaisee sen ensin",
                      "Customer sanctions screening is clear" if sanc == "clear" else
                      f"Customer sanctions screening is {sanc or 'not screened'}; compliance must clear it first"))
    for scr in _party_screenings(decl):
        ok = scr.get("status") == "clear"
        out.append(_check("party_screening", ok, "Ilmoituksen osapuolet seulottu: ei osumia" if ok else
                          f"Ilmoituksen osapuolten pakoteseulonta: {scr.get('status')}; pakotevastaava ratkaisee sen ensin",
                          "Declaration parties screened: no match" if ok else
                          f"Declaration party screening is {scr.get('status')}; compliance must clear it first",
                          data={"source": scr["key"], "screened_at": scr.get("screened_at")}))
    screened_at = ((company.get("kyc") or {}).get("sanctions") or {}).get("checked_at") or company.get("kyc_checked_at")
    age = _age_days(screened_at)
    if sanc == "clear" and (age is None or age > max_age):
        out.append(_check("sanctions_age", False,
                          f"Asiakkaan pakoteseulonta on {age:.0f} päivää vanha; päivittäinen uudelleenseulonta päivittää sen"
                          if age is not None else "Asiakkaan pakoteseulonnan ajankohta puuttuu",
                          f"The customer's sanctions screening is {age:.0f} days old; the daily re-screening refreshes it"
                          if age is not None else "The customer's sanctions screening has no date",
                          severity="advisory", data={"screened_at": screened_at}))
    # EORI: the declarant (direct: the customer; indirect: Huolitsija) and the representative (Huolitsija)
    sandbox = sandbox_registers(company)
    forwarder_eori = eori_normalise((decl.get("representative") or {}).get("eori")) or eori_normalise(config.COMPANY_EORI)
    if rep == "direct":
        party = decl.get("declarant") or decl.get("importer") or decl.get("exporter") or decl.get("holder") or {}
        d_eori = eori_normalise(party.get("eori")) or eori_normalise(company.get("eori"))
        st = await eori_status(d_eori, company=company, max_age_days=max_age, refresh=refresh)
        out.append(_eori_check_row("declarant_eori", "Ilmoittajan (asiakas)", "Declarant (customer)", d_eori, st,
                                   missing_blocking=True,
                                   missing_fi="Asiakkaan EORI-numero puuttuu; asiakas hakee sen Tullin EORI-palvelusta",
                                   missing_en="The customer's EORI number is missing; they apply for one in Tulli's EORI "
                                              "registration service"))
        st = await eori_status(forwarder_eori, company=None, max_age_days=max_age, refresh=refresh, allow_sandbox=sandbox)
        out.append(_eori_check_row(
            "representative_eori", "Edustajan (Huolitsija)", "Representative (Huolitsija)", forwarder_eori, st,
            missing_blocking=True,
            missing_fi="Huolitsijan oma EORI-numero puuttuu (COMPANY_EORI); se tarvitaan sanomaliikenteessä edustajan "
                       "tunnisteeksi (yhtiön perustamisen jälkeen)",
            missing_en="Huolitsija's own EORI number is not configured (COMPANY_EORI); message exchange needs it as the "
                       "representative's identifier (after incorporation)"))
    else:
        st = await eori_status(forwarder_eori, company=None, max_age_days=max_age, refresh=refresh, allow_sandbox=sandbox)
        out.append(_eori_check_row(
            "declarant_eori", "Ilmoittajan ja edustajan (Huolitsija)", "Declarant and representative (Huolitsija)",
            forwarder_eori, st, missing_blocking=True,
            missing_fi="Epäsuorassa edustuksessa Huolitsija on ilmoittaja, mutta sen EORI-numeroa ei ole asetettu "
                       "(COMPANY_EORI)",
            missing_en="In indirect representation Huolitsija is the declarant, but its EORI number is not configured "
                       "(COMPANY_EORI)"))
    return out


# ---------------------------------------------------------------------------------------------
# Four-eyes thresholds and segregation of duties
# ---------------------------------------------------------------------------------------------
def _item_flag_kinds(it: dict) -> set[str]:
    return {str(f.get("kind")) for f in it.get("flags") or [] if isinstance(f, dict) and f.get("kind")}


def _licence_required(it: dict) -> bool:
    if it.get("licence_required") or it.get("licences_required"):
        return True
    lic = it.get("licences")
    if isinstance(lic, dict):
        return bool(lic.get("required") or lic.get("licence_required"))
    if isinstance(lic, list):
        return any(isinstance(x, dict) and (x.get("required") or x.get("licence_required")) for x in lic)
    return False


def _preference_claimed(it: dict) -> str | None:
    """DE 4/17 preference code with a tariff preference (first digit 2 = GSP, 3 = other preferential arrangement,
    4 = customs union): the code, else None. 1xx = erga omnes third-country duty, no preference."""
    codes = [it.get("preference"), it.get("preference_code"), (it.get("origin_assessment") or {}).get("preference_code"),
             (it.get("origin") or {}).get("preference_code") if isinstance(it.get("origin"), dict) else None]
    for c in codes:
        c = str(c or "").strip()
        if len(c) == 3 and c[0] in "234":
            return c
    return None


def classifiers(decl: dict) -> list[dict]:
    """Whoever classified or confirmed the CN codes: confirmations (person or agent) and edits of the codes."""
    out: dict[str, dict] = {}
    for it in decl.get("items") or []:
        by = it.get("cn_confirmed_by") or {}
        if it.get("cn_confirmed") and by.get("id"):
            out[by["id"]] = {"id": by["id"], "name": by.get("name"), "type": by.get("type") or "user"}
        if it.get("cn_confirmed_by_agent") and not by.get("id"):
            out["agent:customs_agent"] = {"id": "agent:customs_agent", "name": "Autopilot · Customs", "type": "agent"}
    for e in decl.get("edits") or []:
        by = e.get("by") or {}
        if by.get("id") and any(c.get("field") in CLASSIFICATION_FIELDS for c in e.get("changes") or []):
            out.setdefault(by["id"], {"id": by["id"], "name": by.get("name"), "type": by.get("type") or "user"})
    return list(out.values())


async def _filed_before(decl: dict) -> int:
    """Declarations of any kind already filed for this customer (demo data does not count)."""
    cid = decl.get("company_id")
    if not cid:
        return 0
    n = 0
    q = {"company_id": cid, "id": {"$ne": decl.get("id")}, "demo": {"$ne": True},
         "$or": [{"status": {"$in": FILED}}, {"mrn": {"$exists": True, "$nin": [None, ""]}},
                 {"history.to": "submitted"}]}
    for meta in KINDS.values():
        n += await db.raw[meta["collection"]].count_documents(q, limit=1)
        if n:
            break
    return n


def _rejected_before(decl: dict) -> bool:
    if decl.get("status") == "rejected" or any(h.get("to") == "rejected" for h in decl.get("history") or []):
        return True
    return any(m.get("type") == "rejected" for m in (decl.get("submission") or {}).get("messages") or [])


async def four_eyes_requirement(decl: dict, kind: str, settings: dict) -> dict:
    t = decl.get("totals") or {}
    duty = int(t.get("duty_cents") or 0)
    vat = int(t.get("vat_cents") or 0)
    value = int(t.get("customs_value_cents") or t.get("statistical_value_cents") or t.get("invoice_value_cents") or 0)
    duty_thr = int(round(float(settings.get("customs_four_eyes_duty_eur") or 0) * 100))
    value_thr = int(round(float(settings.get("customs_four_eyes_value_eur") or 0) * 100))
    reasons = []
    if duty + vat >= duty_thr:
        reasons.append(_r("aeo_four_eyes_duty_vat", f"Tulli ja tuonnin ALV {eur_fi(duty + vat)} ylittää neljän silmän rajan "
                                                    f"{eur_fi(duty_thr)}",
                          f"Duty and import VAT {eur_en(duty + vat)} reach the four-eyes threshold {eur_en(duty_thr)}"))
    if value >= value_thr:
        reasons.append(_r("aeo_four_eyes_value", f"Tullausarvo {eur_fi(value)} ylittää neljän silmän rajan {eur_fi(value_thr)}",
                          f"Customs value {eur_en(value)} reaches the four-eyes threshold {eur_en(value_thr)}"))
    items = decl.get("items") or []
    seqs = lambda pred: [str(it.get("seq")) for it in items if pred(it)]  # noqa: E731
    du = seqs(lambda it: _item_flag_kinds(it) & DUAL_USE_KINDS or (it.get("dual_use") or {}).get("controlled"))
    if du:
        reasons.append(_r("aeo_four_eyes_dual_use", f"Kaksikäyttötuotemerkintä nimikkeillä {', '.join(du)}",
                          f"Dual-use flag on item(s) {', '.join(du)}"))
    lic = seqs(lambda it: _item_flag_kinds(it) & LICENCE_KINDS or _licence_required(it))
    if lic or decl.get("licences_required"):
        reasons.append(_r("aeo_four_eyes_licence", f"Lupa- tai todistusvaatimus nimikkeillä {', '.join(lic) or '-'}",
                          f"Licence or certificate requirement on item(s) {', '.join(lic) or '-'}"))
    sanc = seqs(lambda it: _item_flag_kinds(it) & SANCTIONS_KINDS)
    party_hits = [s for s in _party_screenings(decl) if s.get("status") != "clear" or s.get("hits")]
    company = await db.raw.companies.find_one({"id": decl.get("company_id")}, {"_id": 0, "kyc.sanctions.hits": 1}) or {}
    cleared = bool(((company.get("kyc") or {}).get("sanctions") or {}).get("hits"))  # false positives cleared earlier
    if sanc or party_hits or cleared:
        reasons.append(_r("aeo_four_eyes_sanctions",
                          "Pakotemerkintä: " + ", ".join(([f"nimikkeet {', '.join(sanc)}"] if sanc else []) +
                                                        (["osapuolten seulonnassa osumia"] if party_hits else []) +
                                                        (["asiakkaan seulonnassa vääriä hälytyksiä"] if cleared else [])),
                          "Sanctions flag: " + ", ".join(([f"items {', '.join(sanc)}"] if sanc else []) +
                                                        (["party screening has hits"] if party_hits else []) +
                                                        (["the customer's screening had cleared false positives"]
                                                         if cleared else []))))
    td = seqs(lambda it: _item_flag_kinds(it) & TRADE_DEFENCE_KINDS)
    if td:
        reasons.append(_r("aeo_four_eyes_trade_defence", f"Kaupan suojatoimenpide (polkumyynti- tai tasoitustulli) "
                                                         f"nimikkeillä {', '.join(td)}",
                          f"Trade-defence measure (anti-dumping or countervailing duty) on item(s) {', '.join(td)}"))
    pref = [(str(it.get("seq")), _preference_claimed(it)) for it in items if _preference_claimed(it)]
    if pref:
        reasons.append(_r("aeo_four_eyes_preference", "Etuuskohtelua haetaan: " + ", ".join(f"nimike {s} ({c})" for s, c in pref),
                          "Tariff preference claimed: " + ", ".join(f"item {s} ({c})" for s, c in pref)))
    if not await _filed_before(decl):
        reasons.append(_r("aeo_four_eyes_first_declaration", "Asiakkaan ensimmäinen tulli-ilmoitus",
                          "First customs declaration filed for this customer"))
    if _rejected_before(decl):
        reasons.append(_r("aeo_four_eyes_refiling", "Ilmoitus jätetään uudelleen Tullin hylkäyksen jälkeen",
                          "Filing again after Tulli rejected the declaration"))
    return {"required": bool(reasons), "reasons": reasons,
            "amounts": {"duty_cents": duty, "vat_cents": vat, "duty_vat_cents": duty + vat, "customs_value_cents": value},
            "thresholds": {"duty_vat_cents": duty_thr, "customs_value_cents": value_thr}}


# ---------------------------------------------------------------------------------------------
# The controls
# ---------------------------------------------------------------------------------------------
async def _approval_for(decl: dict) -> dict | None:
    if not decl.get("approval_id"):
        return None
    return await db.raw.approvals.find_one({"id": decl["approval_id"]}, NO_ID)


async def filing_controls(decl: dict, actor: dict | None, *, kind: str = "import", record: bool = True,
                          stage: str = "check", approval: dict | None = None, refresh: bool = True,
                          settings: dict | None = None) -> dict:
    """May `actor` (a person or an agent) file `decl`, and does it need four eyes?

    -> {may_file, four_eyes{required, reasons[], amounts, thresholds, preparers[], classifiers[], excluded_ids[],
        actor_excluded}, reasons[{code, fi, en}], checks[{code, ok, severity, scope, fi, en, data?, legal_point?}],
        kind, stage, actor, declaration_id, lrn, at, decision_id}
    `reasons` are the failed blocking checks. An agent's delegation checks carry `legal_point` (s2 legal points: the
    agent's gate puts them as legal_point, not as extra reasons). record=True writes the audit row (and, for a
    person, the decision-log row through decide(rule_check=True)); refresh=False never calls a register."""
    kind = kind_of(decl, kind)
    settings = settings or await get_settings()
    checks = await _actor_checks(actor, decl, settings) + await _declaration_checks(decl, kind, settings, refresh=refresh)
    failed = [c for c in checks if not c["ok"] and c["severity"] == "blocking"]
    reasons = [_r(f"aeo_{c['code']}", c["fi"], c["en"], **({"legal_point": c["legal_point"]} if c.get("legal_point") else {}),
                  scope=c["scope"]) for c in failed]
    fe = await four_eyes_requirement(decl, kind, settings)
    if approval is None and stage != "request":
        approval = await _approval_for(decl)
    if approval and (approval.get("requested_by") or {}).get("id") and stage != "request":
        preparers = [{k: approval["requested_by"].get(k) for k in ("id", "name", "type")}]
    else:
        ref = actor_ref(actor)
        preparers = [{k: ref.get(k) for k in ("id", "name", "type")}] if ref.get("id") else []
    cls = classifiers(decl)
    excluded = sorted({p["id"] for p in preparers if p.get("id")} | {c["id"] for c in cls})
    actor_id = (actor or {}).get("id")
    seg_reasons = []
    if fe["required"] and any(c["type"] == "agent" for c in cls):
        seg_reasons.append(_r("aeo_segregation_classifier", "Autopilotti vahvisti CN-koodit, joten se ei voi yksin hyväksyä "
                                                            "ilmoitusta neljän silmän rajan ylittyessä",
                              "Autopilot confirmed the CN codes, so it cannot be the sole approver above the four-eyes "
                              "threshold"))
    if fe["required"] and actor_kind(actor) == "person" and actor_id in {c["id"] for c in cls}:
        seg_reasons.append(_r("aeo_segregation_classifier", "Luokittelit tai vahvistit CN-koodit itse, joten et voi yksin "
                                                            "hyväksyä ilmoitusta neljän silmän rajan ylittyessä",
                              "You classified or confirmed the CN codes, so you cannot be the sole approver above the "
                              "four-eyes threshold"))
    if fe["required"] and actor_kind(actor) == "person" and actor_id in {p.get("id") for p in preparers} \
            and stage != "request":
        seg_reasons.append(_r("aeo_segregation_preparer", "Pyysit ilmoituksen jättämistä itse, joten toisen henkilön on "
                                                          "hyväksyttävä se", "You asked for this filing, so another "
                                                                             "person must approve it"))
    fe.update(preparers=preparers, classifiers=cls, excluded_ids=excluded,
              actor_excluded=bool(actor_id and actor_id in excluded) or (fe["required"] and actor_kind(actor) == "agent"),
              segregation_reasons=seg_reasons)
    out = {"declaration_id": decl.get("id"), "lrn": decl.get("lrn"), "kind": kind, "stage": stage, "at": now_iso(),
           "actor": actor_ref(actor), "actor_kind": actor_kind(actor), "may_file": not failed, "reasons": reasons,
           "checks": checks, "four_eyes": fe, "decision_id": None}
    if record:
        await record_controls(decl, actor, out)
    return out


def summary(ctl: dict) -> dict:
    """Short form for decision inputs, approval data and audit rows."""
    fe = ctl.get("four_eyes") or {}
    return {"may_file": ctl.get("may_file"), "stage": ctl.get("stage"), "kind": ctl.get("kind"),
            "reasons": [r["code"] for r in ctl.get("reasons") or []],
            "four_eyes_required": fe.get("required"), "four_eyes_reasons": [r["code"] for r in fe.get("reasons") or []],
            "amounts": fe.get("amounts"), "thresholds": fe.get("thresholds"),
            "excluded_ids": fe.get("excluded_ids"), "classifiers": [c.get("id") for c in fe.get("classifiers") or []]}


async def record_controls(decl: dict, actor: dict | None, ctl: dict):
    entity = KINDS[ctl["kind"]]["entity"]
    s = summary(ctl)
    await audit(actor, "customs_aeo_controls", entity, decl.get("id"), {"lrn": decl.get("lrn"), **s},
                company_id=decl.get("company_id"))
    if actor_kind(actor) != "person":
        return  # the agent's own decide() row (gate_filing / export / transit / ENS filing) carries these reasons
    from core.agent_runtime import decide
    fi_title = f"Tullivalvonta: {decl.get('lrn') or decl.get('id')} ({ctl['stage']})"
    d = await decide(AGENT, f"filing_controls_{ctl['stage']}", subject=(entity, decl.get("id")),
                     subject_label=decl.get("lrn"), company_id=decl.get("company_id"),
                     inputs={"actor": actor_ref(actor), "kind": ctl["kind"], "amounts": s["amounts"],
                             "thresholds": s["thresholds"], "checks": [{k: c.get(k) for k in ("code", "ok", "severity")}
                                                                       for c in ctl["checks"]]},
                     decision={"may_file": ctl["may_file"], "four_eyes": {"required": s["four_eyes_required"],
                                                                          "reasons": s["four_eyes_reasons"],
                                                                          "excluded_ids": s["excluded_ids"]}},
                     confidence=None, model=None, extra_reasons=[{k: r[k] for k in ("code", "fi", "en")}
                                                                 for r in ctl["reasons"]],
                     create_approval=False, rule_check=True, title=f"Customs controls: {decl.get('lrn')} ({ctl['stage']})",
                     title_fi=fi_title)
    ctl["decision_id"] = d.id


async def record_refusal(decl: dict, actor: dict | None, code: str, reasons: list[dict], *, stage: str,
                         kind: str = "import", approval_id: str | None = None):
    await audit(actor, "customs_aeo_refused", KINDS[kind_of(decl, kind)]["entity"], decl.get("id"),
                {"lrn": decl.get("lrn"), "code": code, "stage": stage, "approval_id": approval_id,
                 "reasons": [{k: r.get(k) for k in ("code", "fi", "en")} for r in reasons]},
                company_id=decl.get("company_id"))


def agent_reasons(ctl: dict) -> list[dict]:
    """Stop reasons for an agent's decide(): failed controls that are not s2 legal points, and the four-eyes reasons."""
    out = [{k: r[k] for k in ("code", "fi", "en")} for r in ctl.get("reasons") or [] if not r.get("legal_point")]
    fe = ctl.get("four_eyes") or {}
    if fe.get("required"):
        out += [{k: r[k] for k in ("code", "fi", "en")} for r in fe.get("reasons") or []]
        out += [{k: r[k] for k in ("code", "fi", "en")} for r in fe.get("segregation_reasons") or []]
    return out


def raise_if_refused(ctl: dict, decl: dict | None = None):
    """For a person's action: 403 customs_not_authorised (the person may not file) or 409 customs_controls_failed."""
    if ctl["actor_kind"] == "agent":
        return
    actor_r = [r for r in ctl["reasons"] if r.get("scope") == "actor"]
    if actor_r:
        raise refusal(403, "customs_not_authorised", actor_r)
    if ctl["reasons"]:
        raise refusal(409, "customs_controls_failed", ctl["reasons"])


# ---------------------------------------------------------------------------------------------
# Approvals (core.approvals hook) and filing (services.customs hooks)
# ---------------------------------------------------------------------------------------------
async def approval_controls(a: dict, user: dict, *, record: bool = False, cache: dict | None = None) -> dict | None:
    """For a customs_filing approval card: may `user` approve it, and the four-eyes state for them.
    -> {refusal: {status, detail, code, fi, en, reasons, override_allowed} | None,
        four_eyes: {required, excluded, colleagues, colleagues_any, reasons}} ; None when the card has no declaration."""
    kind = (a.get("data") or {}).get("kind") or ENTITIES.get(a.get("ref_type") or "", "import")
    decl = await db.raw[KINDS[kind]["collection"]].find_one({"id": a.get("ref_id")}, NO_ID)
    if not decl:
        return None
    cache = cache if cache is not None else {}
    if "settings" not in cache:
        cache["settings"] = await get_settings()
    ctl = await filing_controls(decl, user, kind=kind, record=record, stage="approve", approval=a, refresh=record,
                                settings=cache["settings"])
    fe = ctl["four_eyes"]
    filers = await authorised_filers(cache["settings"], cache)
    uid = user.get("id")
    colleagues_any = [u for u in filers if u["id"] != uid]
    colleagues = [u for u in colleagues_any if u["id"] not in fe["excluded_ids"]]
    ref = None
    actor_r = [r for r in ctl["reasons"] if r.get("scope") == "actor"]
    if actor_r or ctl["reasons"]:
        err = refusal(403, "customs_not_authorised", actor_r) if actor_r else refusal(409, "customs_controls_failed",
                                                                                       ctl["reasons"])
        ref = {"status": err.status_code, **err.body()}
        if record:
            await record_refusal(decl, user, err.code, err.reasons, stage="approve", kind=kind, approval_id=a.get("id"))
    return {"refusal": ref, "four_eyes": {"required": fe["required"], "excluded": bool(uid and uid in fe["excluded_ids"]),
                                          "colleagues": len(colleagues), "colleagues_any": len(colleagues_any),
                                          "reasons": fe["reasons"], "excluded_ids": fe["excluded_ids"]}}


async def approval_refusal(a: dict, decl: dict, actor: dict | None, *, kind: str = "import") -> ControlRefusal | None:
    """Defence in depth when a filing approval is decided (services.customs._on_approval): the approver may file and
    the four-eyes rule holds (the approver is not the preparer or a classifier, or a single-user override is on
    record). Returns the refusal (and audits it), or None."""
    ctl = await filing_controls(decl, actor, kind=kind, record=False, stage="approved", approval=a)
    err = None
    actor_r = [r for r in ctl["reasons"] if r.get("scope") == "actor"]
    if actor_r:
        err = refusal(403, "customs_not_authorised", actor_r)
    elif ctl["reasons"]:
        err = refusal(409, "customs_controls_failed", ctl["reasons"])
    else:
        fe = ctl["four_eyes"]
        if fe["required"] and fe["actor_excluded"] and not a.get("four_eyes_override"):
            err = refusal(409, "four_eyes_required", fe["reasons"] + fe["segregation_reasons"])
    if err:
        await record_refusal(decl, actor, err.code, err.reasons, stage="approve", kind=kind, approval_id=a.get("id"))
    return err


async def enforce_filing(decl: dict, actor: dict | None, *, kind: str = "import") -> dict:
    """Before anything is sent to Tulli (services.customs.file_declaration): the filer may file (403
    customs_not_authorised), the declaration passes the controls (409 customs_controls_failed) and, when four eyes
    are required, the approval on record was given by someone other than the preparer and the classifiers, or with
    the audited single-user override (409 four_eyes_required)."""
    if actor_kind(actor) == "agent":
        # The gate already reserved the spend. Check switches again without charging that decision twice;
        # filing_controls below rechecks delegation and customer authority at the submission boundary.
        settings = await get_settings()
        if settings.get("autopilot_halted") or not settings.get("autopilot_enabled"):
            reasons = [_r("halted", "Autopilotti on pysäytetty tai pois päältä", "Autopilot is halted or off")]
            await record_refusal(decl, actor, "customs_controls_failed", reasons, stage="file", kind=kind)
            raise refusal(409, "customs_controls_failed", reasons)
    a = await _approval_for(decl)
    ctl = await filing_controls(decl, actor, kind=kind, record=True, stage="file", approval=a)
    err = None
    actor_r = [r for r in ctl["reasons"] if r.get("scope") == "actor"]
    if actor_r:
        err = refusal(403, "customs_not_authorised", actor_r)
    elif ctl["reasons"]:
        err = refusal(409, "customs_controls_failed", ctl["reasons"])
    else:
        fe = ctl["four_eyes"]
        approver = ((a or {}).get("decided_by") or {}).get("id") or ""
        four_ok = bool(a and a.get("status") == "approved" and (
            a.get("four_eyes_override") or (approver and approver not in fe["excluded_ids"]
                                            and not approver.startswith("agent:"))))
        if fe["required"] and not four_ok:
            err = refusal(409, "four_eyes_required", fe["reasons"] + fe["segregation_reasons"])
    if err:
        await record_refusal(decl, actor, err.code, err.reasons, stage="file", kind=kind,
                             approval_id=(a or {}).get("id"))
        raise err
    return ctl


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------
SETTING_KEYS = ("customs_four_eyes_duty_eur", "customs_four_eyes_value_eur", "customs_authorised_filers",
                "customs_rescreen_days", "record_retention_years", "customs_screening_required_lists")


async def validate_controls_settings(body: dict) -> dict:
    """Checked, normalised changes (400 with a Finnish and English reason)."""
    def bad(fi: str, en: str):
        raise ControlRefusal(400, "invalid_setting", fi, en)
    unknown = set(body) - set(SETTING_KEYS)
    if unknown:
        bad("Tuntematon asetus: " + ", ".join(sorted(unknown)), "Unknown setting: " + ", ".join(sorted(unknown)))
    out = {}
    for k in ("customs_four_eyes_duty_eur", "customs_four_eyes_value_eur"):
        if k in body:
            v = body[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 10_000_000:
                bad("Raja on euromäärä 0–10 000 000", "The threshold is an amount in euros from 0 to 10 000 000")
            out[k] = int(v) if float(v).is_integer() else round(float(v), 2)
    if "customs_rescreen_days" in body:
        v = body["customs_rescreen_days"]
        if type(v) is not int or not 0 <= v <= 30:
            bad("Uusintatarkistuksen väli on 0–30 päivää", "The re-check interval is 0 to 30 days")
        out["customs_rescreen_days"] = v
    if "record_retention_years" in body:
        v = body["record_retention_years"]
        if type(v) is not int or not 6 <= v <= 30:
            bad("Säilytysaika on 6–30 vuotta (kirjanpitolaki 2:10 §: vähintään 6 vuotta)",
                "Retention is 6 to 30 years (Accounting Act 2:10: at least 6 years)")
        out["record_retention_years"] = v
    if "customs_authorised_filers" in body:
        v = body["customs_authorised_filers"]
        if not isinstance(v, list) or len(v) > 200:
            bad("Valtuutetut ilmoittajat annetaan listana", "Authorised filers are a list of users")
        ids = []
        for x in v:
            key = str(x or "").strip()
            u = await db.raw.users.find_one({"$or": [{"id": key}, {"email": key.lower()}]},
                                            {"_id": 0, "id": 1, "name": 1, "org_type": 1, "disabled": 1, "role": 1,
                                             "extra_permissions": 1})
            if not u or u.get("org_type") != "internal" or u.get("disabled") or "customs.file" not in permissions_for(u):
                bad(f"{key} ei ole aktiivinen sisäinen käyttäjä, jolla on customs.file-oikeus",
                    f"{key} is not an active internal user with customs.file")
            if u["id"] not in ids:
                ids.append(u["id"])
        out["customs_authorised_filers"] = ids
    if "customs_screening_required_lists" in body:
        from services.screening import LIST_SOURCES
        v = body["customs_screening_required_lists"]
        if not isinstance(v, list) or not all(isinstance(x, str) and x in LIST_SOURCES for x in v) or "eu" not in v:
            bad("Pakollisten pakotelistojen on oltava listoista " + ", ".join(LIST_SOURCES) + " ja sisällettävä EU",
                "Required sanctions lists must be among " + ", ".join(LIST_SOURCES) + " and include eu")
        out["customs_screening_required_lists"] = [k for k in LIST_SOURCES if k in v]
    return out


# ---------------------------------------------------------------------------------------------
# AEO audit export (CSV) and compliance KPIs
# ---------------------------------------------------------------------------------------------
def period(date_from: str | None, date_to: str | None, *, default_days: int = 90) -> tuple[str, str, str, str]:
    """-> (from_date, to_date, from_bound, to_bound_exclusive) for ISO timestamps (string comparison)."""
    def parse(s: str, name: str) -> date:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            raise ControlRefusal(400, "invalid_date", f"{name}: anna päivä muodossa VVVV-KK-PP",
                                 f"{name}: use a date YYYY-MM-DD")
    to_d = parse(date_to, "to") if date_to else now().date()
    from_d = parse(date_from, "from") if date_from else to_d - timedelta(days=default_days)
    if from_d > to_d:
        raise ControlRefusal(400, "invalid_period", "Alkupäivä on loppupäivän jälkeen", "The start date is after the end date")
    return from_d.isoformat(), to_d.isoformat(), from_d.isoformat(), (to_d + timedelta(days=1)).isoformat()


CSV_COLUMNS = ["at", "source", "kind", "declaration_id", "lrn", "mrn", "company_id", "company_name", "action",
               "status_from", "status_to", "actor_type", "actor_id", "actor_name", "outcome", "reasons", "approval_id",
               "note", "details"]


# Authorisation changes an AEO auditor asks for next to the declarations (POA, EORI, delegation, kill switch).
AUTHORISATION_ACTIONS = ["poa_signed", "poa_revoked", "eori_rechecked", "eori_verified_manually", "eori_manual_revoked",
                         "autopilot_killed", "autopilot_resumed", "autonomy_switch_changed", "kyc_run"]


def _codes(reasons) -> str:
    out = []
    for x in reasons or []:
        out.append(x if isinstance(x, str) else str((x or {}).get("code") or ""))
    return "; ".join(c for c in out if c)


def _actor_cols(a: dict | None) -> dict:
    a = a or {}
    return {"actor_type": a.get("type") or ("agent" if str(a.get("id") or "").startswith("agent:") else "user"),
            "actor_id": a.get("id"), "actor_name": a.get("name")}


async def _declarations_index(ids: set[str]) -> dict[str, dict]:
    out = {}
    for kind, meta in KINDS.items():
        async for d in db.raw[meta["collection"]].find({"id": {"$in": list(ids)}},
                                                       {"_id": 0, "id": 1, "lrn": 1, "mrn": 1, "company_id": 1,
                                                        "company_name": 1}):
            out[d["id"]] = {**d, "kind": kind}
    return out


async def audit_rows(date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Every customs action in the period, oldest first: declarations of all kinds (status changes, edits, drafts),
    approvals (requested, decided, overrides), filings, Tulli's responses, AEO control decisions and refusals, and the
    agents' decisions."""
    settings = await get_settings()
    f, t, lo, hi = period(date_from, date_to, default_days=366 * int(settings.get("record_retention_years") or 6))
    rng = {"$gte": lo, "$lt": hi}
    entities = list(ENTITIES)
    rows: list[dict] = []
    async for r in db.raw.audit_log.find({"at": rng, "$or": [{"entity": {"$in": entities}},
                                                             {"action": {"$regex": "^(customs_|autopilot_customs)"}},
                                                             {"action": {"$in": AUTHORISATION_ACTIONS}}]},
                                         NO_ID):
        data = r.get("data") or {}
        status_to = r["action"][len("customs_status_"):] if r["action"].startswith("customs_status_") else None
        rows.append({"at": r["at"], "source": "audit", "kind": ENTITIES.get(r.get("entity") or ""),
                     "declaration_id": r.get("entity_id") if r.get("entity") in ENTITIES else None,
                     "lrn": data.get("lrn"), "company_id": r.get("company_id"), "action": r["action"],
                     "status_to": status_to, **_actor_cols(r.get("actor")),
                     "outcome": data.get("code") or (("may_file" if data.get("may_file") else "refused")
                                                     if "may_file" in data else None),
                     "reasons": _codes(data.get("reasons")),
                     "approval_id": data.get("approval_id"), "note": data.get("note"),
                     "details": json.dumps(data, ensure_ascii=False, default=str, sort_keys=True)[:4000]})
    async for d in db.raw.agent_decisions.find({"at": rng, "subject_type": {"$in": entities}}, NO_ID):
        rows.append({"at": d["at"], "source": "decision", "kind": ENTITIES.get(d.get("subject_type") or ""),
                     "declaration_id": d.get("subject_id"), "lrn": d.get("subject_label"), "company_id": d.get("company_id"),
                     "action": f"{d.get('agent')}.{d.get('action')}", "actor_type": "agent", "actor_id": f"agent:{d.get('agent')}",
                     "actor_name": d.get("agent_label_en"), "outcome": d.get("outcome"),
                     "reasons": "; ".join(r.get("code", "") for r in d.get("reasons") or []),
                     "approval_id": d.get("approval_id"), "note": d.get("title"),
                     "details": json.dumps({"decision": d.get("decision"), "legal_point": d.get("legal_point"),
                                            "autopilot_enabled": d.get("autopilot_enabled"), "rule_check": d.get("rule_check"),
                                            "reasons": d.get("reasons")}, ensure_ascii=False, default=str)[:4000]})
    async for a in db.raw.approvals.find({"ref_type": {"$in": entities},
                                          "$or": [{"created_at": rng}, {"decided_at": rng}]}, NO_ID):
        base = {"source": "approval", "kind": ENTITIES.get(a.get("ref_type") or ""), "declaration_id": a.get("ref_id"),
                "company_id": a.get("company_id"), "approval_id": a["id"]}
        if lo <= (a.get("created_at") or "") < hi:
            rows.append({**base, "at": a["created_at"], "action": f"approval_requested:{a['kind']}",
                         **_actor_cols(a.get("requested_by")), "outcome": "pending",
                         "reasons": "; ".join(r.get("code", "") for r in a.get("reasons") or []),
                         "note": a.get("title"), "details": json.dumps({"data": a.get("data")}, default=str)[:4000]})
        if a.get("decided_at") and lo <= a["decided_at"] < hi:
            fo = a.get("four_eyes_override")
            rows.append({**base, "at": a["decided_at"], "action": f"approval_{a.get('status')}:{a['kind']}",
                         **_actor_cols(a.get("decided_by")), "outcome": a.get("status"),
                         "reasons": "four_eyes_override" if fo else "", "note": a.get("decision_note"),
                         "details": json.dumps({"four_eyes_override": fo, "void_reason": a.get("void_reason")},
                                               default=str)[:4000]})
    for kind, meta in KINDS.items():
        async for d in db.raw[meta["collection"]].find({"submission.messages.at": rng},
                                                       {"_id": 0, "id": 1, "lrn": 1, "mrn": 1, "company_id": 1,
                                                        "company_name": 1, "submission.messages": 1, "submission.channel": 1}):
            for m in (d.get("submission") or {}).get("messages") or []:
                if not (lo <= (m.get("at") or "") < hi):
                    continue
                rows.append({"at": m["at"], "source": "message", "kind": kind, "declaration_id": d["id"], "lrn": d.get("lrn"),
                             "mrn": m.get("mrn") or d.get("mrn"), "company_id": d.get("company_id"),
                             "company_name": d.get("company_name"), "action": f"tulli_{m.get('direction')}:{m.get('type')}",
                             **_actor_cols(m.get("recorded_by") or {"type": "system", "id": m.get("channel"),
                                                                    "name": f"Tulli ({m.get('channel')})"}),
                             "outcome": m.get("type"), "note": m.get("note"),
                             "details": json.dumps({k: m.get(k) for k in ("channel", "sandbox", "template_version",
                                                                         "document_id", "id")}, default=str)})
    if "customs_messages" in await db.raw.list_collection_names():
        async for m in db.raw.customs_messages.find({"at": rng}, {"_id": 0, "xml": 0}):
            rows.append({"at": m["at"], "source": "message", "kind": m.get("kind"),
                         "declaration_id": m.get("declaration_id"), "lrn": m.get("lrn"), "mrn": m.get("mrn"),
                         "company_id": m.get("company_id"), "action": f"message_{m.get('direction')}:{m.get('type')}",
                         "actor_type": "system", "actor_id": m.get("channel"), "actor_name": m.get("system"),
                         "outcome": (m.get("validation") or {}).get("valid") if isinstance(m.get("validation"), dict) else None,
                         "details": json.dumps({k: v for k, v in m.items() if k not in ("xml",)}, default=str)[:4000]})
    idx = await _declarations_index({r["declaration_id"] for r in rows if r.get("declaration_id")})
    for r in rows:
        d = idx.get(r.get("declaration_id") or "")
        if d:
            r["lrn"] = r.get("lrn") or d.get("lrn")
            r["mrn"] = r.get("mrn") or d.get("mrn")
            r["kind"] = r.get("kind") or d["kind"]
            r["company_id"] = r.get("company_id") or d.get("company_id")
            r["company_name"] = r.get("company_name") or d.get("company_name")
    rows.sort(key=lambda r: (r["at"], r["source"], r.get("action") or ""))
    return rows


def rows_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in CSV_COLUMNS})
    return "﻿" + buf.getvalue()  # BOM: Excel opens UTF-8 (ä, ö) correctly


def _first(history: list[dict], to: str) -> str | None:
    return next((h.get("at") for h in history if h.get("to") == to), None)


async def kpis(date_from: str | None = None, date_to: str | None = None) -> dict:
    """Customs compliance KPIs for the period: filings, rejection rate, amendments, time to release, four-eyes
    requirements and overrides, Autopilot filings and control refusals (all declaration kinds)."""
    f, t, lo, hi = period(date_from, date_to)
    rng = {"$gte": lo, "$lt": hi}
    inside = lambda at: bool(at) and lo <= at < hi  # noqa: E731
    by_kind = {}
    all_hours: list[float] = []
    for kind, meta in KINDS.items():
        filed = rejected = amended = released = invalidated = 0
        hours: list[float] = []
        async for d in db.raw[meta["collection"]].find({"history.at": rng, "demo": {"$ne": True}},
                                                       {"_id": 0, "id": 1, "history": 1, "submission.submitted_at": 1,
                                                        "released_at": 1}):
            hist = d.get("history") or []
            sub_at = _first(hist, "submitted") or (d.get("submission") or {}).get("submitted_at") or _first(hist, "accepted")
            if inside(sub_at):
                filed += 1
            for h in hist:
                if not inside(h.get("at")):
                    continue
                if h.get("to") == "rejected":
                    rejected += 1
                if h.get("from") == "rejected" and h.get("to") in ("draft", "needs_info"):
                    amended += 1
                if h.get("from") in ("accepted", "under_control") and h.get("to") == "invalidated":
                    invalidated += 1
                if h.get("to") == "released":
                    released += 1
                    st, rt = parse_iso(sub_at), parse_iso(h.get("at"))
                    if st and rt and rt >= st:
                        hours.append((rt - st).total_seconds() / 3600)
        all_hours += hours
        by_kind[kind] = {"filed": filed, "rejected": rejected,
                         "rejection_rate_pct": round(rejected / filed * 100, 1) if filed else None,
                         "amendments": amended + invalidated, "amended_after_rejection": amended,
                         "invalidated_after_acceptance": invalidated, "released": released,
                         "time_to_release_hours": _stats(hours)}
    voided = await db.raw.audit_log.count_documents({"at": rng, "action": "customs_approval_voided"})
    cards = await db.raw.approvals.count_documents({"kind": "customs_filing", "created_at": rng})
    fe_required = await db.raw.approvals.count_documents({"kind": "customs_filing", "created_at": rng,
                                                          "data.aeo.four_eyes_required": True})
    overrides = []
    async for a in db.raw.approvals.find({"kind": "customs_filing", "decided_at": rng,
                                          "four_eyes_override": {"$ne": None}}, NO_ID).sort("decided_at", 1):
        overrides.append({"approval_id": a["id"], "declaration_id": a.get("ref_id"), "lrn": (a.get("data") or {}).get("lrn"),
                          "at": a.get("decided_at"), "by": (a.get("four_eyes_override") or {}).get("by"),
                          "note": (a.get("four_eyes_override") or {}).get("note")})
    decided = await db.raw.approvals.count_documents({"kind": "customs_filing", "decided_at": rng, "status": "approved"})
    auto = await db.raw.agent_decisions.count_documents({"at": rng, "agent": "customs_agent", "action": "file_declaration",
                                                         "outcome": "auto"})
    queued = await db.raw.agent_decisions.count_documents({"at": rng, "agent": "customs_agent",
                                                           "action": "file_declaration", "outcome": "owner_queue"})
    refusals = {}
    async for r in db.raw.audit_log.aggregate([{"$match": {"at": rng, "action": "customs_aeo_refused"}},
                                               {"$group": {"_id": "$data.code", "n": {"$sum": 1}}}]):
        refusals[r["_id"] or "unknown"] = r["n"]
    tot = {k: sum(v[k] for v in by_kind.values()) for k in ("filed", "rejected", "amendments", "released",
                                                             "amended_after_rejection", "invalidated_after_acceptance")}
    tot["rejection_rate_pct"] = round(tot["rejected"] / tot["filed"] * 100, 1) if tot["filed"] else None
    tot["time_to_release_hours"] = _stats(all_hours)
    tot["changed_after_approval_request"] = voided
    return {"period": {"from": f, "to": t}, "totals": tot, "by_kind": by_kind,
            "four_eyes": {"filing_cards": cards, "required": fe_required, "approved": decided,
                          "overrides": len(overrides), "override_rate_pct": round(len(overrides) / decided * 100, 1)
                          if decided else None, "override_list": overrides},
            "autopilot": {"auto_filed": auto, "owner_queue": queued},
            "control_refusals": refusals,
            "definitions": KPI_DEFINITIONS}


def _stats(hours: list[float]) -> dict:
    if not hours:
        return {"n": 0, "median": None, "average": None, "p90": None, "max": None}
    s = sorted(hours)
    p90 = s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]
    return {"n": len(s), "median": round(statistics.median(s), 2), "average": round(sum(s) / len(s), 2),
            "p90": round(p90, 2), "max": round(s[-1], 2)}


KPI_DEFINITIONS = {
    "filed": {"fi": "Tullille lähetetyt ilmoitukset (ensimmäinen lähetys jaksolla)",
              "en": "Declarations sent to Tulli (first submission in the period)"},
    "rejection_rate_pct": {"fi": "Tullin hylkäämät / lähetetyt", "en": "Rejected by Tulli / sent"},
    "amendments": {"fi": "Korjaukset: hylätyn ilmoituksen korjaus tai hyväksytyn ilmoituksen mitätöinti",
                   "en": "Amendments: a rejected declaration corrected, or an accepted one invalidated"},
    "changed_after_approval_request": {"fi": "Ilmoitus muuttui hyväksyntäpyynnön jälkeen (hyväksyntä mitätöitiin)",
                                       "en": "Declaration changed after filing approval was requested (approval voided)"},
    "time_to_release_hours": {"fi": "Tunnit lähettämisestä luovutukseen", "en": "Hours from submission to release"},
    "overrides": {"fi": "Neljän silmän poikkeukset (yksin hyväksytyt, perustelu auditoitu)",
                  "en": "Four-eyes overrides (approved alone with an audited reason)"},
}


# ---------------------------------------------------------------------------------------------
# Record retention: flag, never delete
# ---------------------------------------------------------------------------------------------
def _basis(d: dict) -> str | None:
    """The date retention runs from: release (or the final decision) of the declaration; None while it is open."""
    if d.get("status") not in FINAL:
        return None
    hist = d.get("history") or []
    return d.get("released_at") or next((h.get("at") for h in reversed(hist) if h.get("to") in FINAL), None) \
        or d.get("updated_at") or d.get("created_at")


def retain_until(basis: str, years: int) -> str:
    """Kept for `years` full years from the end of the calendar year of the basis date (Tulli archiving rule)."""
    y = int(basis[:4])
    return f"{y + years}-12-31"


@job("customs.retention_review", max_attempts=3, timeout=900, agent=False,
     label_fi="Tulliasiakirjojen säilytysaikojen tarkistus", label_en="Customs record retention review")
async def retention_review(j: dict | None = None) -> dict:
    """Daily: every customs declaration (all kinds) and the documents it used get `retention` /
    `retention_until`. Records inside the retention period are marked retained; records past it are flagged
    review_for_disposal for a person. Nothing is ever deleted here."""
    settings = await get_settings()
    years = max(6, int(settings.get("record_retention_years") or 6))
    today = now_iso()[:10]
    res = {"years": years, "checked": 0, "open": 0, "retained": 0, "review_for_disposal": 0, "documents_flagged": 0,
           "deleted": 0}
    for kind, meta in KINDS.items():
        coll = db.raw[meta["collection"]]
        async for d in coll.find({}, {"_id": 0, "id": 1, "status": 1, "history": 1, "released_at": 1, "updated_at": 1,
                                      "created_at": 1, "retention": 1, "demo": 1}):
            res["checked"] += 1
            basis = _basis(d)
            if not basis:
                ret = {"status": "open", "years": years, "basis": None, "retain_until": None}
                res["open"] += 1
            else:
                until = retain_until(basis, years)
                status = "retained" if today <= until else "review_for_disposal"
                ret = {"status": status, "years": years, "basis": basis, "retain_until": until}
                res[status] += 1
                r = await db.raw.documents.update_many({"used_by.declaration_id": d["id"],
                                                        "retention_until": {"$not": {"$gte": until}}},
                                                       {"$set": {"retention_until": until, "retention_hold": True}})
                res["documents_flagged"] += r.modified_count
            prev = d.get("retention") or {}
            if any(prev.get(k) != ret[k] for k in ("status", "years", "basis", "retain_until")):
                ret["flagged_at"] = now_iso()
                await coll.update_one({"id": d["id"]}, {"$set": {"retention": ret}})
                if ret["status"] == "review_for_disposal":
                    await audit({"type": "agent", "id": f"agent:{AGENT}", "name": "Customs controls (AEO)"},
                                "customs_retention_review_due", meta["entity"], d["id"],
                                {"retain_until": ret["retain_until"], "years": years})
    if res["review_for_disposal"]:
        from core.alerts import raise_alert
        await raise_alert("customs_retention_review", "info",
                          {"fi": f"{res['review_for_disposal']} tulliasiakirjan säilytysaika on päättynyt",
                           "en": f"{res['review_for_disposal']} customs records are past their retention period"},
                          {"fi": "Henkilö päättää hävittämisestä; järjestelmä ei poista mitään.",
                           "en": "A person decides on disposal; the system deletes nothing."},
                          link="/ops/customs", data={"count": res["review_for_disposal"]})
    return res


schedule("customs.retention_daily", job="customs.retention_review", daily_at="03:40",
         label_fi="Tulliasiakirjojen säilytys", label_en="Customs record retention")()


def default_kind_for(entity: str | None) -> str:
    return ENTITIES.get(entity or "", "import")


__all__ = ["filing_controls", "four_eyes_requirement", "approval_controls", "approval_refusal", "enforce_filing",
           "agent_reasons", "raise_if_refused", "audit_rows", "rows_to_csv", "kpis", "retention_review",
           "ControlRefusal", "control_refusal_handler", "validate_controls_settings", "SETTING_KEYS"]
