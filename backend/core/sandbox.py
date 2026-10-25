"""Which companies run against sandboxes instead of real outside systems.

A sandbox company is a test tenant (company.test_tenant / test, set for e2e./test./kuvatesti. sign-ups or by ops)
whose Business ID is in the reserved unissued 399xxxx block. Real Business IDs are issued sequentially (3596xxx in
2026), so a real customer can never land in the sandbox even if their e-mail looks like a test address.

For a sandbox company the registers (YTJ, VIES, EU EOS), Finnish Customs, carrier booking and the movement simulator
answer from sandboxes, and every such answer is marked source "sandbox". TEST_TENANT_SANDBOX=false turns this off.
"""
from core import config


def sandbox_enabled() -> bool:
    return config.env_bool("TEST_TENANT_SANDBOX", True)


def is_test_company(company: dict | None) -> bool:
    return bool(company and (company.get("test_tenant") or company.get("test")))


def reserved_business_id(business_id: str | None) -> bool:
    digits = "".join(ch for ch in (business_id or "") if ch.isdigit())
    return len(digits) == 8 and digits.startswith("399")


def is_sandbox_company(company: dict | None) -> bool:
    return bool(sandbox_enabled() and is_test_company(company) and reserved_business_id((company or {}).get("business_id")))


def real_registers() -> bool:
    """Read-only public registers (YTJ, VIES, EU EOS, PRH) answer for real even for sandbox companies (no-sandbox rule):
    a reserved 399xxxx ID then reads "not found" from the real register. Submissions (Tulli, carrier booking, movement
    messages) keep their sandbox guard. TEST_TENANT_REAL_REGISTERS=false restores the sandbox register answers (CI)."""
    return config.env_bool("TEST_TENANT_REAL_REGISTERS", True)


def sandbox_registers(company: dict | None) -> bool:
    return is_sandbox_company(company) and not real_registers()


async def sandbox_company_id(company_id: str | None) -> bool:
    if not company_id:
        return False
    from core.db import db
    c = await db.raw.companies.find_one({"id": company_id}, {"_id": 0, "test_tenant": 1, "test": 1, "business_id": 1})
    return is_sandbox_company(c)


# ---------------------------------------------------------------------------------------------
# Sandbox register answers (YTJ, VIES, EU EOS) for sandbox companies. Shapes match services/kyc.py.
# ---------------------------------------------------------------------------------------------
SANDBOX_SOURCE = "Sandbox register (test tenant, reserved 399xxxx Business ID)"


def sandbox_ytj(company: dict, business_id: str) -> dict:
    from core.util import now_iso
    addr = company.get("address") or {}
    return {"found": True, "business_id": business_id, "name": company.get("name"), "auxiliary_names": [],
            "company_form": "Osakeyhtiö", "status_code": "sandbox", "business_id_status": "valid",
            "trade_register_status": "registered", "registration_date": "2026-01-01", "end_date": None,
            "main_business_line": "Sandbox test company", "address": {k: addr.get(k) for k in ("street", "postal_code", "city", "country")},
            "vat_registered": True, "employer_register": False, "prepayment_register": True, "situations": [],
            "website": None, "last_modified": now_iso(), "source": SANDBOX_SOURCE, "url": None, "fetched_at": now_iso(),
            "sandbox": True}


def sandbox_vies(vat_id: str) -> dict:
    from core.util import now_iso
    return {"status": "pass", "valid": True, "vat_id": vat_id, "summary": f"{vat_id} is valid in the sandbox VAT register",
            "summary_fi": f"{vat_id} on voimassa sandbox-ALV-rekisterissä",
            "data": {"name": None}, "source": SANDBOX_SOURCE, "checked_at": now_iso(), "sandbox": True}


def sandbox_eori(business_id: str, stored: str | None = None) -> dict:
    """The sandbox EORI register holds exactly FI<Business ID> for each sandbox company; any other number stored on the
    profile is not registered (a test can store one to exercise the not-registered path)."""
    from core.util import now_iso
    from services.customs import eori_normalise
    eori = f"FI{business_id}"
    asked = eori_normalise(stored)
    if asked and asked != eori_normalise(eori):
        return {"status": "fail", "valid": False, "eori": asked, "summary": f"{asked} is not in the sandbox EORI register",
                "summary_fi": f"{asked} ei ole sandbox-EORI-rekisterissä", "data": {"eori": asked}, "tried": [asked],
                "source": SANDBOX_SOURCE, "checked_at": now_iso(), "sandbox": True}
    return {"status": "pass", "valid": True, "eori": eori, "summary": f"{eori} is valid in the sandbox EORI register",
            "summary_fi": f"{eori} on voimassa sandbox-EORI-rekisterissä",
            "data": {"eori": eori, "status": "0"}, "tried": [eori], "source": SANDBOX_SOURCE, "checked_at": now_iso(),
            "sandbox": True}
