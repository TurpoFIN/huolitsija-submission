# Configuration inventory — source snapshot

Extracted from runtime code at base d78217d plus s4. Environment names only: no secret values. See RUNBOOK.md for live transitions and branch-only variables. Source references locate the owning behavior.

### CRM

| Environment variable | Owning code |
|---|---|
| `CRM_EMAIL_FROM` | `backend/services/prospecting.py:311` |
| `CRM_EMAIL_PROVIDER` | `backend/services/prospecting.py:307` |
| `CRM_IMAP_FOLDER` | `backend/services/prospecting.py:670`; `backend/services/prospecting.py:689` |
| `CRM_IMAP_HOST` | `backend/services/prospecting.py:616`; `backend/services/prospecting.py:655` |
| `CRM_IMAP_PASSWORD` | `backend/services/prospecting.py:663` |
| `CRM_IMAP_PORT` | `backend/services/prospecting.py:656` |
| `CRM_IMAP_SECURITY` | `backend/services/prospecting.py:655` |
| `CRM_IMAP_USER` | `backend/services/prospecting.py:616`; `backend/services/prospecting.py:663` |
| `CRM_OPTOUT_SECRET` | `backend/services/crm.py:378` |
| `CRM_PROSPECT_DAILY_AT` | `backend/services/prospecting.py:926`; `backend/services/prospecting.py:938`; `backend/services/prospecting.py:960` |
| `CRM_PUBLIC_BASE_URL` | `backend/services/crm.py:398` |
| `CRM_REPLY_TO` | `backend/services/prospecting.py:354`; `backend/services/prospecting.py:355`; `backend/services/prospecting.py:372` |
| `CRM_SANDBOX_DOMAIN` | `backend/services/prospecting.py:299`; `backend/services/prospecting.py:921`; `backend/services/prospecting.py:922` |
| `CRM_SMTP_HOST` | `backend/services/prospecting.py:315` |

### Carriers and tracking

| Environment variable | Owning code |
|---|---|
| `DCSA_<SCAC>_API_KEY` | `backend/services/tracking.py:96`; `backend/services/tracking.py:112` |
| `DCSA_<SCAC>_API_SECRET` | `backend/services/tracking.py:113`; `backend/services/tracking.py:114` |
| `DCSA_<SCAC>_BASE_URL` | `backend/services/tracking.py:93` |
| `CARRIER_BOOKING_MODE` | `backend/services/carrier_booking.py:101` |
| `CARRIER_CONFIRM_HOURS` | `backend/services/agents/carrier_agent.py:16` |
| `DCSA_<SCAC>_KEY_HEADER` | `backend/services/tracking.py:112` |

### Company identity

| Environment variable | Owning code |
|---|---|
| `COMPANY_ADDRESS` | `backend/routes/legal.py:23`; `backend/core/config.py:44` |
| `COMPANY_BIC` | `backend/core/config.py:48` |
| `COMPANY_BUSINESS_ID` | `backend/core/config.py:41` |
| `COMPANY_EMAIL` | `backend/core/config.py:45` |
| `COMPANY_EORI` | `backend/core/config.py:43` |
| `COMPANY_IBAN` | `backend/core/config.py:47` |
| `COMPANY_NAME` | `backend/core/config.py:40` |
| `COMPANY_PHONE` | `backend/core/config.py:46` |
| `COMPANY_PRIVACY_EMAIL` | `backend/routes/legal.py:25` |
| `COMPANY_REGISTERED` | `backend/routes/legal.py:15` |
| `COMPANY_REGISTERED_OFFICE` | `backend/routes/legal.py:27` |
| `COMPANY_VAT_ID` | `backend/core/config.py:42` |

### Compliance and privacy

| Environment variable | Owning code |
|---|---|
| `PRH_BO_API_URL` | `backend/services/beneficial_owners.py:111` |
| `PRH_BO_CLIENT_ID` | `backend/services/beneficial_owners.py:112` |
| `PRH_BO_SERVICE` | `backend/services/beneficial_owners.py:114` |
| `PRH_BO_USER_ID` | `backend/services/beneficial_owners.py:113` |
| `PRIVACY_RATE_LIMIT_PER_EMAIL_HOUR` | `backend/routes/privacy.py:60` |
| `PRIVACY_RATE_LIMIT_PER_IP_HOUR` | `backend/routes/privacy.py:61` |

