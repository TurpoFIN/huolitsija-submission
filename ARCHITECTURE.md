# Architecture

Huolitsija has one FastAPI backend (`backend/server.py`), one React frontend, and a MongoDB database. All API routes begin with `/api`. The frontend uses same-origin requests; `frontend/src/setupProxy.js` supplies the local proxy.

The backend groups HTTP contracts in `routes/`, domain logic in `services/`, external adapters in `adapters/`, and shared authentication, permissions, audit, configuration, jobs, approval and storage code in `core/`. Uploaded files are stored in MongoDB GridFS. Startup hooks register indexes and optional demo records; workers handle background jobs.

Customer access is scoped to a company. Internal staff and partners have separate permissions. The main domains are quoting and procurement, shipments and tracking, documents and customs, billing and finance, compliance, messaging and email intake. External commercial actions depend on configured integrations and the domain's approval rules.

The email quote demo is an optional subsystem in `services/demo_autoquote` and `services/demo_world`. Its simulated outside actors are separate from real requester email. Enabling it needs deliberate mailbox, recipient and sandbox settings; the local example leaves it disabled.

The frontend uses React 19, React Router, CRA/CRACO and Tailwind. Shared controls live in `components/`, domain panels in `features/`, route pages in `pages/`, and Finnish/English dictionaries in `i18n/`. The shipment map is a local package with editable TypeScript source and a prebuilt bundle. Only the current package archive is retained.

Official customs, origin, transport and payment schemas and data remain under `backend/data/`. Their manifests preserve source URLs and hashes. Tests also replay explicitly captured public responses. These archives are the main contributor to repository size; they are retained so verification and reference-data regeneration remain possible.
