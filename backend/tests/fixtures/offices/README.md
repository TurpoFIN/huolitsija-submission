# Official EU Customs Office List capture

URL: https://ec.europa.eu/taxation_customs/dds2/rd/compressed_file/data_download/COL-Generic-20261024.zip

Captured: 2026-10-24T21:58:46.492125+00:00

Snapshot date: 2026-10-24

SHA-256 (unaltered ZIP): `4c17d41a29581c54c7e186744cba4afe4ccf845ea846be0ec4452548bd62cc32`

4275 offices. Reproduce with `PYTHONPATH=backend python backend/data/customs_offices/fetch.py --out <new-directory>`.

The archive includes roles, traffic competence, seasonal timetables, addresses, status/version dates and UN/LOCODE. No coordinates are supplied in this capture. GeoInfoCode is a frontier/type code, not a coordinate. An absent activeTo is an open-ended office version, not a fabricated expiry. The dated extract is not a complete history of office versions.
