# Beneficial owners and PEP sources: availability and licence terms

Researched 2026-10-24 (s12 owners). Code: `backend/services/beneficial_owners.py`, `backend/services/pep.py`.

| Source | What it gives | Status in Huolitsija | Licence / cost | Env |
|---|---|---|---|---|
| GLEIF LEI API | LEI by Business ID, Level 2 direct / ultimate parent, reporting exceptions | **live** | CC0, free, no key | none |
| Wikidata | PEP default: positions held (P39) with dates, family (P26/P451/P40/P22/P25) | **live** | CC0, free, no key | none |
| YTJ open data API v3 | company form, register entries, situations | **live** (KYB, existing) | CC BY 4.0, free | none |
| OpenSanctions API | PEP / RCA matching (`/match/default`, topics `role.pep`, `role.rca`) | **config switch**, off | paid for commercial use | `OPENSANCTIONS_API_KEY` |
| UK Companies House PSC | persons with significant control of UK companies | **config switch**, off | free key, OGL v3.0 | `COMPANIES_HOUSE_API_KEY` |
| PRH beneficial owner register | owners of Finnish companies (names, shares, votes) | **adapter built, not obtainable today** | contract + AML-law purpose | `PRH_BO_API_URL`, `PRH_BO_CLIENT_ID`, `PRH_BO_USER_ID` |

## PRH beneficial owner register (tosiasialliset edunsaajat)

- After the CJEU ruling of 22 Nov 2022 (joined cases C-37/20 and C-601/20, WM and Sovim), the general public has
  no right to see beneficial owner data. PRH gives it only to users who have an **AML-law purpose (rahanpesulain mukainen
  käyttötarkoitus)**, stated on PRH's form (käyttötarkoitusselvitys). Sources:
  https://www.prh.fi/fi/yrityksetjayhteisot/tietopalvelut.html, the Virre käyttötarkoitusselvitys form
  https://www.prh.fi/material/sites/prh/forms/okDhEZWIg/bdc4v3m7o/PRH-Kayttotarkoitusselvitys_Virre.pdf
- Channels in 2026:
  - **Edunsaajatietopalvelu** (PRH's beneficial owner information service; since 27 Aug 2026 it is the only PRH
    channel; Virre no longer shows owners). You log in with Suomi.fi, and the organisation has to authorise you. It opened in
    phases: auditors (17 Feb 2026), lawyers (8 May 2026), estate agents (29 May 2026), banks and other financial firms on
    Finanssivalvonta's supervised list (19 Aug 2026). The data is chargeable for anyone who is not an authority. It is a
    web service with no public API.
    https://prh.fi/fi/tietoa_prhsta/uutislistaus/tiedotteet/2026/edunsaajatietopalvelu-finanssiala-19.8.2026.html
  - **X-Road (Suomi.fi-palveluväylä) interface for contract customers**: service
    `FI/GOV/0244683-1/xroadvirre-services`, operations `XRoadBeneficialOwnerCompanyRepresentInfoV2` (owners of a
    company by Business ID) and `XRoadBeneficialOwnerActiveRoleInfo` (companies of a person by personal ID code). PRH
    publishes the WSDL and the service description in the liityntäkatalogi:
    https://liityntakatalogi.suomi.fi/dataset/FI.GOV.0244683-1.xroadvirre-services ("Edellytyksenä liitynnän käytölle on
    Kansalliseen palveluväylään liittyminen ja sopimuksen tekeminen tietojen käytöstä PRH:n kanssa... Palvelu on
    maksuton viranomaisille"). Prices and the contract come from PRH at tietopalvelut(a)prh.fi.
    Stored copy: `prh/XRoadBeneficialOwnerCompanyRepresentInfoV2.wsdl` (fetched 2026-10-24, sha256
    `ca651efb1e64ede5651f7a76639eff7c3eda88a990e14ce31b23695b59fc0b81`, from
    .../resource/38ae69d6-ce07-4b37-9d8c-01e159247319/download/service.wsdl). Service description PDF 13.6.2023
    (sha256 `17cd98c98ac60e0dc813c5e272d7d0e2e6233f7118d05cd9325e76dc4d89f8a1`).
- Can Huolitsija get access? **Not today, as far as we can tell.** A freight forwarder or customs broker is not an
  obliged entity under rahanpesulaki 444/2017 1:2 §, so it has no AML-law purpose for PRH. PRH decides on a submitted
  käyttötarkoitusselvitys. AMLD6 (Directive (EU) 2024/1640) art. 12 adds a "legitimate interest" route for persons
  about to transact with an entity, but member states must transpose it by 10 July 2027. Check with PRH before
  you rely on this.
- Code: `PRHBeneficialOwnersAdapter` implements that WSDL (X-Road v6 SOAP headers client/service/userId/id/
  protocolVersion 4.0; body `businessId` + `userId`; parses `naturalPerson` firstname/surname/dateOfBirth/nationality/
  `beneficialOwnership` ownershipExtent/voteExtent/otherBeneficialOwnership and `noBeneficialOwners`). It needs your own
  X-Road security server endpoint `PRH_BO_API_URL`, `PRH_BO_CLIENT_ID` = `FI/COM/<Business ID>/<subsystem>`, and
  `PRH_BO_USER_ID` = the user ID PRH assigns. X-Road has no client secret, because the security server authenticates with its
  certificate, so `PRH_BO_CLIENT_SECRET` is not used. Without the variables the source reports `not_configured`. It never
  returns made-up owners.
  The parser has **not** been run against a real PRH response, because PRH publishes no example message for this
  operation. When access arrives, capture one real reply and add it as a test fixture.

