# NCTS transit message schemas (official DG TAXUD XSDs)

`60.4.16/` = the XML schemas of **DDNTA 6.4.1-v1.00 (SfA), Appendix X** (NCTS phase 6, XSD version 60.4.16, aligned
with DDNTA RFC-List.44). Every `CC0xxC` message Huolitsija builds or parses (`backend/services/customs_messages/ncts.py`)
is validated against these files with lxml.

| | |
|---|---|
| Publisher | European Commission, DG TAXUD (DDNTA = Design Document for National Transit Applications) |
| Retrieved from | Swedish Customs (Tullverket), "Technical specifications for NCTS phase 6": `DDNTA_APP_X_6.4.1-v1.00(SfA).7z` |
| Archive URL | https://www.tullverket.se/download/18.6967fe101964058a61b7a007/1752650865809/DDNTA_APP_X_6.4.1-v1.00(SfA).7z |
| Page | https://www.tullverket.se/foretag/ansokochdeklarera/deklareradigitalt/ediforsystemutvecklare/editekniskaspecifikationer/sctsnctsfas6.4.72334712192df55a6c0789b3.html |
| Archive sha256 | `46a350e57fcb90b7ea4ad3dab1ac245cb5fda76058f0fdc3fbca9837179d5e19` (90 938 bytes) |
| Retrieved | 2026-10-24 (see `60.4.16/SOURCE.json`) |
| File hashes | `60.4.16/SHA256SUMS` (all 129 `.xsd` files of Appendix X; the `RuleMappings/` folder is not copied) |
| Script | `backend/data/xsd/fetch_ncts_ics2.py --only ncts --out <new dir>` (needs `pip install py7zr`) |

## Why phase 6 and not phase 5

The task asked for "NCTS phase 5". NCTS-P5 is no longer in use in Finland:

* DG TAXUD, *Guidance on the entry summary declarations and transit declarations for road and rail transport in relation
  to the start of the deployment window for NCTS phase 6 and ICS2 release 3 step 3* (CIRCABC ICS2 library, folder
  "ICS2 and NCTS-P6"): the NCTS-P6 deployment window ran 1.3.2025–1.9.2025 for every national administration of the EU
  and the Common Transit Convention.
* DG TAXUD, *Guidance for the submission of an ENS for road and rail during the ICS2 and NCTS P6 derogation period*
  (same folder, annex): Finland (FI) — "Until 31/12/2025, ICS1 or ICS2; from 1/1/2026, ICS2", i.e. Finland runs
  NCTS-P6 **opt-out**: the transit declaration carries no safety-and-security data, and the ENS is lodged separately in
  ICS2 (which `customs_ens.py` does).
* The remaining derogations (HR, LV, PL, RO, SK) ended on 1.6.2026 ("End of temporary derogations for NCTS Phase 6 and
  link with ICS2 ENS filing", Commission ICS2 resources page).

The P6 messages keep the P5 message family and names (CC015C, CC028C, CC029C, …); the schema version (60.x vs 51.x)
and some data groups changed. The NCTS-P5 51.x schemas are therefore not committed.

## Other copies seen (not used)

HMRC's open-source `hmrc/transit-movements-validator` (`conf/v3_0/xsd`, "60.4.16") is **not** byte-identical to the
DDNTA files: HMRC re-numbered types for the UK (e.g. `HolderOfTheTransitProcedureType14` vs DG TAXUD's `Type23` in
CC015C) and adds ERMIS types. Use the DG TAXUD files above.

## National specifics

Tulli publishes national guidance (message implementation guide, national code lists, customer testing) on
tulli.fi, which is behind a Cloudflare human-verification page and could not be read by a script. The DG TAXUD schemas describe the common EU format, NOT the Finnish trader interface.
Tulli uses FIxxxA national messages; see `../ncts-fi/README.md`. National rules and
customer testing remain additional to XSD validation.

Correction (2026-10-24): the precise Finnish deployment date is not established by the EU deadline. P6 operation by end-2025 is confirmed by COM(2026)362; current national MIG is 2.2.0.
