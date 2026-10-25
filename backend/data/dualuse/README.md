# Dual-use control data (Reg (EU) 2021/821)

Built by `scripts/build_dualuse_data.py --on 2026-10-24` on 2026-10-24 (UTC). Re-run the script when a new
consolidated version of Regulation (EU) 2021/821 or a new correlation table is published; it picks the newest
consolidated text in force on `--on` through the Publications Office SPARQL endpoint.

## Which text is in force on 2026-10-24

| | |
|---|---|
| Consolidated text | CELEX **02021R0821-20260921** (consolidated version of 21.9.2026) |
| EUR-Lex | https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02021R0821-20260921 |
| Fetched from | Publications Office Cellar, `http://publications.europa.eu/resource/celex/02021R0821-20260921` (English XHTML; eur-lex.europa.eu itself answers scripts with an AWS WAF challenge) |
| Source sha256 | `d29358c49f93a8672f10d1a32ae561a6a83934ab2a130652be29abba7ce6f1ea` (3 231 736 bytes; gzipped copy in `source/02021R0821-20260921.en.xhtml.gz`) |
| Annex I as amended by | Commission Delegated Regulation **(EU) 2025/2003** of 8.9.2025 (OJ L 14.11.2025, in force 15.11.2025), corrigendum OJ L 9.2.2026; Delegated Reg (EU) 2026/1405 (language-version corrections) |

The Commission adopted the 2026 update of Annex I on 14.9.2026 (DG TRADE news, 14.9.2026). It enters into force
only after the two-month scrutiny period and publication in the Official Journal; on 2026-10-24 it is **not** in
force, so the 2025/2003 list above is the applicable one. SPARQL on 2026-10-24 lists no newer amending act.

## Files

| File | Content | sha256 |
|---|---|---|
| `annex1.json` | Annex I: 384 entries (e.g. `5A002`) and 3 437 nodes incl. sub-items (`5A002.a.1`), each with category, group, parent, children, text, notes, technical notes, N.B., regime | `48a8f6e20ac5a5813c57a70753d7bdd91c4100c9a57bd0402a34036cb5732d66` |
| `annex2_eugea.json` | Annex II: Union general export authorisations EU001–EU008 (items, destinations as ISO codes, conditions text) + Section I (items excluded from EU001/EU003/EU004/EU007) | `6838d84655d73d9d5aef3de71aa95d9e1695b7b3f98a17b677a4d731cd4f27a4` |
| `annex4.json` | Annex IV: 70 items whose intra-Union transfer needs an authorisation (Part I / Part II) | `bc3ec7c96266460418c8fb1aec8d308586bb58c17f0dece2e3778b00d4e20150` |
| `cn_correlation.json` | Commission correlation table: 1 391 CN 10-digit codes → TARIC footnote `DUxxx` → Annex I code (all 6 212 rows resolve to an Annex I node) | `c8d9ca14334e082022e884aecdeefab3805637248fff16dccaa1b21325a8e499` |
| `embargo_destinations.json` | EU Sanctions Map regimes per country: arms embargo (country-wide / targeted / 1989 political declaration) and dual-use bans, with the regime's legal acts | `57ce795987e318194313311ad4847e22d23187b5c41a08677c47e67ab9931f65` |
| `source/2026 - Dual use correlation table 1 January 2026.xlsx` | the correlation table as downloaded | `7f9e6d94cbf4f3299bbfb3eca2f6fa3a319781faaee7c5724f2758e1c4b5fa2e` |

### Correlation table source
European Commission, DG TAXUD, CIRCABC library "Dual use codes" (linked from DG TRADE's page
"Exporting dual-use items" as "Dual-use correlation table"):
https://circabc.europa.eu/ui/group/0e5f18c2-4b2f-42e9-aed4-dfe50ae1263b/library/c3d06bd7-6ef0-4771-bbd7-f92b976ae9a0 —
file "2026 - Dual use correlation table 1 January 2026.xlsx" (modified 2025-12-31), download
https://circabc.europa.eu/rest/download/8dd0feca-0b12-494e-9ad3-a1a5565e56ad (guest access, no login).
The table is a support tool without legal force: a CN code listed there means the goods *may* be dual-use; whether
they are is decided by the technical parameters of the Annex I entry.

### EU Sanctions Map source
`https://www.sanctionsmap.eu/api/v1/regime` (fetched 2026-10-24, raw sha256
`6f33075926dada2b06bc40b67a3406bb86445e0d3658b4edc90c5400d206b93c`). Used for Art. 4(1)(b) of Reg 2021/821 (military
end-use in a country under an arms embargo) and EU008 Part 2(c). The 1989 China declaration is kept but flagged
`political_declaration` (it is not a Council decision/common position, so it does not trigger Art. 4(1)(b) by itself).

### Regime of an entry
Annex I does not state the regime per entry. `regimes` follows the numbering convention of the EU list as published by
national competent authorities (e.g. Croatian MFA, https://mvep.gov.hr/o-hrvatskom-izvozu/kontrola-izvoza/export-control/list-of-dual-use-items/245196):
category 0 → NSG; third character 0 → Wassenaar Arrangement, 1 → MTCR, 2 → NSG, 3 → Australia Group, 4 → CWC;
5 (`xx5xx`, new in 2025) → Member States' additional commitments accepted also as Wassenaar members (Delegated Reg
(EU) 2025/2003, recital 3).

## Parser checks built into the script
The build fails if Annex I has fewer than 300 entries, if EU001–EU008 are not all found, if Annex IV has fewer than 30
items, if a destination name cannot be mapped to an ISO code, or if the correlation table header changes.
