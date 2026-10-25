# Huolitsija operating and go-live runbook

25 October 2026 · Road Step 7 · live identity `f53f2fa06ba444c47e68e7efc05c8087c8dd021f` (since 01:44 UTC). All five legal pages, including the DPA, are verified on this identity; the recorded journey is still gated.

Start with [Owner activation actions](OWNER-ACTIVATION.md): exact company steps, integrator handoff and required receipts for all ten provider areas. It supersedes older activation guidance below where noted.

**Use Autopilot for routine operations, with an accountable owner and qualified contracted specialists for the legal responsibilities listed in [LEGAL-HUMAN-POINTS.md](LEGAL-HUMAN-POINTS.md).** This document does not certify commercial readiness. Several integrations remain partial or unconfigured; production Customs requires transport and customer-conformance evidence, not merely credentials.

Latest acceptance rule: capability rows use only **live / live-ready / partial / missing**. Test mode is permitted only when production is blocked on a named company-only step (incorporation, signed contract or payment). Missing implementation or unproven behavior stays partial/missing; simulated success is not a final capability result.

## Evidence and revision discipline

Before any live run, read `https://huolitsija-app.emergent.host/api/version`, record its commit and the UTC time, and compare the rendered frontend asset identity. Read authenticated `/api/integrations/status`, `/api/settings`, `/api/autopilot/status`, the sourcing status and insurance provider status from the same release. Use the credential-store **sign_in** workflow for demo accounts; never print passwords, session cookies, API keys, webhook tokens or PEM keys. **Correction:** an earlier claim that demo passwords were already in the person's store was wrong. Presence and a successful sign-in must be observed. The corrected review path is **`evidence/s2-review/r2/REVIEW-r2.md`**.

Integrator's live probe at 21:53 UTC against `a86ad24`: email `log`, SMS `log` and disabled; OpenAI available; Stripe test mode and webhook enabled; Tulli disabled; insurance provider absent (Redkik sandbox unconfigured); credit bureau absent; CRM and partner messages use sandbox domains and log delivery; company Business ID, VAT ID and EORI absent. These are a dated baseline, not current proof after another deploy. Hosting is Emergent, with MongoDB through Emergent; region and contractual subprocessors are unverified.

## Current live state — f53f2fa, 25 October 2026 02:05 UTC

Read from the public host at 02:05 UTC; it supersedes the r8a notes below where they differ.

| Area | Observed on `f53f2fa` | Evidence |
|---|---|---|
| Legal pages | `/terms` (version 1.1), `/privacy`, `/dpa`, `/cookies` and `/company` render in Finnish and English, light and dark, at 1440 and 390 px: 40 of 40 checks pass, no horizontal overflow, the DPA shows its seven sections. The r8a finding that the DPA was absent no longer applies. | `evidence/s7-legal-live-f53f2fa/results.json` and its 48 screenshots |
| Company identity | `GET /api/legal/config`: name Huolitsija Oy, `registered=false`; Business ID, VAT ID, EORI, address, phone and registered office are empty; contact and privacy e-mail `ops@huolitsija.fi`; notice version 1.1. | live probe 02:05 UTC |
| Autopilot | `GET /api/autopilot/status`: enabled=false, halted=false (last changed 00:45 UTC, incident drill). It stays off globally; only a test-tenant switch is allowed. | live probe 02:05 UTC |
| Runbook in the app | `/ops/runbook` (internal staff only) serves this runbook, its companion documents and this PDF from the release. Handed to the integrator as `18fc522` for the 02:30 UTC train; it counts as live only once `GET /api/runbook` answers on the public host. | branch `feat/runbook-0230` |
| Recorded journey | Not run. Gates: s2's email hop on test tenant `e19d517b`, the master's EUR.1 clearance and the integrator's activation. | — |
| Capability count | 14 of 104 rows live (recount lane, sprint state 02:00 UTC). | `shared/capmap/CAPABILITY-MAP-live.md` |

## r8a observed state — 25 October 2026

