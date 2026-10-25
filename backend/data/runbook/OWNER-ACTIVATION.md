# Owner activation actions

25 October 2026. This is the company-action checklist for Huolitsija. It does not certify provider activation. Last s4 production identity probe: r8a `fc211b4ec3514c733c719367881563c2f5858004`; provider activation and recorded journey are separate evidence gates. Reuse existing accounts and stored credentials. Only the integrator changes Emergent configuration.

## Postmark: account approval and sender authentication

**Owner action:** in the existing Postmark account, the Account Owner submits the approval request describing transactional freight quotes, booking updates and invoices, expected volume and recipient relationship. If already submitted, supply the information requested by the reviewer; do not create another account. In Sender Signatures/Domains, open heliosone.fi and have its DNS administrator publish the exact DKIM TXT and custom Return-Path CNAME generated for this account. Recheck both verification indicators. DNS authentication and account approval are separate gates. [Approval procedure](https://postmarkapp.com/support/article/1084-how-does-the-account-approval-process-work), [domain verification API](https://postmarkapp.com/developer/api/domains-api).

**Existing DNS action already on the board:** NS1 zone `heliosone.fi`, issue `issue_iicklrdl5uct557xuvaeug5jne`. Add TXT at `20261024191646pm._domainkey.heliosone.fi` using the exact public DKIM value in that issue/Postmark DNS Settings; add CNAME `pm-bounces.heliosone.fi` → `pm.mtasv.net`. Leave existing SPF and DMARC unchanged. The domain is already added in Postmark. s14 will verify and inspect a real receipt for `dkim=pass` and `dmarc=pass`; no duplicate request is needed.

**Integrator action:** retain the existing server token, select `EMAIL_PROVIDER=postmark`, use the verified sender, and configure authenticated inbound and delivery/bounce webhooks. For the email-channel lane, use its final merged endpoint/authentication contract and `EMAIL_CHANNEL_ADDRESS`; do not reuse an obsolete HMAC-only instruction or put a token into public evidence. Prove unauthenticated inbound rejection before enabling forwarding.

**Acceptance:** an allowed external mailbox receives the actual quote/invoice, its Message-ID correlates to the app, and the provider delivery event is recorded. Approval is needed for recipients outside verified domains. A controlled heliosone.fi-only journey can prove that limited scope while approval remains pending. A blackhole delivery or log row does not prove receipt. No new owner credential request is needed merely to read a verification email; coordinator relays sender/subject issues.

## Stripe: live business activation

**Owner action:** open the existing Stripe Dashboard business onboarding, enter the incorporated legal entity, Business ID, representative/beneficial-owner details requested by Stripe, company bank account and customer-facing support/statement details. Complete requested identity checks and accept the service agreement; resolve any outstanding requirements. [Stripe business verification](https://docs.stripe.com/get-started/account/set-up).

**Integrator action:** after service activation, provision the live key securely and register the live `/api/webhooks/stripe` endpoint with its matching signing secret. Check app `STRIPE_API_KEY` and webhook environment agree. Preserve test evidence separately.

**Acceptance:** an authorised real transaction has matching amount/currency, authenticated payment event, paid invoice, allocation and zero remaining balance. Until company activation, the specifically authorised test payment is a technical proof only; classify live-ready only if the implementation and round trip work and company activation is the sole remaining blocker.

## Tulli: customer status, form 934 and certificate

**Owner action:** obtain the company's EORI and representation mandates. In Tulli's Authorisations and Decisions Service apply for UTU message-exchange customer status for the relevant procedures and roles (message declarant and, if applicable, service provider/software supplier). Identify the sender and software. Arrange procedure-specific consultation/testing and any required deferment authorisation; the current introduction specifically names payment deferment for import message customer status.

**Form 934 distinction:** the older official Tulli notice directs certain other-system applications to customs form 934. It is a message-customer application, **not a certificate**. The supplied introduction dated 17 April 2026 directs UTU registration online and other-system applications through the current form/instructions (it specifically mentions Intrastat). Use the current procedure-specific application or obtain Tulli confirmation before submitting 934; do not apply an old ELEX/NCTS list indiscriminately to migrated UTU procedures. [Historical official notice](https://valtioneuvosto.fi/-/162752825/muutoksia-tullin-sanoma-asiointiin-5.11.2022?languageId=en_US).

**Certificate action:** the authorised company representative applies at [DVV e-services](https://asiointi.dvv.fi) for the certificate appropriate to Tulli message signing and client authentication. Have the technical custodian install the issued certificate and private key securely, record expiry and renewal responsibility, and complete signed connectivity testing. The supplied 25 June 2026 technical guide §10.1–10.2 specifies DVV application/installation and a one-year client certificate; notification/GSI server-certificate validity differs. Never publish the key, certificate binding or raw licensing/credential material.

**Integrator/customs action:** install `TULLI_CLIENT_CERT`, `TULLI_CLIENT_KEY`, `TULLI_DECLARANT_ID`, correct endpoint and procedure modes only after the actual transport passes. Run the agreed customer test cases and retain Tulli's registration decision and authorised production start date. Source: supplied `tulli-introduction.pdf`, printed pp.4–6; `tulli-technical-guidebook-2026-06-25.pdf`, §§10–12. The transport documents are already supplied; do not request them again.

**Implementation evidence:** the customs lane reports the existing v1.21 transport in `9258593` passed 40 protocol plus 5 exchange-durability checks; `9d7e95f` changes tests/evidence only. Company transport configuration and live authority acceptance remain unverified.

**Acceptance:** correlated authority acceptance/release for a legitimately authorised declaration. EORI, a local XSD pass or a certificate alone does not prove transport/customer conformance. Missing implementation remains partial; only a working integration blocked solely on registration/certification is live-ready. Customer POA and the owner's standing filing delegation remain separate from technical registration.

## Company bank agreement

**Owner action:** with the chosen company bank, sign a corporate payment/file-transfer agreement covering authorised debit accounts, payment limits, pain.001 submission and pain.002/camt.054 reports. For the implemented Nordea Web Services route, contact Nordea Business Centre, name the administrator, receive the agreement customer/user identifiers and securely enrol the company's PKI certificate using the bank's activation procedure. Arrange renewal and signing authority. [Nordea Web Services](https://www.nordea.fi/en/business/our-services/mobile-online-services/web-services.html).

**Integrator/finance action:** provision the agreed identity/certificate and bank endpoint, verify the bank response-signature trust configuration, and select the implemented API mode only after that check. A shared bank demo certificate must never be used as the company's production identity. Finance confirmed these production settings from `7ade205`: `BANK_PAYMENT_MODE=api`, `BANK_API_PROVIDER=nordea`, `BANK_API_CUSTOMER_ID`, `BANK_API_TARGET_ID`, and `BANK_API_ENVIRONMENT=PRODUCTION`. Supply `BANK_API_CERT` + `BANK_API_KEY` (`BANK_API_KEY_PASSWORD` if encrypted), or `BANK_API_P12` + `BANK_API_P12_PASSWORD`. Optional settings: `BANK_API_URL`, `BANK_API_SOFTWARE_ID`, `BANK_API_FILETYPE_PAIN001`, `BANK_API_FILETYPE_PAIN002`, `BANK_API_FILETYPE_CAMT054`, and `BANK_API_RESPONSE_CERT` (the public Nordea pin is bundled by default). `BANK_API_DEMO` must be false/unset. Keep `HUOLITSIJA_ALLOW_BANK_UPLOAD` unset and never run the bank-upload test.

**Implementation evidence:** finance reports response-signature verification closed in `23b358c`: bank certificate pin, signed timestamp, and request/customer/environment binding are checked before decoded content is used. An unverified upload response holds the run and blocks resend/cancel pending reconciliation. References: `evidence/s13-continuation/bank-signature-report.md` and `VAT-CONSUMER-HANDOFF.md`. Deployment and company payment remain unverified; this is not a new missing-code claim.

**Acceptance:** an authorised company payment is accepted by the bank and its genuine status/debit report reconciles once to the correct payable. A generated payment file or a downloaded exchange-rate file is not payment proof. Code/signature gaps remain partial even after the agreement is signed.

## Terminal49 paid access

**Owner action:** open the existing Terminal49 account and agree a paid plan with API read access and the webhook features the business needs; confirm quota, billing and permission to track the company's cargo with the account manager. Reuse the stored key; do not buy a second subscription. [Trial and paid transition](https://help.terminal49.com/articles/8907097858-terminal49-trial).

**Integrator/visibility action:** configure the existing key and authenticated webhook, then use one genuine bill of lading/container reference with customer authority. Previous real reads returned a paid-plan restriction; request-creation guards do not prove tracking.

**Acceptance:** actual provider read and correlated event appear on that shipment. First real request also requires a real shipment reference. A trial description does not establish this account's entitlement.

## AISStream access

**Owner action:** finish the existing GitHub-authenticated AISStream account access/2FA and create a server-side API key at [AISStream Account](https://aisstream.io/account/). Store it through the project's secure credential mechanism. This is an access step, not necessarily a paid contract; do not invent a purchase requirement.

**Integrator/visibility action:** set `AISSTREAM_API_KEY` on the backend, subscribe to an authorised area/vessel set and confirm actual stream messages. Keep keys out of browser connections. [Official stream documentation](https://www.aisstream.io/documentation/).

**Acceptance:** timestamped real vessel observations render on the deployed map with provenance and freshness. A vessel observation does not prove a particular shipment is aboard.

## DHL key activation

**Owner action:** in the existing DHL Developer Portal app, confirm access to **Shipment Tracking – Unified**, complete any account/application activation and ask DHL API support to resolve the stored key's 401 if it persists. Specify that product; Freight booking credentials are separate. [DHL Unified Tracking](https://developer.dhl.com/tracking?lang=zh-hant).

**Integrator/visibility action:** use the stored `DHL_API_KEY`; make one bounded read within the assigned limits and record the status without headers/key. Then use a genuine authorised tracking reference. The prior lane reported 401; recheck the latest lane evidence before treating it as current.

**Acceptance:** real tracking data enter the correct shipment and persist. A portal key being visible is not API activation.

## Carrier DCSA onboarding

**Owner action:** choose each actual contracted ocean carrier and request its production Track & Trace API onboarding/data entitlement, supported DCSA version, customer identifiers, authentication method, endpoints, quota and webhook arrangements. Separately agree booking authority and commercial rates if bookings are required. [DCSA Track & Trace](https://dcsa.org/standards/track-and-trace) is a standard, not a universal production carrier account.

**Integrator/execution action:** configure the matching carrier adapter and map real references; verify one carrier event and one booking confirmation where contracted. Keep DCSA reference-server fixtures labelled as protocol checks.

**Acceptance:** a real carrier acknowledges the authorised booking and sends associated tracking events. A conformant synthetic event does not prove onboarding, capacity or movement.

## Redkik contract

**Owner action:** contact [Redkik](https://redkik.com/) for the Finnish company's intended countries, cargo/modes, volumes and embedded-insurance role. Agree the actual insurer/intermediary arrangement, territorial/product limits, premium collection, delegated binding/claims authority, data terms and production API onboarding. Sign the resulting company contract; do not infer underwriting authority from a developer key.

**Integrator/commercial action:** configure the contracted provider credentials/endpoints and delegated limits. Check a genuine quote, authorised bind, policy/certificate and provider reference against the contracted product. Current evidence shows only adapter/refusal/test-double checks, so the capability remains partial until real integration behavior is proven.

**Acceptance:** insurer-backed offer/binding and delivered policy evidence. Owner contract alone cannot validate the adapter or confer authority beyond the contract.

## Creditsafe contract

**Owner action:** agree a Creditsafe Connect production licence for the required countries/reports, decision use, retention and sharing rights. Ask the account manager to activate the existing account and production entitlement; complete the Welcome Email activation. If that email is in marc@heliosone.fi, relay its sender/subject to the coordinator. [Connect onboarding](https://doc.creditsafe.com/connect-apis-catalog/product-catalog/connect-api-full/connectapi).

**Integrator/commercial action:** configure the selected credit adapter using the licensed production account. Confirm a real company report and timestamp feed the actual credit decision; preserve absent/unsupported fields as missing.

**Acceptance:** sourced bureau response with licence-permitted use and observed credit decision. No contract or real API-response evidence exists in the recovered baseline; keep partial, not automatically live-ready.

## Completion record

For every item retain date, responsible role, provider/account identifier without secrets, scope, contract/approval reference, configured release, genuine receipt and observed business result. Record company action and software gap separately. Existing sign-ins and authorisations are reused. Do not claim incorporation, a signed DPA, bank activation or provider production access from this checklist being filled in.
