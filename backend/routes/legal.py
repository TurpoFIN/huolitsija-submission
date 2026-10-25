"""Public legal identity. Explicit allowlist: never serialize config/settings wholesale."""
from fastapi import APIRouter, Response
from core import config
from core.settings import get_settings

router = APIRouter(prefix="/api/legal", tags=["legal"])


@router.get("/config")
async def legal_config(response: Response):
    response.headers["Cache-Control"] = "no-store"
    s = await get_settings()
    business_id = s.get("seller_business_id") or None
    # An ID alone is not proof of incorporation; registration is explicitly confirmed by the operator.
    registered = config.env_bool("COMPANY_REGISTERED", False)
    return {
        "company": {
            "name": config.COMPANY_NAME,
            "registered": registered,
            "business_id": business_id,
            "vat_id": s.get("seller_vat_id") or None,
            "eori": config.COMPANY_EORI or None,
            "address": config.env("COMPANY_ADDRESS") or None,
            "email": config.COMPANY_EMAIL or None,
            "privacy_email": config.env("COMPANY_PRIVACY_EMAIL") or config.COMPANY_EMAIL or None,
            "phone": config.COMPANY_PHONE or None,
            "registered_office": config.env("COMPANY_REGISTERED_OFFICE") or None,
        },
        "session_days": config.SESSION_DAYS,
        "notice_version": "1.1",
    }