Integrator evidence at 00:27 UTC identifies `fc211b4ec3514c733c719367881563c2f5858004`; an independent s4 version read on this continuation matches. Source: `shared/evidence/integrator-r8/r8a-live/api-probes.json`. Deep health returned HTTP 200 with all listed checks `ok`; this is service health, not customer-journey proof. Email is Postmark live with sender `Huolitsija <marc+huolitsija@heliosone.fi>`; Stripe remains test mode with its webhook enabled. SMS is disabled/log-only. Tulli is disabled with no endpoint or declarant ID configured. The company remains unregistered with Business ID, VAT ID, EORI, address and registered office missing. Contact and privacy email are `ops@huolitsija.fi`.

The integrator's 00:28:48 UTC Autopilot read shows enabled=false and halted=false. Keep it off until the master confirms the s8 EUR.1 test is done and permits activation; the integrator must also confirm the single-RFQ guard and webhook/config readiness. Only the integrator enables it immediately before recording. Prospecting/sourcing outreach stays log-only. Step 7 observes and records; it does not toggle Autopilot or manually advance business work.

The shared capability recount for this exact SHA is **12/104 live, 2 live-ready, 85 partial, 5 missing**. This is the capability-map lane's count, not a new s4 recount. Step 7 has no standalone rows in the fixed 104-row inventory; publishing legal pages does not increase the numerator. No road step is complete solely because its UI renders.

The earlier four-route legal proof is bound to r7 `00c10d5`; do not relabel its screenshots as r8. The s4 eight-state `/dpa` probe against this identity rendered the homepage in every state, using frontend asset `main.a7b80349.js`; the expected DPA heading and seven sections were absent. DPA source exists in `66ada89`, but its deployed publication is unverified pending the integrator's final r8 SHA. Failure evidence: `evidence/s4-continuation/live-r8-dpa/results.json`. Company fields stay incomplete regardless of legal-page publication. The recorded digital journey and separate Stripe test-paid receipt remain unverified until their actual evidence is retained.

## Launch sequence

1. Incorporate and record the legal entity and authorised representatives. Populate identity below; obtain tax registration, EORI and relevant mandates. Do not borrow Helios One Oy's Business ID for the new company.
2. Name the responsible privacy/controller contact and statutory decision owners. Confirm actual processor contracting entities, DPAs, hosting/data regions, international-transfer safeguards and retention policy. Test the contact channel. An unincorporated name is not a complete GDPR controller identity.
3. Establish approved mail delivery and inbound routing, OpenAI configuration, authorised payment, carrier and customs paths, monitoring and backups. Preserve isolation between test tenants and commercial customers.
4. Run [FINAL-RUN-PLAN.md](FINAL-RUN-PLAN.md) on the exact deployed release. Gate on business behavior, not passing unit tests. Capture all manual actor interventions.
5. Activate contracted live accounts individually, verify their receipts, then enable Autopilot and sourcing within approved limits. For an unavailable integration keep its explicit unavailable state; never convert a stub response to success.
6. Commercial launch still requires actual company identity, service contracts, insurance arrangements and any required Customs/ICS2 certification. Record these as evidence, not placeholder checkmarks.

## Configuration ownership

Set secret values through the deployment's existing secret store. Backend env changes require a backend restart/redeploy; `REACT_APP_BACKEND_URL` is a frontend build-time value. Settings are persisted in MongoDB (`key=app`); a new environment default does **not** replace an existing stored setting. `/api/settings` requires an internal session; writes require `settings.write`. Never export that whole response into public evidence.

[CONFIG-INVENTORY.md](CONFIG-INVENTORY.md) enumerates every literal runtime environment name and every core app setting in this source snapshot, with owning code and defaults where declared. Dynamic families and pending-step variables are expanded below. [PROCESSORS.md](PROCESSORS.md) maps recipients to real code and observed activation. Re-run the inventory after other branches land; this is part 1, not a claim that unpublished branch configuration is deployed.

Published terms are now version 1.1; new signup consent records use 1.1. Preserve existing contract versions. `TERMS-v1.0-source.js` retains the prior published wording for historical records; it is not the current notice. If stored `terms_version` remains 1.0, update the setting to `1.1 · NSAB 2015 (PSYM 2015)` for new quotes after deployment, without rewriting existing quotes/invoices.

### Platform, identity and authentication