### Credit

| Environment variable | Owning code |
|---|---|
| `ASIAKASTIETO_BASE_URL` | `backend/adapters/credit_bureau.py:464` |
| `ASIAKASTIETO_PASSWORD` | `backend/adapters/credit_bureau.py:466` |
| `ASIAKASTIETO_USER_ID` | `backend/adapters/credit_bureau.py:465` |
| `CREDITSAFE_BASE_URL` | `backend/adapters/credit_bureau.py:409` |
| `CREDITSAFE_PASSWORD` | `backend/adapters/credit_bureau.py:411` |
| `CREDITSAFE_USERNAME` | `backend/adapters/credit_bureau.py:410` |
| `CREDIT_BUREAU` | `backend/adapters/credit_bureau.py:392` |

### Customs

| Environment variable | Owning code |
|---|---|
| `TULLI_CLIENT_CERT` | `backend/core/config.py:91` |
| `TULLI_CLIENT_KEY` | `backend/core/config.py:92` |
| `TULLI_DECLARANT_ID` | `backend/core/config.py:93` |
| `TULLI_ENDPOINT` | `backend/core/config.py:90` |
| `TULLI_MODE` | `backend/core/config.py:89` |
| `TULLI_SANDBOX_DECISION_SECONDS` | `backend/services/tulli_sandbox.py:22` |
| `TULLI_SANDBOX_RELEASE_SECONDS` | `backend/services/tulli_sandbox.py:21` |

### EDI

| Environment variable | Owning code |
|---|---|
| `EDI_AS2_CERT_PEM` | `backend/core/config.py:116` |
| `EDI_AS2_ENABLED` | `backend/core/config.py:113` |
| `EDI_AS2_ID` | `backend/core/config.py:114` |
| `EDI_AS2_KEY_PASS` | `backend/core/config.py:117` |
| `EDI_AS2_KEY_PEM` | `backend/core/config.py:115` |
| `EDI_OUR_UNB_ID` | `backend/core/config.py:112` |
| `EDI_POLL_SECONDS` | `backend/core/config.py:111` |
| `EDI_SFTP_ENABLED` | `backend/core/config.py:110` |

### Email

| Environment variable | Owning code |
|---|---|
| `EMAIL_FROM` | `backend/core/config.py:72` |
| `EMAIL_PROVIDER` | `backend/core/config.py:71` |
| `POSTMARK_SERVER_TOKEN` | `backend/core/config.py:73` |
| `POSTMARK_WEBHOOK_TOKEN` | `backend/core/config.py:78` |
| `RESEND_API_KEY` | `backend/core/config.py:74` |
| `RESEND_WEBHOOK_SECRET` | `backend/core/config.py:76` |

### Insurance

| Environment variable | Owning code |
|---|---|
| `INSURANCE_PROVIDER` | `backend/adapters/insurer.py:372` |
| `REDKIK_BASE_URL` | `backend/adapters/insurer.py:82` |
| `REDKIK_CLIENT_ID` | `backend/adapters/insurer.py:83` |
| `REDKIK_CLIENT_SECRET` | `backend/adapters/insurer.py:84` |
| `REDKIK_COMMODITY_ID` | `backend/adapters/insurer.py:86` |
| `REDKIK_COVER_TYPES` | `backend/adapters/insurer.py:88` |
| `REDKIK_POLICY_ID` | `backend/adapters/insurer.py:85` |

### OpenAI and legacy AI configuration

| Environment variable | Owning code |
|---|---|
| `ANTHROPIC_API_KEY` | `backend/core/config.py:52` |
| `EMERGENT_LLM_KEY` | `backend/core/config.py:51` |
| `LLM_MAX_CONCURRENCY` | `backend/core/llm.py:162` |
| `LLM_MODEL` | `backend/core/config.py:59` |
| `LLM_MODEL_FAST` | `backend/core/config.py:61` |
| `LLM_PROVIDER` | `backend/core/config.py:58` |
| `LLM_REASONING_EFFORT` | `backend/core/config.py:60` |
| `LLM_VISION_MODEL` | `backend/core/config.py:63` |
| `LLM_VISION_PROVIDER` | `backend/core/config.py:62` |
| `OPENAI_API_KEY` | `backend/core/config.py:56` |
| `OPENAI_BASE_URL` | `backend/core/config.py:57` |

