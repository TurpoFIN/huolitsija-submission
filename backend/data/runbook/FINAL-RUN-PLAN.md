# Final live-site run — executable script plan

Status: **plan only; journey not executed by s4 part 1**. Target `https://huolitsija-app.emergent.host`. Evidence root `/shardflux/project/tasks/task_biwogtmco6in3t5owytfysf4hm/evidence/s4/final-run/`.

## Preconditions and stop rules

Read `/api/version` immediately before and after each journey; record commit, frontend asset hash, timestamp, viewport and actor. If deployment changes during a journey, invalidate mixed-revision acceptance and rerun on one identity. Merge s2/s3/s5/s6 and required document/customs/carrier/finance work first. Inspect live settings, receipts and populated routes. Existing `scripts/e2e_autopilot.py`, `scripts/e2e_smoke.py`, `scripts/e2e_pay.mjs` and `scripts/sourcing_cycle_e2e.py` are helpers to adapt, not substitutes for the actual customer path.

Sign in as customer, owner and partner using the credential-store `sign_in` tool in their separate browser sessions. Do not copy secrets into scripts, arguments, screenshots or recordings. An earlier statement that demo passwords were already in the person's store was wrong. Report observed sign-in success or the concrete failure. Correct review reference: `evidence/s2-review/r2/REVIEW-r2.md`.

Inbox verification rule: a one-time code or verification link from marc@heliosone.fi is not a stored credential request and must not become a direct request to the person. Raise an issue to the coordinator with the exact sender and subject for relay. Do not click a Cloudflare or other human-verification challenge. Only genuinely missing stored secrets use a single-field secure request.

Final acceptance uses the real provider in production configuration. Each capability row uses exactly **live**, **live-ready**, **partial** or **missing**. `live` requires a real-service receipt and observed business outcome. `live-ready` means implementation works and only a named company step (incorporation, signed contract or payment) blocks live activation. Missing code, uncertain transport and unproven behavior remain `partial` or `missing`, never `live-ready`.

Provider test mode is allowed only where live operation requires a specific company-only step; name that prerequisite beside the result. The originally requested Customs sandbox and Stripe test-payment sequence remains a technical rehearsal, not the final acceptance endpoint. Obtain genuine mandates and controlled counterparties before the production sequence; no invented shipment, fake signature or simulated movement can pass as real freight. Do not charge a real card or contact arbitrary carriers simply to manufacture evidence. Production payment requires the corresponding authorised company account and transaction.

Required ordinary-path outcome: after customer submission and valid standing authorisations, the system advances all routine forwarder work without an employee/operator manually pushing status. The customer, carrier/sandbox driver, payment provider and legally required owner decisions are separate actors and must be counted. Do not silently approve an ops card to make an autonomous journey appear successful.

## Journey and actor table

| # | Trigger / actor | Real surface or event | Required observed result / proof |
|---|---|---|---|
| 1 | Customer | Public `/quote` → chosen lane/cargo/date | Numeric price and currency, included/excluded charges, mode, rate provenance and validity. Record quote ID and first usable result time. |
| 2 | Customer + KYC agent | Signup/claim quote, company KYC | Completed business identifiers, source checks, result and decision actor. Missing/sanctions/PEP cases hold correctly; not a generic green badge. |
| 3 | Customer | Accept quote / booking form | Confirm exact amount and terms version; shipment created once, link to quote retained. Capture duplicate click/retry behavior. |
| 4 | Customer + document agent | Real shipment documents | Upload invoice/packing list and mandated POA; extracted actual values, validation and generated required transport docs. No invented signature/certificate. |
| 5 | Authorised officer once, then customs agent | Standing delegation + customer POA; customs detail | Delegation actor/scope recorded; declaration submitted through the real configured authority channel, and correlated authority response/release recorded. If incorporation/authorisation alone blocks production, label the test result and exact company prerequisite. Missing transport implementation remains partial. |
| 6 | Carrier agent + contracted carrier | Carrier booking | Specific carrier/ref/route/contract, one request, correlated confirmation. A log-only adapter does not satisfy confirmation. |
| 7 | Customer | Actual shipment map, desktop and mobile | Same shipment/legs/locations, position time, ETA and provenance. Stale/missing position shown honestly; screenshot the populated route. |
| 8 | Actual provider event → exception agent | Signed tracking exception event | Observe a controlled, authentic provider delay/rollover once; exception appears, agent selects allowed action, customer notification actually reaches controlled inbox. Rebook if rule requires; preserve old/new booking IDs. |
| 9 | Carrier/provider | Delivery event + POD | Delivered state requires actual POD fields, timestamp and source. Simulator is labelled as such. |
| 10 | Invoice agent | Invoice detail + downloadable PDF | Issued invoice with correct registered/test seller identity, customer, taxable lines, totals, due date and reference; links to shipment. Missing identity stays a failure for commercial readiness. |
| 11 | Customer + Stripe | Checkout from actual invoice | Pay displayed amount/currency through the authorised production account. If live onboarding is blocked only on company incorporation/verification, perform the originally requested Stripe test payment and report live-ready blocked on that exact step. Capture no secret/card data. |
| 12 | Stripe webhook + reconciliation agent | Invoice/payment/reconciliation surfaces | Signed event recorded once, invoice paid, amount allocated, balance zero and ledger/reference links. Reload and confirm persisted state; duplicate webhook changes nothing. A redirect alone is failure. |
| 13 | Owner observes only | Autopilot decision/job/audit trail | Step-by-step actors show agent routine execution and identify any owner intervention. Count expected legal decisions separately. |

