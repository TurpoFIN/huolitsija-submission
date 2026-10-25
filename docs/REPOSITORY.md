# Repository provenance and curation

This is a standalone export prepared on **25 October 2026** from existing Huolitsija work. Its new root commit uses the requested history anchor, **24 October 2026 at 20:00 America/Los_Angeles** (`2026-10-25T03:00:00Z`). That timestamp is an export-history convention, not a claim that the application was first developed at that time.

Source baseline: `TurpoFIN/huolitsija`, revision `c73b997260d93eaec0ce17bfbccb494c01436409`. Frontend application source additionally comes from the complementary Emergent revision `51580a3c6bf4fa082ae98aeade8f1eccc3fcf90e`, which includes the public-flow route and matching deferred/recovery handling. This captures the recovered Gmail-demo working state; live application acceptance of that state was still in progress when exported. The original repository and its history are preserved separately. This repository imports no earlier Git objects or branches.

Retained: application code and interfaces, existing tests, explicit test fixtures, official reference data and schemas, the current shipment-map package and source, operational runbook source, and maintenance scripts needed by retained tests/data.

Excluded: development notes and handoffs, recordings, screenshots and test-result logs, obsolete map archives, accounting reference scratch captures, unused scraped documentation (including site telemetry and sample tokens), platform editor/health plugins, dependency installs, local environments, and the original Git history. Omitted documentation retains a hash/link inventory in `REFERENCE_SOURCES.json`. Public tariff captures needed by tests were moved from the old evidence directory into `backend/tests/fixtures/public_surcharges/`.

Packaging changes add a root README, concise architecture and integration guides, local MongoDB Compose service, safe configuration examples, a focused test runner and GitHub checks. The platform visual-editor dependency and injected editor script are removed. No product feature or domain rule was intentionally removed.

Fixture metadata omits expired signing queries from public download URLs. The Maersk public tariff capture has script elements removed; its original and curated hashes are recorded alongside it. The source scanner's narrow allowlist covers a public EU feed token, one geographic-name false positive and explicit CI-only password literals.

The application has no selected open-source license. Public visibility and licensing are separate decisions. Third-party notices and source attributions remain in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) and the relevant data folders.