### Partner sourcing

| Environment variable | Owning code |
|---|---|
| `PARTNER_EMAIL_FROM` | `backend/services/prospecting.py:311`; `backend/services/partner_network.py:272` |
| `PARTNER_EMAIL_PROVIDER` | `backend/services/prospecting.py:307`; `backend/services/partner_network.py:383`; `backend/services/partner_network.py:951`; `backend/routes/partner_network.py:27` |
| `PARTNER_IMAP_FOLDER` | `backend/services/partner_mailbox.py:79`; `backend/services/partner_mailbox.py:104`; `backend/services/partner_network.py:956` |
| `PARTNER_IMAP_HOST` | `backend/services/partner_mailbox.py:64`; `backend/services/partner_network.py:955`; `backend/services/partner_network.py:956`; `backend/services/partner_network.py:960` |
| `PARTNER_IMAP_PASSWORD` | `backend/services/partner_mailbox.py:72` |
| `PARTNER_IMAP_PORT` | `backend/services/partner_mailbox.py:65` |
| `PARTNER_IMAP_SECURITY` | `backend/services/partner_mailbox.py:64` |
| `PARTNER_IMAP_USER` | `backend/services/partner_mailbox.py:72`; `backend/services/partner_network.py:955` |
| `PARTNER_INBOUND_REQUIRE_AUTH` | `backend/services/partner_network.py:505` |
| `PARTNER_INBOUND_SECRET` | `backend/routes/partner_network.py:154` |
| `PARTNER_NETWORK_KILL` | `backend/services/partner_network.py:117` |
| `PARTNER_OPTOUT_SECRET` | `backend/services/partner_network.py:208` |
| `PARTNER_REPLY_TO` | `backend/services/partner_network.py:276` |
| `PARTNER_SANDBOX_DOMAIN` | `backend/services/partner_network.py:264`; `backend/services/partner_network.py:498`; `backend/services/partner_network.py:950` |
| `PARTNER_SMTP_HOST` | `backend/services/partner_network.py:384` |

### Payments

| Environment variable | Owning code |
|---|---|
| `BANK_WEBHOOK_SECRET` | `backend/routes/payments_ops.py:65` |
| `PAYMENT_WEBHOOK_SIGNING_SECRET` | `backend/core/config.py:68` |
| `STRIPE_API_KEY` | `backend/core/config.py:66` |
| `STRIPE_WEBHOOK_SECRET` | `backend/core/config.py:68` |

### Platform, operations and shared sources