| Variables / switches | Purpose and go-live action |
|---|---|
| `MONGO_URL`, `DB_NAME` | Persistent database connection/database; secret URI. Use a separate disposable DB for tests. Obtain backup/restore policy from Emergent and prove a restore in isolation. |
| `APP_BASE_URL`, `REACT_APP_BACKEND_URL`, `CORS_ORIGINS` | Public links/payment redirects, optional split API URL, exact allowed origins. Production must use HTTPS and restrict origins; avoid `*`. Same-origin `/api` is the normal frontend path. |
| `JWT_SECRET`, `SESSION_DAYS`, `COOKIE_SECURE` | Stable protected signing key (or existing DB-stored key), default 14-day maximum session, secure cookie true in production. Key rotation can invalidate sessions; record and test it. |
| `COMPANY_NAME`, `COMPANY_BUSINESS_ID`, `COMPANY_VAT_ID`, `COMPANY_EORI`, `COMPANY_ADDRESS`, `COMPANY_EMAIL`, `COMPANY_PHONE`, `COMPANY_IBAN`, `COMPANY_BIC` | Registered name, Y-tunnus, tax/customs IDs, full postal address, monitored mailbox, optional telephone and settlement account. Set from authoritative incorporation/bank records. `seller_business_id`, `seller_vat_id`, `seller_iban`, `seller_bic` stored settings override corresponding defaults. Reconcile documents and public identity. |
| `COMPANY_REGISTERED`, `COMPANY_REGISTERED_OFFICE`, `COMPANY_PRIVACY_EMAIL` | s4 additions: explicitly confirm registration, registered municipality and monitored privacy contact (falls back to company email). `/api/legal/config` exposes only public identity, session lifetime and notice version. An ID alone does not imply incorporation. |
| `SEED_DEMO`, `DEMO_OPS_EMAIL`, `DEMO_OPS_PASSWORD`, `DEMO_RESET_PASSWORDS` | Seed/test identity controls. Disable demo seeding/reset for commercial operation; preserve needed test identities in their isolated sandbox. Do not reset existing customer passwords. |
| `API_RATE_LIMIT_PER_MIN`, `PUBLIC_QUOTE_RATE_LIMIT_PER_HOUR`, `PRIVACY_RATE_LIMIT_PER_EMAIL_HOUR`, `PRIVACY_RATE_LIMIT_PER_IP_HOUR`, `TRUSTED_PROXY_HOPS` | Abuse controls. Configure trusted proxy hops to the real ingress, so spoofed forwarding headers cannot bypass client limits. |

### Autopilot, emergency stop and daily operations

Enable at Ops → Autopilot (or the Settings autonomy control), using the exact confirmation **`enable`**. The endpoint is `POST /api/settings/autonomy/autopilot_enabled` with `{ "enabled": true, "confirmation": "enable" }`. This is a privileged, audited action; verify it first on a controlled authorised company journey. Set guardrails before enabling.

| Runtime / setting | Operational meaning |
|---|---|
| `JOBS_WORKER_ENABLED`, `JOBS_POLL_SECONDS`, `JOBS_CONCURRENCY` | Durable worker on/off; default polling 2 seconds, concurrency 4. A running process does not prove jobs advance; inspect jobs, leases and heartbeat. |
| `autopilot_enabled`, `autopilot_halted` | Enabled is business automation; halted is global hold. Do not set these by raw DB writes. Agent jobs stay queued/retry while halted. |
| `guard_spend_per_day_cents`, `guard_confidence_floor`, `guard_claim_limit_cents`, `guard_default_credit_limit_cents` | Daily committed spend, confidence, claims and customer exposure. Currency is cents; verify boundary behavior in the actual decision log. |
| `insurance_claim_limit_cents` | Insurer-delegated settlement ceiling. Default zero means no authority recorded. Do not raise without the policy/insurer mandate. |
| `owner_alert_email`, `OWNER_ALERT_EMAIL`, `owner_queue_sla_hours`, `owner_escalation_hours` | Owner notification/digest and escalation. Confirm actual delivery; log mode is not an alert to a person. |
| `customs_filing_delegated`, `customs_delegation_scope`, `customs_confirm_timeout_hours` | Internal standing filing delegation and scope (`direct` or `direct_and_indirect`); customer POA remains separately required. Use `POST /api/autopilot/customs-delegation` with enabled/scope/confirmation/signer_name/signer_title. |
| `TEST_TENANT_SANDBOX`, `SIMULATOR_ENABLED`, `SIMULATOR_STEP_SECONDS` | Reserved test-company path and simulated movement. Never use simulation as proof of physical freight movement. Simulated test-tenant results never prove a real provider transaction. |
| `EXC_REBOOK_HOURS`, `DELIVERY_POD_WAIT_HOURS`, `INVOICE_CUSTOMS_WAIT_HOURS`, `INVOICE_AUTO_ISSUE`, `EXCEPTION_DETECTION` | Exception and delivery/invoice timing controls; POD and customs evidence must still be present. |