Record request IDs and business IDs in `steps.csv`: run_id, revision, step, UTC start/end, actor_type, actor_label, route, quote_id, shipment_id, invoice_id, event_id, expected, observed, pass_fail, screenshot, recording_timecode, intervention_reason. Do not include private bearer tokens or unnecessary customer personal data.

## Random city-pair coverage

At the beginning of the run draw at least **five** cross-continent pairs from the supported city catalogue, with a stored random seed and the candidate pool/hash. Select continents first to cover Europe–Asia, Europe–Africa, Asia–North America, South America–Europe and Oceania–Asia. Then choose a supported city in each continent by the seeded draw. Do not replace a failing lane silently; record all attempts and why any input was rejected.

Read the rate-card/lane query for each sampled pair and store a redacted proof of whether an exact seeded rate exists. At least **two** pairs must have no seeded rate. If the first draw does not include those, continue the draw and retain the rejected coverage candidates. Use coherent cargo/date/Incoterm data. Request quotes in the real public route (not direct DB inserts). Capture amount, currency, mode, line totals, validity, provenance, elapsed time and decision state for all five or more lanes.

A blank, dash, zero invented freight line, perpetual spinner or “none available” is a failure of the required priced-quote result. “RFQ pending” is a truthful interim result but does not pass a priced-quote requirement; follow the real partner request/reply through to an actual price. Indicative estimates must remain labelled estimates and cannot be reported as confirmed carrier tariffs.

## Partner-network cycle

1. Record both autonomy switches, approved lanes, daily send limits, negotiation bounds, global halt and actual recipient routing. Confirm PARTNER_SANDBOX_DOMAIN is unset for the production cycle.
2. Enable Sourcing with typed `enable`. Choose a lane needing capacity and trigger/observe the scheduled cycle.
3. Observe discovered candidate + public source + eligibility; actual outbound RFQ arrives at a controlled partner mailbox. Stored outbox alone does not pass delivery.
4. Partner actor replies with a dated rate, currency, equipment/mode, route, inclusions, validity and conditions. Observe ingestion, source message, parsed fields and OpenAI trace through the actual provider.
5. Observe negotiated counteroffer where applicable, partner acceptance, accepted rate card, carrier record and provenance. A new customer quote must consume that rate rather than an old seeded tariff.
6. Exercise opt-out and confirm no follow-up reaches that recipient. Turn sourcing off and demonstrate queued sends stop; keep global halt/guardrail cases in a separate negative-run recording.

## Email-only journey to paid invoice

Use a controlled real external sender mailbox and approved production delivery, with CRM_SANDBOX_DOMAIN/PARTNER_SANDBOX_DOMAIN removed only for their corresponding authorised cycles. Start with a new natural-language RFQ plus attachment, in Finnish or English; no portal login by the customer. Preserve mailbox message IDs/headers with addresses redacted for public evidence.

Email RFQ → delivered priced reply → customer booking reply → secure KYC/POA link and completed validation → documents submitted by email or secure link → real Customs response + carrier confirmation (provider test only for a named company-only prerequisite) → email tracking/exception/delivery → delivered invoice and Stripe payment link → customer payment (test only for a named company-only prerequisite) → delivered paid receipt. Secure one-time KYC and Stripe-hosted payment pages are allowed; portal-account navigation or manual ops advancement is not. Test same-message replay and a different sender attempting to hijack the thread. Neither may create a second booking/payment or reveal another customer's records.

## Recording and screenshots

Capture continuous browser video of the customer route and the key owner/partner observations. Keep a second machine-readable event timeline; elapsed provider waits may be trimmed in an edited presentation only if time jumps are labelled and the full recording is retained. Use actual site renders, never fabricated composites. Hide secret input and credential-store interactions; start recording after sign-in, noting that boundary.