| Environment variable | Owning code |
|---|---|
| `API_PROXY_TARGET` | `frontend/src/setupProxy.js:5` |
| `API_RATE_LIMIT_PER_MIN` | `backend/core/api_keys.py:49` |
| `APP_BASE_URL` | `backend/core/config.py:32` |
| `COMPANIES_HOUSE_API_KEY` | `backend/services/beneficial_owners.py:182` |
| `COOKIE_SECURE` | `backend/core/config.py:37` |
| `CORS_ORIGINS` | `backend/core/config.py:28` |
| `DB_NAME` | `backend/core/config.py:27` |
| `DEFAULT_PAYMENT_TERMS_DAYS` | `backend/core/config.py:106` |
| `DELIVERY_POD_WAIT_HOURS` | `backend/services/agents/delivery_agent.py:13` |
| `DEMO_OPS_EMAIL` | `backend/core/config.py:97` |
| `DEMO_OPS_PASSWORD` | `backend/core/config.py:98` |
| `DEMO_RESET_PASSWORDS` | `backend/services/seed_core.py:51` |
| `DHL_API_KEY` | `backend/services/tracking.py:190`; `backend/services/tracking.py:208` |
| `DIGITRAFFIC_ENABLED` | `backend/core/config.py:101` |
| `EXCEPTION_DETECTION` | `backend/services/exceptions_agent.py:386` |
| `EXC_REBOOK_HOURS` | `backend/services/agents/exception_agent.py:22` |
| `GEOCODER_URL` | `backend/services/geo.py:46` |
| `IATA_DGR_DATA_PATH` | `backend/services/dangerous_goods.py:56` |
| `INVOICE_AUTO_ISSUE` | `backend/services/billing.py:545` |
| `INVOICE_CUSTOMS_WAIT_HOURS` | `backend/services/agents/invoice_agent.py:14` |
| `JOBS_CONCURRENCY` | `backend/core/jobs.py:49` |
| `JOBS_POLL_SECONDS` | `backend/core/jobs.py:48` |
| `JOBS_WORKER_ENABLED` | `backend/services/monitoring.py:48`; `backend/core/jobs.py:50` |
| `JWT_SECRET` | `backend/core/config.py:35` |
| `MONGO_URL` | `backend/core/config.py:26` |
| `OPENSANCTIONS_API_KEY` | `backend/services/pep.py:373`; `backend/services/pep.py:385` |
| `OSRM_URL` | `backend/services/geo.py:44` |
| `OWNER_ALERT_EMAIL` | `backend/core/agent_runtime.py:350` |
| `PHOTON_URL` | `backend/services/geo.py:47` |
| `POSITION_TICK_SECONDS` | `backend/core/config.py:102` |
| `PUBLIC_QUOTE_RATE_LIMIT_PER_HOUR` | `backend/routes/public_quote.py:27` |
| `REACT_APP_BACKEND_URL` | `frontend/src/lib/api.js:9`; `frontend/src/features/map/ShipmentMap.jsx:12` |
| `SEA_AVOID_RED_SEA` | `backend/services/geo.py:45` |
| `SEED_DEMO` | `backend/core/config.py:96` |
| `SESSION_DAYS` | `backend/core/config.py:36` |
| `SIMULATOR_ENABLED` | `backend/services/simulator.py:24`; `backend/services/simulator.py:31` |
| `SIMULATOR_STEP_SECONDS` | `backend/services/simulator.py:25` |
| `TEST_TENANT_SANDBOX` | `backend/adapters/credit_bureau.py:57`; `backend/core/sandbox.py:14` |
| `TRACKING_WEBHOOK_SECRET` | `backend/routes/webhooks_feeds.py:27` |
| `TRADE_GOV_API_KEY` | `backend/services/screening.py:634` |
| `TRADE_STATS_SOURCE` | `backend/adapters/trade_stats.py:87` |
| `TRUSTED_PROXY_HOPS` | `backend/routes/public_quote.py:28` |
| `WEBHOOKS_ALLOW_PRIVATE` | `backend/services/webhooks_out.py:64` |

### Quotes

| Environment variable | Owning code |
|---|---|
| `QUOTE_APPROVAL_THRESHOLD_EUR` | `backend/core/config.py:105` |
| `QUOTE_CUSTOMS_FEE_VAT_CODE` | `backend/services/pricing.py:104` |
| `QUOTE_CUSTOMS_INCLUDED_ITEMS` | `backend/services/pricing.py:106` |
| `QUOTE_DOMESTIC_LEG_VAT_CODE` | `backend/services/pricing.py:103` |
| `QUOTE_ICS2_FEE_EUR` | `backend/services/pricing.py:105` |

### SMS

| Environment variable | Owning code |
|---|---|
| `SMS_PROVIDER` | `backend/core/config.py:81` |
| `TWILIO_ACCOUNT_SID` | `backend/core/config.py:82` |
| `TWILIO_AUTH_TOKEN` | `backend/core/config.py:83` |
| `TWILIO_FROM` | `backend/core/config.py:84` |
| `TWILIO_MESSAGING_SERVICE_SID` | `backend/core/config.py:85` |
| `TWILIO_STATUS_CALLBACK_URL` | `backend/core/config.py:86` |

## Persisted app settings

Stored in MongoDB `settings` document `key=app`; env defaults do not overwrite stored values. Set via the authenticated Settings API; autonomy switches require their dedicated endpoints.

