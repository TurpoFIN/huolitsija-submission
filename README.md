# Huolitsija

A Finnish and English freight-forwarding application with customer, operations and partner portals. It combines quoting and booking, shipment tracking, documents and customs, billing, and an email-driven quote agent.

The application uses React, FastAPI and MongoDB. External integrations are configured separately; the repository includes no account credentials or customer database. Demo records are explicitly marked as demo data.

## Run locally

Install uv, Node.js 22, Yarn Classic 1.22.22, and Docker with Compose. uv supplies Python 3.11. Run commands from the repository root unless a different directory is shown.

```sh
docker compose up -d --wait mongo
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r backend/requirements.lock.txt
cp backend/.env.example backend/.env
```

Set `DEMO_OPS_PASSWORD`, `DEMO_CUSTOMER_PASSWORD` and `DEMO_PARTNER_PASSWORD` in `backend/.env` to passwords of your own choosing. A demo user is created only when its password is configured.

```sh
.venv/bin/python -m uvicorn server:app --app-dir backend --host 127.0.0.1 --port 8001
```

In another terminal:

```sh
cp frontend/.env.example frontend/.env
yarn --cwd frontend install --frozen-lockfile
yarn --cwd frontend start
```

Open `http://localhost:3000`. The development server forwards `/api` to port 8001. Demo logins are `ops.demo@huolitsija.fi`, `customer.demo@huolitsija.fi` and `partner.demo@huolitsija.fi`, with the passwords you configured. Password changes persist; reseeding does not reset them by default.

The first start may take several minutes while demo records resolve public geocoding, routes and customs references. Subsequent starts reuse the database. Set `SEED_DEMO=false` for an unseeded installation.

Email defaults to a database outbox (`EMAIL_PROVIDER=log`), payments have no configured provider, customs filing is disabled, and the autonomous public demo is off. Open-data services such as maps and geocoding can still make network requests. AI features require your own `OPENAI_API_KEY` and an accessible `LLM_MODEL`; without them, tasks that require AI remain unavailable or need review.

## Checks

With local MongoDB running and Python dependencies installed:

```sh
.venv/bin/python scripts/check.py
CI=true yarn --cwd frontend test --watchAll=false --runInBand
yarn --cwd frontend build
```

The curated Python check runs focused existing tests in separate processes, using the local MongoDB on port 27027. It covers public tariff parsing, AI configuration, requester admission, email reply provenance, and the Gmail transport contract. Google HTTP is simulated in these tests; no live email is sent. This is a bounded packaging check, not the complete integration suite. See [testing](docs/TESTING.md) for the wider suites.

## Repository guide

| Path | Contents |
| --- | --- |
| `backend/core`, `routes`, `services`, `adapters` | API, business logic, jobs, approvals and integrations |
| `frontend/src` | Customer, operations and partner interfaces, including FI/EN text |
| `frontend/vendor` | Current map package and its editable source |
| `backend/data` | Runtime reference data, official schemas and source provenance |
| `backend/tests` | Tests and explicit fixtures / captured public responses |
| `scripts` | Focused checks, reference-data maintenance and integration-test helpers |
| `shared/s4` | Original operational and legal runbook source, mirrored into the API |

Read the [architecture](ARCHITECTURE.md), [integration setup](docs/INTEGRATIONS.md), [data and dependency notices](THIRD_PARTY_NOTICES.md), and [export provenance](docs/REPOSITORY.md).

The operational runbooks describe the original deployment and its historical readiness; they do not establish that this local installation is ready for commercial operation. No open-source license has been selected for the application. Third-party material keeps its own terms.
