# Personal-data recipients — code and deployment evidence

Source snapshot d78217d + s4; live activation baseline from integrator probe on a86ad24, 24 October 2026 21:53 UTC. Runtime configuration and signed contracts must be checked again before commercial launch. No hosting region, processor legal entity or DPA has been inferred from a product name.

| Recipient / technology | Actual source | Data / role | Observed or conditional activation |
|---|---|---|---|
| Emergent hosting | Live host; backend/server.py, core/db.py; deployment handoff | App/database/documents/log hosting, processor role to be confirmed by agreement | Live host observed by integrator. Hosting region and onward processors unverified. |
| MongoDB | core/db.py (`MONGO_URL`) | Database software | MongoDB through Emergent; **not evidence of an Atlas contract**. Do not list MongoDB Inc. as a separate processor without actual hosting contract. |
| OpenAI | core/llm.py direct Responses API and config.py | Selected documents, prompts, messages, extraction/operational decisions | OpenAI reported available by live integrations API. Actual current model and vision path must be rechecked after model-fix lane; no remembered model ID used here. |
| Stripe | services/payments.py, routes/invoices.py | Payment contacts, amount, invoice reference; controller/processor roles vary by purpose | Live app uses test mode. Production onboarding, live payments and contractual role assessment remain outstanding. |
| Postmark | core/email.py, services/notify_channels.py | Recipient/sender, message body and delivery/bounce data | Supported; live email remains log at baseline. Account test/approved state is separate from adapter configuration. |
| Resend | core/email.py, services/notify_channels.py | Same email data | Supported alternative; no evidence currently selected. |
| SMTP/IMAP mailbox host | core/email.py, services/partner_network.py, services/crm.py | Mail content, headers, attachments; processor depends on actual mailbox host | Name the contracted mailbox host before enabling. Do not assume Gmail, Microsoft or another vendor from protocol support. |
| Twilio | core/sms.py, routes/channels.py | Phone, SMS text, status callback | Supported; live SMS log mode/disabled. Trial/test credentials do not prove delivery. |
| Redkik / actual insurer | adapters/insurer.py, services/insurance.py | Cargo/route/value/customer contacts | No active insurer on live. Sandbox adapter is unconfigured. Assess insurer/intermediary/controller role and distribution contract before live bind. |
| OpenSanctions | services/pep.py | PEP screening query, names | Optional paid API only when key configured; baseline activation unverified. |
| PRH/YTJ, Vero public data, VIES/EORI | services/kyc.py, services/credit.py | Business identifiers and validation | Register/authority sources; not automatically processors under an Article 28 contract. |
| PRH beneficial owner service, UK Companies House, GLEIF, Wikidata | services/beneficial_owners.py, services/pep.py | Ownership/PEP public data and query identifiers | Some sources need contract/key; inspect activation. Avoid claiming public parent-company data proves natural-person beneficial ownership. |
| Creditsafe / Asiakastieto | adapters/credit_bureau.py | Business credit query/report | Optional; no bureau active in live probe. |
| Carriers, agents, terminals, warehouse, Tulli, tax authorities | services/carrier_booking.py, tracking.py, tulli.py and domain routes | Shipment contacts, documents and statutory declarations | Recipients can be independent controllers. Sharing is limited to necessary services/duties and actual destination. |
| OpenFreeMap; OpenStreetMap Nominatim; Photon (komoot); OSRM | `frontend/vendor/map-source/src/ShipmentMap.tsx` lines77–78; `services/geo.py` lines44–47 | Map tile requests/browser IP; backend address or coordinate lookup | Actual default endpoint names read from implementation. Operator overrides can change backend recipients; verify configured host and terms. These are recipients, not assumed Article28 processors. |

## Required contractual register

For each enabled processor record its contracting legal entity, processing purpose/data, DPA version/date, subprocessors, locations, access controls, retention/deletion, breach contact and any Chapter V transfer mechanism/assessment. For independent controllers record purpose and legal disclosure basis. Obtain these from the actual contracts/configuration, not a guess based on headquarters. Public privacy wording deliberately distinguishes deployed infrastructure, optional adapters and separate controller recipients.
