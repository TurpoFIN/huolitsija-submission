# Testing

`python scripts/check.py` runs the focused packaging suite using Python's current interpreter. It refuses a missing local MongoDB on port 27027 and selects a fresh database namespace. Every module runs in a new process because existing Motor clients and several suites own their event loop and database configuration at import time.

The original test collection includes unit, parser, contract, database and authenticated HTTP suites. They are not a single uniformly isolated pytest suite: some expect a separate running backend, background workers, sandbox servers, fixture replays, or explicit opt-in. Do not point them at a deployed customer database. `scripts/run-ci-backend.sh`, `ci-local-env.sh`, `ci-execution-env.sh` and `ci-run.sh` retain the original wider-suite support; their profile settings need to match the suites selected. Bank/provider acceptance is a separate run.

Public surcharge captures were relocated to `backend/tests/fixtures/public_surcharges/`; test assertions and payloads are retained. Customs and compliance tests retain their official source captures and hashes. Local parser tests do not establish that an authority or provider will accept a live transaction.

Frontend checks use the existing Jest tests and a production build:

```sh
CI=true yarn --cwd frontend test --watchAll=false --runInBand
yarn --cwd frontend build
```

The separate map source has a locked npm dependency graph. Its prebuilt package is the application's dependency. To rebuild it, run `npm ci` and `npm run build:lib` in `frontend/vendor/map-source`, then pack and update the frontend's file dependency and Yarn lock together. The source-only export does not include the original map demo site or its browser-test harness.

`backend/requirements.txt` preserves the dependency inputs. The reproducible installation uses `backend/requirements.lock.txt`, which pins the resolved transitive versions; the oscrypto URL pins the required upstream revision.
