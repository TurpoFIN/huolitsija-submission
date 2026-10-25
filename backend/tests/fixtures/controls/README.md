# Official controls screening captures

Captured unchanged by `scripts/controls_capture.py`; gzip decompression returns the exact HTTP body. No synthetic subjects. The tests parse and index the entire feeds using the production engine.

| Feed | Fetched UTC | Official URL | Raw SHA-256 |
|---|---|---|---|
| eu | 2026-10-24T21:25:17.110204+00:00 | https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw | `636800c3e494b2ef8934f51f5e23c914a6208a383ac850e674b1b35a7d49d34f` |
| un | 2026-10-24T21:24:52.418798+00:00 | https://scsanctions.un.org/resources/xml/en/consolidated.xml | `dfb2a90b0e3a02193e4198b2c4a9eafeb04325469e48a80a8092650f6cf6dc76` |
| ofac | 2026-10-24T21:25:01.090772+00:00 | https://www.treasury.gov/ofac/downloads/sdn.csv | `96dade5a1cde8c9746937b4859ad75d8f2a34eed1a4046cc1f2240a830f70bda` |
| csl | 2026-10-24T21:24:53.344907+00:00 | https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.csv | `f8d7c6a4f37583ebd97a86f4bf4c1860bf0be0c00a6eeef2b540f186a80290cb` |
| uk | 2026-10-24T21:24:51.085876+00:00 | https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml | `8e71e2237d0dee3191d433f94ba6cec65ec76b5f0f3e9d2e42b4f3f992682281` |
| ofac_alt | 2026-10-24T21:24:59.582328+00:00 | https://www.treasury.gov/ofac/downloads/alt.csv | `e489fe41709a1d38693c16be8448d55a05cd4b39c9c2ffa98fbcc76bb5ec8050` |
| ofac_add | 2026-10-24T21:24:59.734271+00:00 | https://www.treasury.gov/ofac/downloads/add.csv | `2a76ba45cbd96ca22cb35e255c68ac9d6efba461dd85c056002592592c2bef4f` |

TARIC fixtures used by these tests are in `../taric/`, captured by `scripts/taric_capture.py` with their own manifest. Annex I/II/IV and the correlation workbook are under `backend/data/dualuse/`, with source and provenance there.

Run live verification with `CONTROLS_LIVE=1`. The live test refreshes all five lists from their official servers, screens Almaz-Antey as a carrier, verifies the legal decision in MongoDB, and fetches current TARIC Russia export measures. Offline tests load these official captures into a uniquely named disposable MongoDB database.
