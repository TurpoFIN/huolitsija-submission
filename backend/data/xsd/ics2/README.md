# ICS2 entry summary declaration schemas (official DG TAXUD XSDs)

`3.2.0.0/` = the **IE3** (economic operator ↔ ICS2 via the Harmonised Trader Interface) XML schemas of the ICS2
Common Technical System Specifications, EO package for ICS2 software release 3.2.0.0, plus the HTI v3.20 code lists.
Every IE3 message Huolitsija builds or parses (`backend/services/customs_messages/ics2.py`) is validated against these
files with lxml.

| | |
|---|---|
| Publisher | European Commission, DG TAXUD |
| Retrieved from | CIRCABC public interest group "EU Advance Cargo Information System (ICS2)" → Library → "ICS2 Common Technical System Specifications" → `ICS2 3.2.0.0 CTSS EO Package.7z` (modified 2025-11-27) → inner `ICS2 3.2.0.0-EO-TSC.7z` → `IE3-XSDs/V2`, `IE3-XSDs/V3`, `history.txt` |
| Package URL | https://circabc.europa.eu/rest/download/5c528a3e-b9b8-4e6f-82ac-272962a92514 (public, guest download) |
| Package sha256 | `483988a96d4da93a3fad97dbcd6df9d5c60e42920830e562569010a399164902` (2 630 367 bytes) |
| Code lists | `ICS2-HTI-v3.20-EO.zip` (folder "ICS2 Common Functional System Specifications", modified 2026-08-11), file `7. ICS2-HTI-CL-(2026-06-30)-v3.20.xlsx`, CIRCABC id `4b6801df-72b8-416a-9b11-14527480d86e`; zip sha256 `810e5852732706b98edd8434f9820d4c097754d9c88b85fc835fe2443398d1e2`; the lists used are extracted to `3.2.0.0/codelists.json` |
| Group | https://circabc.europa.eu/ui/group/18fb5859-3970-4ac5-b30b-6604977a15a7/library/16e29912-d290-417e-a45c-b286407e1770 |
| Retrieved | 2026-10-24 (see `3.2.0.0/SOURCE.json`) |
| File hashes | `3.2.0.0/SHA256SUMS` |
| Script | `backend/data/xsd/fetch_ncts_ics2.py --only ics2 --out <new dir>` (needs `pip install py7zr`, `openpyxl`) |

## Which namespace version is used

Namespaces are `urn:wco:datamodel:eu:ics2:2` (V2) and `urn:wco:datamodel:eu:ics2:3` (V3). DG TAXUD notes in the same library
("ICS2 Release management": *Decommissioning of older version of certain ICS2 messages*, Ares(2025)8334989 and the
final reminder of 14.1.2026): since **3 February 2026 only v3** is accepted for IE3F10, IE3A10, IE3F13, IE3A13,
IE3F17, IE3A17, IE3F34, IE3A34, IE3F40, IE3A40, IE3F41, IE3A41, IE3F45, IE3A45, IE3F50, IE3A50, IE3F51, IE3A51 and
IE3Q05. Every other message has only a v2 schema. `ics2.py` therefore files IE3F50/IE3F51 (road/rail complete dataset)
in v3 and the maritime house-level (IE3F14/IE3F15/IE3F16) and air (IE3F22/IE3F23/IE3F24/IE3F26) filings in v2.

The `history.txt` of the package records that the road/rail partial filings F52–F55 (and the HTI v3.20 road filings
F56–F59) were removed from release 3.2.0.0: they have specifications but no schema in production yet, so they are
not built.

The earlier copy of the same schemas published by the Austrian Federal Ministry of Finance (BMF, "ICS2 –
Spezifikationen", `IE3-XSDs.zip`, 1.4.2025) still contains the withdrawn F52–F55 v3 schemas; it is superseded by the
3.2.0.0 package above.
