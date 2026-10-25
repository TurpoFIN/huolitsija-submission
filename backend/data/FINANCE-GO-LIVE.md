# Finance (s13) — what runs by itself, and the switch for each external system

Branch `feat/finance`. Functional currency EUR. Every agent action goes through `services.finance_core.agent_decide`
(s2 `core.agent_runtime.decide` once merged): audit row in `agent_decisions`, kill switch (`autopilot_halted`) and the
Autopilot switch respected, owner-queue card when a guardrail stops it.

## The loop with Autopilot on (no person)

| Step | Trigger | Agent / job | Person needed? |
|---|---|---|---|
| Expected cost per shipment | `shipment.created` | `finance.accrue_shipment` → `cost_accruals` from the booked quote option | no |
| Carrier invoice arrives | purchase-invoice inbox (IMAP) or upload | `finance.ap_mailbox_poll` (120 s) → `finance.carrier_invoice_process` | no |
| Read it | Finvoice 3.0 / PEPPOL UBL XML: deterministic; PDF/image: AI (`core.llm`, LLM_PROVIDER switch) | carrier_invoices | only when the model is unavailable or unsure |
| Audit + match | tolerance `ap_tolerance_pct` / `ap_tolerance_abs_cents`, IBAN change, duplicates, VAT plausibility | carrier_invoices | IBAN change and amounts above `ap_auto_approve_max_cents` |
| Dispute | over tolerance | owner card `carrier_invoice_dispute` + agent e-mails the carrier for a credit note | owner may override |
| Pay | daily 10:00 | `finance.payment_run` → pain.001 (ISO 20022 XSD-validated) → `agent_decide("approve_payment_run")` | see bank switch |
| Bank status | pain.002 / camt.054 debit | payables → paid, realised FX | no |
| FX | daily 17:30 | `finance.fx_refresh` (ECB), month end `finance.fx_revalue` | no |
| Collections | after s2 dunning `collection` stage | `finance.collections_sweep` 08:00 | write-off always |
| VAT period | 12th of the month | `finance.vat_period_close` → kausiveroilmoitus data + EU sales list | filing: see VAT switch |
| Bookkeeping | daily 02:00 | `finance.ledger_export` | see ledger switch |

## Switches (env) — sandbox/file default → live

| Env | Default | Live value | What going live needs |
|---|---|---|---|
| `AP_IMAP_HOST/PORT/USER/PASSWORD/SECURITY/FOLDER` | unset (sandbox: greenmail `ostolaskut@huolitsija.test`) | the company's purchase-invoice mailbox | mailbox on the company domain; `AP_INBOUND_REQUIRE_AUTH=true` |
| `AP_EMAIL_PROVIDER`, `AP_EMAIL_FROM` | `log` | `smtp` / Postmark | sender domain with SPF/DKIM/DMARC |
| `LLM_PROVIDER`, `LLM_MODEL`, `OPENAI_API_KEY` | (s3 switch) | `openai`, `gpt-6.1-sol` | key in Emergent secrets |
| `BANK_PAYMENT_MODE` | `file` (pain.001 download, uploaded in the corporate web bank) | `api` | bank agreement + `BANK_API_PROVIDER` keys/certificate |
| `SEPA_PAIN001_VERSION` | `pain.001.001.09` | `pain.001.001.03` if the bank still requires it | bank's channel spec |
| `BANK_SERVICE_CODE` | unset | bank-assigned payer service code | bank agreement |
| `LEDGER_EXPORT_FORMAT` | `procountor` | `netvisor` / `csv` | choice of accounting software |
| `LEDGER_API` | `off` | `procountor` / `netvisor` | vendor API credentials |
| `VAT_FILING_MODE` | `file` (Ilmoitin.fi file) | `api` | Vero API certificate / Suomi.fi authorisation |
| `COLLECTIONS_AGENCY` | `file` (hand-over CSV) | agency API (not built) | collection agency agreement |
| `LEDGER_ACCOUNTS_JSON` | built-in Finnish chart | the accountant's chart | accountant |