**Emergency stop:** use Ops → Autopilot stop / `POST /api/autopilot/kill`; inspect `autopilot_halted=true` and the held job list. Stop sourcing separately with `POST /api/partner-network/stop` (cancels queued sends) and, if necessary, `PARTNER_NETWORK_KILL=true`. Stop CRM outbound execution as well via its documented enabled/Autopilot control and provider log mode. This does not recall already sent email, cancel a bank payment, undo a carrier booking or retract an accepted Customs declaration. Inspect in-flight jobs and external receipts before retrying; uncertain external submissions require reconciliation, never blind replay.

**Resume:** reconcile in-flight effects, correct the incident, then `POST /api/autopilot/resume` with confirmation `enable`. Read the returned switches and explicitly enable each desired autonomy switch; do not assume sourcing resumes. Run one controlled transaction before widening traffic.

**Daily owner check:** failed/late jobs and dead letters; approvals and legal holds; missing carrier confirmations, customs responses and POD; overdue invoices/payment mismatches; message bounces; sanctions/PEP source freshness; retention failures; spend and credit guards. Retry a dead letter only after determining whether the provider already acted. Review the daily digest as monitoring evidence, not as proof that all business work succeeded. A sole owner can contract cover for required human decisions during absence.

### OpenAI provider and model

`LLM_PROVIDER=openai`, `OPENAI_API_KEY`, `OPENAI_BASE_URL` (official API base), `LLM_MODEL`, `LLM_MODEL_FAST`, `LLM_REASONING_EFFORT`, `LLM_MAX_CONCURRENCY`; legacy `LLM_VISION_PROVIDER`/`LLM_VISION_MODEL`, `EMERGENT_LLM_KEY` and `ANTHROPIC_API_KEY` exist in the base snapshot. **Use OpenAI only.** Do not activate legacy alternate-provider fallbacks. The model-fix lane owns routing changes; inspect its final code and runtime status before launch.

Fetch the live account's `/v1/models` using the protected OpenAI key; save only model IDs and timestamp, never headers/key. Select an ID from that response, then prove text, structured JSON and document/image behavior through the actual app. A models-list result alone does not prove the chosen model supports the requested payload. `scripts/llm_check.py --list` is the repository's diagnostic; review its current implementation before execution. No model ID is prescribed from memory in this runbook. Verify `ai_runs` records OpenAI for each relevant operation; check the final vision branch explicitly.

### Email, SMS and inbound journeys

