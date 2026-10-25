# s4 part 1 — legal pages and operating handoff

- Public routes: `/terms`, `/privacy`, `/dpa`, `/cookies`, `/company`, all Finnish/English with existing theme and navigation controls.
- Public identity: `GET /api/legal/config`; explicit allowlist, no keys/bank details/internal settings. Existing company env and seller-ID settings populate it; new `COMPANY_REGISTERED`, `COMPANY_REGISTERED_OFFICE`, `COMPANY_PRIVACY_EMAIL` are documented in RUNBOOK.md. Missing incorporation details remain visibly missing.
- [Runbook](RUNBOOK.md), [full configuration inventory](CONFIG-INVENTORY.md), [actual processors/recipients](PROCESSORS.md), [human legal responsibilities](LEGAL-HUMAN-POINTS.md), [final-run plan](FINAL-RUN-PLAN.md).
- [PDF delivery](HUOLITSIJA-RUNBOOK.pdf) includes all five documents. Incorporation, contracts, provider approvals and final E2E execution are not performed by this part.
- Evidence is in `evidence/s4/`. Local tests: 123 frontend, 2 legal-route and 2 security-header tests; actual local signup records legal version 1.1. Local renderer checks 64 states plus four interactions. Build exit 0 with existing CSS minimizer warnings.
- Terms version is 1.1. Preserve earlier contract records; original version 1.0 source is archived here. Update persisted app `terms_version` for new quotes after deployment if it remains 1.0; do not rewrite historical documents.
- Part-landed acceptance requires live legal page proof and shared documents; local checks alone do not seal it. Final-run plan is not a completed autonomous journey.
- Corrections: `evidence/s2-review/r2/REVIEW-r2.md` is the review path. Earlier password-store claim was wrong; demo sign-in must use the credential store and be observed.

Continuation: [Exact owner actions](OWNER-ACTIVATION.md) and recording scope are updated. DPA version1.0 adds processor-specific contract terms; signed parties/annex remain prerequisites. See `evidence/s4-continuation/HANDOFF.md` for local proof and deployment gates.
