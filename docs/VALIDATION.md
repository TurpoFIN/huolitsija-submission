# Initial export checks

Checked locally on 25 October 2026 with Python 3.11.15, Node.js 22.23.4, Yarn 1.22.22 and MongoDB 7.0.45:

- 82 selected backend tests passed in five isolated pytest processes, including the relocated and sanitized public tariff fixtures.
- All 263 existing frontend tests passed across 28 suites after including the matching frontend source revision.
- The frontend production build compiled successfully.
- Gitleaks reported zero unresolved source findings with the checked-in narrow public/test allowlist. Private environments, dependency installs, reports, database material and the original Git directory are excluded from the tracked tree.

These checks establish the curated source's tested packaging state. They do not establish live provider delivery, end-to-end autonomous freight operations, commercial readiness, or GitHub-hosted CI execution. Live demo acceptance continues separately in the original project.