| Variables / switches | Activation and observable proof |
|---|---|
| `EMAIL_PROVIDER`, `EMAIL_FROM` | `log` stores only; `postmark`, `resend`, `smtp` use their actual delivery adapters. Choose a verified sender at heliosone.fi until a company domain is available. |
| `POSTMARK_SERVER_TOKEN`, `POSTMARK_WEBHOOK_TOKEN` | Use the existing server, request/confirm account approval, verify **DKIM and Return-Path on heliosone.fi** from Postmark's generated DNS values (do not guess selectors). Test-domain delivery is not approval to send to arbitrary recipients. Configure Delivery/Bounce webhooks on `/api/webhooks/postmark`; token is HTTP Basic-auth secret. Confirm delivered event and external inbox receipt. |
| `RESEND_API_KEY`, `RESEND_WEBHOOK_SECRET` | Alternative adapter, only if actually selected/contracted. Signature-verified delivery events; avoid claiming it is currently in use because code exists. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_SECURITY` | General SMTP (`starttls` or `ssl`; `none` only isolated tests). Separate `PARTNER_SMTP_*`, `CRM_SMTP_*` share these five suffixes. |
| `SMS_PROVIDER`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` or `TWILIO_MESSAGING_SERVICE_SID`, `TWILIO_STATUS_CALLBACK_URL` | `log` sends nothing. **Trial → paid:** upgrade the existing Twilio account, assign an SMS-capable sender/service, complete destination-country registration, use live account credentials. Trial verified-recipient restrictions and test-credential magic numbers are not production. Callback `/api/webhooks/twilio/sms-status`; prove real delivery to an opted-in test recipient and the authenticated status callback. |
| `sms_notifications_enabled` and customer opt-in | Both operational switch and recipient preference must permit service SMS. Turning the global switch on does not grant customer consent. |
| `EMAIL_CHANNEL_IMAP_HOST/PORT/USER/PASSWORD/SECURITY/FOLDER`, `EMAIL_CHANNEL_WEBHOOK_SECRET`, `EMAIL_CHANNEL_FROM`, `EMAIL_CHANNEL_DOMAIN`, `EMAIL_CHANNEL_PROVIDER` | s6 declared inbound/outbound email-only path. Raw RFC822/Postmark input `/api/email-channel/inbound` uses HMAC of raw bytes. Verify actual provider support or signed relay; never disable authentication to make Postmark work. Message-ID correlation and sender matching must survive retries. |
| `email_channel_enabled`, `email_channel_auto_reply` | s6 proposed settings; reconcile after its merge. An email-only journey may use secure one-time KYC links and Stripe-hosted checkout, but must not require portal login or an ops operator to advance routine work. |

### Partner sourcing and CRM

Sourcing is a separate switch: `POST /api/settings/autonomy/partner_network_enabled` with `{ "enabled": true, "confirmation": "enable" }`; Ops → Sourcing / Hankinta. It is independent of routine Autopilot, while respecting global halt. Limits: `partner_target_margin_pct`, `partner_max_price_cents`, `partner_sell_price_cents`, `partner_approved_lanes`, `partner_daily_send_limit`, `partner_max_rounds`, `partner_rfqs_per_lane`, `partner_counter_anchor_pct`. Keep an approved-lane list and demonstrate that opt-outs prevent later sends.

`PARTNER_EMAIL_PROVIDER`, `PARTNER_EMAIL_FROM`, `PARTNER_REPLY_TO`; `PARTNER_IMAP_HOST/PORT/USER/PASSWORD/SECURITY/FOLDER`; `PARTNER_INBOUND_SECRET`, `PARTNER_INBOUND_REQUIRE_AUTH`, `PARTNER_OPTOUT_SECRET`; `PARTNER_SANDBOX_DOMAIN`, `PARTNER_NETWORK_KILL`; optional `PARTNER_SMTP_*`. **Remove `PARTNER_SANDBOX_DOMAIN` only after approved sender/inbound delivery, opt-out handling, limits and one controlled real outreach are proved.** Empty/unset changes the recipient from a sandbox mailbox to the actual carrier. Stop queued messages and inspect recipients before switching.

CRM: `CRM_EMAIL_PROVIDER`, `CRM_EMAIL_FROM`, `CRM_REPLY_TO`, `CRM_PUBLIC_BASE_URL`, `CRM_OPTOUT_SECRET`, `CRM_PROSPECT_DAILY_AT`, `CRM_IMAP_HOST/PORT/USER/PASSWORD/SECURITY/FOLDER`, `CRM_SMTP_*`, `CRM_SANDBOX_DOMAIN`, `TRADE_STATS_SOURCE`. Settings: `crm_daily_send_limit`, `crm_follow_up_days`, `crm_max_follow_ups`, `crm_target_industries`, `crm_target_regions`. **Remove `CRM_SANDBOX_DOMAIN` separately** after confirming sender, lawful business contact selection, source attribution and opt-out. Removing one sandbox domain does not remove the other.

### Quotes, credit and insurance

