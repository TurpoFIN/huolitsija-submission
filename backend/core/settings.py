"""Business settings editable by ops admins (thresholds, margins, fees). Defaults from env."""
from core import config
from core.db import db

DEFAULTS = {
    "autopilot_enabled": False,
    "partner_network_enabled": False,
    "partner_target_margin_pct": 18.0,
    "partner_max_price_cents": 500000,
    "partner_sell_price_cents": 610000,
    "partner_approved_lanes": [],
    # Sourcing Autopilot (Hankinta-autopilotti) limits: RFQ/counter/accept e-mails per Helsinki day, negotiation rounds,
    # open RFQs per lane, and how far below the ceiling the first counteroffer starts (percent of the ceiling).
    "partner_daily_send_limit": 20,
    "partner_per_partner_daily_limit": 4,
    "partner_max_rounds": 3,
    "partner_rfqs_per_lane": 3,
    "partner_counter_anchor_pct": 5.0,
    "autopilot_quote_max_cents": 1000000,
    "autopilot_min_margin_pct": 15.0,
    "seller_business_id": config.COMPANY_BUSINESS_ID,
    "seller_vat_id": config.COMPANY_VAT_ID,
    "seller_iban": config.COMPANY_IBAN,
    "seller_bic": config.COMPANY_BIC,
    "quote_approval_threshold_eur": config.QUOTE_APPROVAL_THRESHOLD_EUR,
    "default_margin_pct": 18.0,
    "customs_clearance_fee_eur": 65.0,
    "customs_extra_item_fee_eur": 6.0,
    "documentation_fee_eur": 35.0,
    "payment_terms_days": config.DEFAULT_PAYMENT_TERMS_DAYS,
    # B2B commercial contracts: reference rate + 8 pp (Korkolaki 4 a §). 1.7.-31.12.2026: 2.5 % + 8 = 10.5 %
    # (Suomen Pankki, STT 2026-06). Review each half-year.
    "late_interest_pct": 10.5,
    "quote_validity_days": 14,
    "insurance_rate_pct": 0.25,
    "insurance_min_premium_eur": 35.0,
    "ai_auto_send_routine_messages": True,
    "ai_reply_requires_approval": True,
    "auto_approve_low_risk_kyc": True,
    "vat_rate_standard_pct": 25.5,
    "vat_rate_reduced_pct": 13.5,  # foodstuffs, animal feed, pharmaceuticals, books (Tulli VAT rates page)
    "vat_rate_reduced_low_pct": 10.0,  # newspapers and periodicals
    "record_retention_years": 6,  # customs records, POAs, invoices: 6 years from end of release year
    "email_channel_enabled": True,  # s6: customer e-mail is read, answered and acted on (off: logged, owner queue)
    # s12 retention (services/retention.py policy table): legal minimums win over smaller values
    "retention_financial_statements_years": 10,  # Kirjanpitolaki 2:10 § 1 mom.
    "retention_quote_unconverted_months": 24,
    "retention_kyc_years_after_relationship": 5,
    "retention_token_days": 7,
    "retention_privacy_request_years": 3,
    "terms_version": "1.1 · NSAB 2015 (PSYM 2015)",
    # s5 any-city quoting
    "global_quoting_enabled": True,
    "global_sourcing_deadline_hours": 72,
    "global_benchmark_stale_days": 21,
    # execution (s9): alert this many hours before each cut-off; LCL fill target; hauliers' slot answers
    "execution_cutoff_alert_hours": [48, 24, 6],
    "execution_lcl_fill_target_pct": 85.0,
    "execution_slot_auto_accept": True,
    # s14: milestone SMS to users who opted in (core/sms.py; provider by SMS_PROVIDER)
    "sms_notifications_enabled": False,
    # Autopilot operations (s2): kill switch, guardrails, owner queue, customs delegation, dunning.
    "autopilot_halted": False,  # kill switch: agent jobs wait until a person resumes with the typed word
    "guard_spend_per_day_cents": 500000,  # money agents may commit per Helsinki day (carrier bookings, claims, AI)
    "guard_confidence_floor": 0.85,
    "guard_claim_limit_cents": 100000,
    "guard_default_credit_limit_cents": 500000,  # credit limit for a new low-risk customer set by the KYC agent
    "insurance_claim_limit_cents": 0,  # insurer's delegated settlement authority; 0 = no policy recorded yet
    "owner_queue_sla_hours": 24,
    "owner_escalation_hours": 4,
    "owner_alert_email": "",  # extra mailbox for owner alerts and the daily digest (besides ops admins)
    "customs_filing_delegated": False,  # director's standing delegation to lodge declarations (typed confirmation)
    "customs_delegation_scope": "direct",  # direct | direct_and_indirect
    "customs_confirm_timeout_hours": 24,  # wait this long for the customer to confirm goods data before filing
    "dunning_enabled": True,
    "dunning_reminder1_days": 7,
    "dunning_reminder2_days": 21,
    "dunning_final_days": 35,
    "dunning_collection_days": 45,
    "dunning_fixed_compensation_cents": 4000,  # Korkolaki 633/1982 10 a §: EUR 40 once per late B2B invoice
    "dunning_reminder_fee_cents": 0,
    "rate_expiry_warning_days": 14,
    # s7 commercial
    "crm_daily_send_limit": 20,
    "crm_follow_up_days": 4,
    "crm_max_follow_ups": 2,
    # TOL 2008 groups (4 digits; PRH open data v3 mainBusinessLine) that ship goods: manufacturing and wholesale
    "crm_target_industries": ["1623", "2229", "2511", "2599", "2829", "3109", "4669", "4673", "4675", "4690"],
    "crm_target_regions": [],
    "credit_default_limit_cents": 500000,
    "credit_auto_limit_max_cents": 5000000,
    "credit_hold_overdue_days": 30,
    "insurance_provider": "",
    # Customs AEO-style controls (s11, services/customs_aeo.py): four-eyes thresholds, who may file, re-check age.
    "customs_four_eyes_duty_eur": 5000,  # duty + import VAT at or above this needs a second person
    "customs_four_eyes_value_eur": 100000,  # customs value at or above this needs a second person
    "customs_authorised_filers": [],  # user ids allowed to file; [] = everyone with customs.file
    "customs_rescreen_days": 1,  # EORI / party screening results older than this are checked again before filing
    "customs_screening_required_lists": ["eu", "un"],  # sanctions lists a filing needs fresh; others when loaded
    "email_channel_enabled": True,  # s6: customer e-mail is read, answered and acted on (off: logged, owner queue)
}


async def get_settings() -> dict:
    doc = await db.settings.find_one({"key": "app"}, {"_id": 0}) or {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in doc.items() if k in DEFAULTS})
    return out


async def update_settings(changes: dict) -> dict:
    clean = {k: v for k, v in changes.items() if k in DEFAULTS}
    if clean:
        await db.settings.update_one({"key": "app"}, {"$set": clean}, upsert=True)
    return await get_settings()
