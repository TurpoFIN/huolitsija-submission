# Finnish Customs transit MIG 2.2.0

Official Finnish Customs national external messages (FI015A, FI007A, FI044A,
FI014A and responses). Downloaded 2026-10-24 from the Customs-owned publication:
https://finnishcustoms-suomentulli.github.io/MIGs/Transit/common/2.2.0/schemas.zip

`2.2.0/SOURCE.json` records archive SHA256 and retrieval time; `SHA256SUMS`
records each untouched XSD. Reproduce using `backend/data/xsd/fetch_ncts_fi.py
--version 2.2.0 --out <new-directory>`.

The official version manifest at
https://finnishcustoms-suomentulli.github.io/MIGs/Transit/common/intro.json
labels 2.0.0 (2025-05-30) “NCTS phase 6”, and 2.2.0 (2026-10-01) “System development”.
Finland runs P6 on 2026-10-24; the Commission's 2026 implementation report also
lists Finland among the sixteen administrations that deployed P6 by end-2025:
https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52026DC0362
Do not infer the precise national deployment date from the EU deployment deadline.

The EU CCxxxC common schema is NOT the Finnish external interface. National
rules and customer conformance still apply beyond structural XSD validation.