`QUOTE_APPROVAL_THRESHOLD_EUR`, `DEFAULT_PAYMENT_TERMS_DAYS`, `QUOTE_CUSTOMS_INCLUDED_ITEMS`, `QUOTE_ICS2_FEE_EUR`, `QUOTE_CUSTOMS_FEE_VAT_CODE`, `QUOTE_DOMESTIC_LEG_VAT_CODE`, `PUBLIC_QUOTE_RATE_LIMIT_PER_HOUR`. Core settings include margins, clearance/document fees, quote validity, VAT rates, payment terms, credit ceilings and `autopilot_quote_max_cents`/`autopilot_min_margin_pct`; see inventory. Revalidate legal tax rates at launch rather than treating source defaults as permanent tax advice.

Global geocoding/routing: `GEOCODER_URL`, `PHOTON_URL`, `OSRM_URL`, `SEA_AVOID_RED_SEA`. Distinguish geocoder coordinates, rate-card evidence, live carrier rates, indicative estimates and unanswered RFQs. An absent seeded rate must not be replaced with an invented confirmed price.

Credit: `CREDIT_BUREAU=none|asiakastieto|creditsafe`; `ASIAKASTIETO_BASE_URL/USER_ID/PASSWORD`, `CREDITSAFE_BASE_URL/USERNAME/PASSWORD`; settings `credit_default_limit_cents`, `credit_auto_limit_max_cents`, `credit_hold_overdue_days`. A PRH/YTJ lookup is not a bureau credit report. Personal/sole-trader decisions need the privacy assessment in the legal matrix.

Insurance: `INSURANCE_PROVIDER=redkik` takes precedence over setting `insurance_provider`; `REDKIK_BASE_URL`, `REDKIK_CLIENT_ID`, `REDKIK_CLIENT_SECRET`, `REDKIK_POLICY_ID`, `REDKIK_COMMODITY_ID`, `REDKIK_COVER_TYPES`. Obtain a real distribution/policy arrangement and organisation credentials. Default base is `https://sandbox.app.redkik.com`; production is `https://sales.app.redkik.com` in the adapter. Verify these against the granted account before switching. Test quote → explicit bind → real policy ID/certificate and limits. `stub_named` is **not insurance**. Loadsure is documented-only in this snapshot, despite an older interface draft suggesting a provider switch. Settings `insurance_rate_pct`/`insurance_min_premium_eur` do not create coverage.

### Carrier, execution, documents and visibility

`CARRIER_BOOKING_MODE=log|sandbox|live`, `CARRIER_CONFIRM_HOURS`; dynamic `CARRIER_<CODE>_BASE_URL`, `_BOOKING_URL`, `_API_KEY`, `_ENABLED`, `_SANDBOX`, `_PORTAL_ACCOUNT`, `_SERVICE_CONTRACT_REF`, `_CONTRACT_REF`. Codes are normalised from carrier records; adapter type selects required names. Test a real contract/quotation reference and a correlated confirmation; a request acceptance is not a confirmed booking. `DCSA_<SCAC>_BASE_URL/API_KEY/API_SECRET/KEY_HEADER` is the separate tracking family. `DHL_API_KEY`, `DIGITRAFFIC_ENABLED`, `POSITION_TICK_SECONDS` control existing tracking inputs.

s9 declared: `EXECUTION_FROM`, `EXECUTION_SANDBOX_DOMAIN`, `WMS_PROVIDER=none|email|odoo`, `WMS_ODOO_URL/DB/USER/API_KEY`; operations include SI/VGM cutoffs, pickups, warehouse handoff and consolidation. Check the actual merged reader before setting. Sandbox recipient rewrites must be removed only after the same controlled-delivery checks as sourcing.

s8 declared: `TRADEDOCS_AUTO`, `EBL_PLATFORM=none|dcsa`, `EBL_ISSUANCE_URL`, `EBL_ISSUANCE_TOKEN`, `COO_CHAMBER_EMAIL`, `TRADEDOCS_ISSUER_*`. A generated B/L PDF is not a carrier-signed negotiable title; an origin application is not a chamber-issued certificate. Record actual issuers/signatures and evidence.

s10 declared: `AISSTREAM_API_KEY`, `AIS_GLOBAL_ENABLED`, `FLIGHT_TRACKING_PROVIDERS`, `OPENSKY_CLIENT_ID/CLIENT_SECRET`, `TERMINAL49_API_KEY`, `MILESTONES_FROM_SCHEDULE`, `EXC_ROLLOVER_DAYS`, `EXC_CONGESTION_HOURS`. Record provenance/freshness. A map route, AIS vessel position or scheduled milestone is not proof that a specific shipment was physically delivered.