Save `customer-journey.webm` (or MP4), `partner-cycle.webm`, `email-only.webm`; numbered PNGs for quote, KYC, booking, documents, customs response, carrier confirmation, map, exception, POD, invoice, Stripe confirmation and reconciliation. Include 1440px and 390px customer views, plus language variants relevant to legal pages. Include the final invoice PDF and redacted event/actor table.

## Acceptance and report

Each named output needs a concrete value and observed behavior. Document legal/owner holds as holds; do not claim “no humans” merely because the app is online. Summarise employee/operator interventions, customer actions, carrier actions, legal decisions and sandbox-driver actions separately. The ordinary journey passes only if routine forwarder transitions happen autonomously and financial reconciliation is observed.

Report revision, routes, exact monetary values, IDs, delivery receipts, screenshots, video/timecodes and per-step pass/fail. Mark unexecuted evidence layers **unverified**; capability status is partial/missing unless the precise live-ready condition above is proven. A sandbox result is never a final capability status. Do not seal final done from this plan. s4 part 1 can be sealed only after legal pages are live and the runbook/legal matrix are in `shared/s4/`.

## r8 recording execution scope (coordinator clarification, 24 October 23:25 UTC)

The next recording starts only after the integrator reports r8 live. Use the actual customer email (s6), automatic quote (s5), customer acceptance, booking creation, the single real partner RFQ (s3) and invoice/payment link (s13). No real cargo is supplied. Label pickup, tracking and delivery with their actual capability status; do not inject physical events or turn an acceptance-test request into a dispatch order. This is a recording of the available real digital path, not a claim that physical freight moved.

The coordinator relays customer mail through its existing Gmail session. The read-only IMAP attempt using the existing app password was rejected; no repeated login or new credential request is authorised. Postmark DNS and account approval remain separate gates described in OWNER-ACTIVATION.md. s6 owns the trigger and supplies exact Message-ID/thread/quote/shipment/invoice references. Wait for s4's recording-start notice before sending.

Ops authentication reuses the existing credential-store browser session for `ops.demo@huolitsija.fi`. The coordinator confirmed the named environment password is stale: do not retry API password login or request another credential. API observation may reuse that browser session cookie only in memory; never print or persist cookies, authentication state or secrets. Authenticate before recording and dismiss credential dialogs before capture.

The observer must not approve cards, run agents, change settings, issue invoices or forge events. Autopilot is enabled by the established pre-run configuration, observed at start and end. Every business transition is attributed to customer, app agent, carrier/provider or owner. A zero-write observer alone is insufficient to prove zero owner actions: reconcile the server audit/job timeline for the same run IDs and interval. Any manual advance fails the zero-owner criterion.

Retain a continuous WebM from the deployed browser, per-step screenshots, before/after `/api/version` and frontend asset identities, timecodes, a redacted actor table and an explicit missing-step list. If a stage is held, record the hold and its reason. Do not present a prepared script, old video or app route load as the requested recorded journey.

Recorder: `scripts/s4-record-observer.cjs` uses Playwright video on the actual deployed app. Set `EXPECTED_SHA` to the observed r8 commit and `EVIDENCE_DIR` to a new final-run directory; set `BROWSER_CDP_URL` to the existing signed-in browser debugging endpoint and reuse its app cookie only in memory (no password login). Run under a 60-minute command timeout in a background terminal with stdin available. It exits by stdin `{"stop":true}` or its own 55-minute limit, closes its browser and writes the WebM plus JSON timeline. Commands such as `{"route":"/ops/autopilot"}` and `{"scroll":600}` only observe. The script rejects a different deployment, disabled/halted Autopilot and login redirects. It blocks browser writes and never labels the journey passed. Customer/provider effects run through their normal channels in the coordinated s6/s3 lanes. Record the customer mailbox separately through the coordinator's existing session.

Recorder validation in this continuation is syntax-only; execution on r8 is still pending. Do not run it against the old release to imply final-run acceptance.

Coordinator gate: keep Autopilot off during preparation. Only the integrator enables it after exact r8 deployment and the s3 single-RFQ guard are verified, immediately before recording, and notifies s4. Prospecting/sourcing outreach remains log-only. s4 never toggles Autopilot.

25 October master gate: road Step 7 owns legal pages, this runbook PDF and the recording; wait for the master to confirm that the s8 EUR.1 test is finished and Autopilot may be enabled, in addition to the exact revision/webhook/single-RFQ checks above.

25 October s6 coordination: use existing test tenant `e19d517b-7637-49e9-8403-97fe63db7728`, sender `marc+huolitsija-email@heliosone.fi`, and explicit `test_tenant=true`; do not create another tenant or change Autopilot settings. Authentication stays in the standing browser session, with app cookies used only in memory and never shared through evidence files. No recording or email trigger before the exact final r8, webhook readiness and master clearance gates above.