## YTJ open data API v3 (https://avoindata.prh.fi/opendata-ytj-api/v3)

The OpenAPI spec at `/v3/schema?lang=en` is CC BY 4.0 (fetched 2026-10-24, sha256
`cac658d4d4cf5d2b5bd55cd7f41f2bd1741cbfc7323bccd03960f5d04181087e`). It has the paths `/companies`, `/all_companies`,
`/description` and `/post_codes`. A company has businessId, euId, names, mainBusinessLine, companyForms,
companySituations, registeredEntries, addresses, tradeRegisterStatus, status, registrationDate and lastModified.
**It has no ownership, no board, no managing director and no representatives.** The owners code reuses `services.kyc.ytj_lookup`
for company form and status and reports YTJ as `not_available` for ownership. Directors and representatives are only
available from PRH's paid trade register services (Virre / X-Road `XRoadCompanyRepresentInfo`, contract).

## GLEIF (https://api.gleif.org/api/v1)

The data is CC0 (GLEIF Golden Copy / LEI data are free of charge, without a key). Calls used:
`/lei-records?filter[entity.registeredAs]=<Business ID>&filter[entity.legalAddress.country]=FI`,
`/lei-records/{lei}/direct-parent-relationship`, `/ultimate-parent-relationship`, `/direct-parent`,
`/ultimate-parent`, `/direct-parent-reporting-exception`, `/ultimate-parent-reporting-exception`. These were verified live on 2026-10-24.
Onninen Oy (1071207-9, LEI 743700HUNZJ7XDH11G65) is IS_DIRECTLY_CONSOLIDATED_BY and IS_ULTIMATELY_CONSOLIDATED_BY Kesko Oyj
(0109862-8, LEI 743700OX6HSVMCAHPB95). Kesko reports exceptions with the reason NO_KNOWN_PERSON. GLEIF Level 2 is
accounting consolidation. It is not beneficial ownership (no natural persons).

## OpenSanctions (https://www.opensanctions.org/licensing/)

- The bulk data is **Creative Commons BY-NC 4.0**: free for non-commercial use only. "Any use inside a for-profit business
  requires a data license. Compliance screening is a commercial use even though it generates no revenue"
  (opensanctions.org FAQ). Free keys are issued only to academia, non-profits and journalists.
- The hosted API is billed per query from prepaid credits (2026-10-24 page: 2,000 free credits on sign-up with a business
  e-mail; 500 credits EUR 50, 5,000 EUR 400, 30,000 EUR 2,000, 100,000 EUR 5,000, 500,000 EUR 15,000). Flat-rate
  screening and OEM licences are priced by sales. **This is a paid item, and nothing was bought.**
- API: `POST https://api.opensanctions.org/match/default?topics=role.pep&topics=role.rca`, header
  `Authorization: ApiKey <key>`, body `{"queries": {"q": {"schema": "Person", "properties": {"name": [...],
  "country": [...], "birthDate": [...]}}}}`. Without a key the API returns HTTP 401 (probed). The OpenAPI spec was fetched on
  2026-10-24 (sha256 `d318014eedfb0f24733f0deff975ca5f096300450a92247cdd0ba34084c21ae9`).
- Code: it is used only when `OPENSANCTIONS_API_KEY` is set. Otherwise Wikidata is used, and the result lists
  OpenSanctions as `not_configured`.

## UK Companies House PSC register

`GET https://api.company-information.service.gov.uk/company/{number}/persons-with-significant-control`, with the API key as
the HTTP basic user name. The key is free (developer.company-information.service.gov.uk), the data is under the Open Government
Licence v3.0, and the limit is 600 requests per 5 minutes. Without a key the API answers 401 "Empty Authorization header"
(probed 2026-10-24). Code: it is used for companies whose address country is GB and that have `company_number` or
`registration_number`. Without the key it reports `not_configured`.

## Wikidata (default PEP source)

The data is CC0 and free. Calls: `wbsearchentities` (https://www.wikidata.org/w/api.php) and SPARQL (https://query.wikidata.org/sparql)
with a descriptive User-Agent (Wikimedia User-Agent policy). Class QIDs were verified live on 2026-10-24. The list is
`PEP_CLASSES` / `ORG_CLASSES` in `services/pep.py`, for example Q48352 head of state, Q2285706 head of government, Q14212 prime
minister, Q83307 minister, Q26204040 deputy minister, Q4175034 legislator, Q486839 member of parliament, Q27169 MEP,
Q1553195 party leader, Q117826617 supreme court judge, Q107363151 central bank governor, Q121998 ambassador and
Q5097014 chief of defence. Organisations: Q190752 supreme court, Q32766 constitutional court, Q66344 central bank,
Q43306178 supreme audit institution, Q7278 political party, Q270791 state-owned enterprise and Q245065 intergovernmental
organization. Local offices are excluded: Q708492 council member and Q30185 mayor.
Limits: coverage depends on Wikidata editors. National MPs, ministers and heads of state are well covered. Boards of
state-owned enterprises and senior military officers are thin. A "clear" result means no match was found in
Wikidata. It does not prove that the person is not a PEP.

## AMLD rule as implemented

AMLD (EU) 2015/849 art. 3(9) defines PEP functions and art. 3(10) family members. Art. 22 says the obligation lasts "for at least 12 months"
after the function ends, and until the person "is deemed to pose no further risk". Phases: `current`, `former_within_12m`
(enhanced due diligence is mandatory), `former_over_12m` (still status `pep`, because deciding that the risk has ended is a person's call),
and `undated`. A person with kyc.approve approves the relationship in KYC (legal point `pep_relationship`). An agent
cannot approve it.