### Customs, trade compliance and retention

`TULLI_MODE=disabled|sandbox|test|production`; `TULLI_IMPORT_MODE`, `TULLI_EXPORT_MODE`, `TULLI_TRANSIT_MODE` override per system once s11 lands. `TULLI_ENDPOINT`, `TULLI_CLIENT_CERT`, `TULLI_CLIENT_KEY`, `TULLI_DECLARANT_ID`; sandbox timing `TULLI_SANDBOX_RELEASE_SECONDS`, `TULLI_SANDBOX_DECISION_SECONDS`, and s11 `TULLI_SANDBOX_EXIT_SECONDS`.

**Production transport remains partial pending deployed authority proof.** Customs lane reconciliation of `9258593..9d7e95f` reports no transport/configuration changes: the v1.21 implementation already in `9258593` passed 40 protocol and 5 exchange-durability checks in the frozen full run. The supplied schemas/examples and guidebooks are available; no acquisition request remains. No deployed company transport or live authority acceptance was verified. UTU customer status through Authorisations and Decisions, separate DVV enrolment/certificate, applicable procedure testing and customer mandates remain distinct gates. Local protocol/XSD checks do not prove Customs acceptance.

Transit additional names: `TULLI_HOLDER_EORI`, `TULLI_HOLDER_NAME`, `TULLI_TRANSIT_OFFICE_OF_DEPARTURE`, `TULLI_GUARANTEE_GRN`, `TULLI_GUARANTEE_ACCESS_CODE`. Guarantee amount/currency is declaration data. EU COL office ingestion is incomplete; fresh verified office records/roles are required. Arrival and unloading require actual physical-event evidence.

ICS2: `ICS2_MODE=disabled|sandbox|sti|conformance|production`; `ICS2_EORI`, `ICS2_AS4_ENDPOINT`, `ICS2_AS4_CERT`, `ICS2_AS4_KEY`, `ICS2_PARTY_ID`, `ICS2_RECEIVER_PARTY_ID`, `ICS2_SERVICE`, `ICS2_ACTION`, `ICS2_FINAL_RECIPIENT`, optional `ICS2_PARTY_ID_TYPE`, `ICS2_SERVICE_TYPE`, `ICS2_AP_USERNAME`, `ICS2_AP_PASSWORD`. Obtain EU Login/UUM&DS roles, registered AP/PMode and self-conformance approval. AP acknowledgement differs from ENS business acceptance. A downloaded STI XML file is not a submitted declaration. The implemented Domibus profile is inline/base64; MTOM retrieval is unimplemented. See `shared/s11/golive-ncts-ics2.md`.

`TARIC_SOURCE=consultation` is the documented source; HTTPS access plus DB is required, no paid key. Do not present an AI fallback estimate as authoritative duty. `customs_four_eyes_duty_eur`, `customs_four_eyes_value_eur`, `customs_authorised_filers`, `customs_rescreen_days` are s11 settings. Keep authority, POA, EORI and independent-review predicates intact.

Compliance: `TRADE_GOV_API_KEY` optional; `OPENSANCTIONS_API_KEY` needs licence; `COMPANIES_HOUSE_API_KEY`; actual current PRH reader uses `PRH_BO_API_URL`, `PRH_BO_CLIENT_ID`, `PRH_BO_USER_ID`, `PRH_BO_SERVICE` (the older interfaces.md mentions a client secret, so reconcile the contract before provision). `IATA_DGR_DATA_PATH` is a licensed data path, not permission to redistribute a manual.

Retention settings: `record_retention_years`, `retention_financial_statements_years`, `retention_quote_unconverted_months`, `retention_kyc_years_after_relationship`, `retention_token_days`, `retention_privacy_request_years`. Review `/api/compliance/retention/*`, legal holds and export/erasure requests. Run dry-run first, inspect real candidate records and law/purpose, then apply. Global halt may hold scheduled retention; owner must cover deadlines. Customer access closure does not erase mandatory accounting/customs records. Backups need their own retention/restore controls.

### Billing, payments, finance, API and EDI