| Setting | Source default |
|---|---|
| `autopilot_enabled` | `False` |
| `partner_network_enabled` | `False` |
| `partner_target_margin_pct` | `18.0` |
| `partner_max_price_cents` | `500000` |
| `partner_sell_price_cents` | `610000` |
| `partner_approved_lanes` | `[]` |
| `partner_daily_send_limit` | `20` |
| `partner_max_rounds` | `3` |
| `partner_rfqs_per_lane` | `3` |
| `partner_counter_anchor_pct` | `5.0` |
| `autopilot_quote_max_cents` | `1000000` |
| `autopilot_min_margin_pct` | `15.0` |
| `seller_business_id` | `config.COMPANY_BUSINESS_ID` |
| `seller_vat_id` | `config.COMPANY_VAT_ID` |
| `seller_iban` | `config.COMPANY_IBAN` |
| `seller_bic` | `config.COMPANY_BIC` |
| `quote_approval_threshold_eur` | `config.QUOTE_APPROVAL_THRESHOLD_EUR` |
| `default_margin_pct` | `18.0` |
| `customs_clearance_fee_eur` | `65.0` |
| `customs_extra_item_fee_eur` | `6.0` |
| `documentation_fee_eur` | `35.0` |
| `payment_terms_days` | `config.DEFAULT_PAYMENT_TERMS_DAYS` |
| `late_interest_pct` | `10.5` |
| `quote_validity_days` | `14` |
| `insurance_rate_pct` | `0.25` |
| `insurance_min_premium_eur` | `35.0` |
| `ai_auto_send_routine_messages` | `True` |
| `ai_reply_requires_approval` | `True` |
| `auto_approve_low_risk_kyc` | `True` |
| `vat_rate_standard_pct` | `25.5` |
| `vat_rate_reduced_pct` | `13.5` |
| `vat_rate_reduced_low_pct` | `10.0` |
| `record_retention_years` | `6` |
| `retention_financial_statements_years` | `10` |
| `retention_quote_unconverted_months` | `24` |
| `retention_kyc_years_after_relationship` | `5` |
| `retention_token_days` | `7` |
| `retention_privacy_request_years` | `3` |
| `terms_version` | `'1.1 · NSAB 2015 (PSYM 2015)'` |
| `sms_notifications_enabled` | `False` |
| `autopilot_halted` | `False` |
| `guard_spend_per_day_cents` | `500000` |
| `guard_confidence_floor` | `0.85` |
| `guard_claim_limit_cents` | `100000` |
| `guard_default_credit_limit_cents` | `500000` |
| `insurance_claim_limit_cents` | `0` |
| `owner_queue_sla_hours` | `24` |
| `owner_escalation_hours` | `4` |
| `owner_alert_email` | `''` |
| `customs_filing_delegated` | `False` |
| `customs_delegation_scope` | `'direct'` |
| `customs_confirm_timeout_hours` | `24` |
| `dunning_enabled` | `True` |
| `dunning_reminder1_days` | `7` |
| `dunning_reminder2_days` | `21` |
| `dunning_final_days` | `35` |
| `dunning_collection_days` | `45` |
| `dunning_fixed_compensation_cents` | `4000` |
| `dunning_reminder_fee_cents` | `0` |
| `rate_expiry_warning_days` | `14` |
| `crm_daily_send_limit` | `20` |
| `crm_follow_up_days` | `4` |
| `crm_max_follow_ups` | `2` |
| `crm_target_industries` | `['1623', '2229', '2511', '2599', '2829', '3109', '4669', '4673', '4675', '4690']` |
| `crm_target_regions` | `[]` |
| `credit_default_limit_cents` | `500000` |
| `credit_auto_limit_max_cents` | `5000000` |
| `credit_hold_overdue_days` | `30` |
| `insurance_provider` | `''` |

## Dynamic families (not literal constants)

- `CARRIER_<CODE>_{BASE_URL,BOOKING_URL,API_KEY,ENABLED,SANDBOX,PORTAL_ACCOUNT,SERVICE_CONTRACT_REF,CONTRACT_REF}`. Required subset depends on integration type (`services/carriers.py`, `services/carrier_booking.py`).
- `SMTP_{HOST,PORT,USER,PASSWORD,SECURITY}`, `PARTNER_SMTP_{HOST,PORT,USER,PASSWORD,SECURITY}`, `CRM_SMTP_{HOST,PORT,USER,PASSWORD,SECURITY}`; source `core/email.py` and channel reader.
- `EDI_SFTP_PASSWORD_<PARTNER>` is read by per-partner configuration; never store it in a partner record.
- Pending-step variables and settings are listed separately in RUNBOOK.md so a declaration in interfaces.md is not mistaken for a deployed feature.
