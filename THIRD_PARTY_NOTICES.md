# Data and dependency notices

The application source has no selected open-source license. A new Git repository does not change the rights or licenses of incorporated material.

- JavaScript and Python dependencies retain their own licenses. The frontend's exact dependency resolution is in `frontend/yarn.lock`. The shipment map contains MapLibre GL JS (BSD-3-Clause), deck.gl (MIT) and Lucide (ISC); its source is included in `frontend/vendor/map-source` and the active bundle is retained as a package archive.
- The map displays OpenFreeMap/OpenMapTiles/OpenStreetMap attribution. Preserve that attribution and applicable map/data terms when deploying.
- `backend/data` contains source-attributed public reference material: customs nomenclatures, authority schemas and code lists, rules of origin, dangerous goods, trade controls, public geography, transport and payment specifications. Per-folder READMEs, manifests and embedded source fields identify the publishers and captures. Those materials retain the publishers' rights and terms; they are not relicensed as application code.
- `backend/tests/fixtures` contains explicit synthetic test inputs and captured public responses, with source manifests where supplied. The public tariff fixtures include publisher documents; their URLs and capture metadata are preserved in that folder.

Captured material represents a dated reference or test input. It is not a guarantee of current law, rates, service availability or authorization to transact.