`STRIPE_API_KEY` determines test/live; `STRIPE_WEBHOOK_SECRET` or Emergent-compatible `PAYMENT_WEBHOOK_SIGNING_SECRET` must match the same Stripe environment and endpoint `/api/webhooks/stripe`. Use a real authorised payment; test mode is permitted only while the named company activation step blocks live payments. Go-live: complete Stripe business verification with the real representative, use live keys and live webhook secret, check amount/currency/idempotency and reconcile a controlled payment. A checkout return URL is not payment proof; authenticated webhook and ledger allocation are required.

`TRACKING_WEBHOOK_SECRET`, `BANK_WEBHOOK_SECRET` sign raw provider payloads. `/api/webhooks/bank/camt054` imports XML; verify exact bank line, invoice matching, duplicates and unmatched handling. Dunning settings: enabled, first/second/final/collection days, fixed compensation and reminder fee. Do not treat the source's fee/interest citation as legal approval; validate current interest/fee rules with the actual B2B contract.

s13 declared: `FX_SOURCE`, `AP_IMAP_HOST/PORT/USER/PASSWORD/SECURITY/FOLDER`, `AP_INBOUND_REQUIRE_AUTH`, `BANK_PAYMENT_MODE=file|api`, `BANK_API_PROVIDER` and provider-specific `BANK_API_*`, `LEDGER_EXPORT_FORMAT=procountor|netvisor|csv`, `LEDGER_API=off|netvisor|procountor` and account keys, `COLLECTIONS_AGENCY=none|file`, `VAT_FILING_MODE=file|api`. Provider-specific names are unresolved until the actual adapter lands; never invent them. Settings: `ap_tolerance_pct`, `ap_tolerance_abs_cents`, `ap_auto_approve_max_cents`, `ap_payment_days_before_due`, `collections_after_days_overdue`, `collections_fee_cents`, `collections_agency_after_days`, `fx_revalue_month_end`, `ledger_accounts`. A payment/export file is not bank acceptance or a filed tax return.

Shipper API keys are scoped per customer and revoked in Portal → API. Keep `WEBHOOKS_ALLOW_PRIVATE` unset/false in production; it exists only for isolated tests. EDI: `EDI_SFTP_ENABLED`, `EDI_POLL_SECONDS`, `EDI_OUR_UNB_ID`; per-partner `EDI_SFTP_PASSWORD_<PARTNER>`. AS2: `EDI_AS2_ENABLED`, `EDI_AS2_ID`, `EDI_AS2_KEY_PEM`, `EDI_AS2_CERT_PEM`, `EDI_AS2_KEY_PASS`; configure trusted partner certs and prove signed/encrypted MDN exchange. HTTP gateway signatures and replay protection remain enabled.

## Recovery, backups and release checks

For incidents, capture redacted job/transaction identifiers and UTC sequence, halt relevant automation, determine the first failed boundary, and reconcile external effects before retry. Keep the previous deployment revision and a restorable DB backup. A source rollback does not roll back data or provider actions. After recovery, prove one complete controlled business journey and one relevant negative case; then restore authorised switches deliberately.

Backup frequency, retention, encryption, restore point and recovery time are **not yet verified with Emergent**. Assign an owner to obtain those controls and run a restore test before commercial launch. Do not claim a backup exists solely because hosting is paid.

## Source coverage and remaining merge work

Read sources: all sections of `shared/interfaces.md`, `shared/capmap/CAPABILITY-MAP.md`, `shared/s14/README.md`, and s11 go-live documents for Tulli, NCTS/ICS2, AEO, TARIC, origin and controls. The task-tree README discovery also found release-evidence READMEs (`s1-ship-4013082`, `r4-ship-04af356`, `r5-ship-2ce7003`, `r6-ship-a86ad24`) and an EORI capture README; these establish historical evidence, not current activation. Other steps had no step README at discovery. Their declared variables are above and remain subject to merged-code reconciliation. `CONFIG-INVENTORY.md` is complete for the specified baseline, not future revisions.

The old capability map is a code audit, not a live acceptance record. s4 changes legal publication/documentation only; it does not upgrade customs, insurance, messaging or finance capability scores. Complete final-run evidence before upgrading those rows.
