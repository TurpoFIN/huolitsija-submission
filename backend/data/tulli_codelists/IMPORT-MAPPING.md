# Ordinary H1 import: source → draft → FI415B → approval gate

Scope: domestic Finnish business imports for release for free circulation, IM/A,
procedure 4000/999, transaction-value valuation. The existing AEO, origin, sanctions,
TARIC and licence gates remain mandatory. This does not implement every exceptional
MIG procedure (centralised clearance, simplified or retrospective declarations,
warehousing, private-person or low-value declarations).

Official source: [Import MIG 2.1.7 data requirements](https://finnishcustoms-suomentulli.github.io/MIGs/Import/en/pages/datarequirements.html).
The corresponding `data.json`, constraints and code-list rows are pinned in this
directory, with full-download URL/date/SHA256 in README.md and sources.json.
Refresh: `python scripts/fetch_tulli_import_reference.py`.

## Required fields and source ownership

The table covers the required emitted FI415B groups and their required children,
plus H1 conditions stricter than the XSD. `IMP-XSD` checks the complete outbound
projection before approval: every child, attribute, count, string length and numeric
facet is checked against the same unmodified XSD used at submission. A structural
failure contains its XML path and schema reason; it cannot progress to filing.

| FI415B path (relative where grouped) | Draft/source | Missing or invalid gate; owner |
|---|---|---|
| Identification; FunctionalReference LRN | generated durable `next_ref('LRN')` | IMP-XSD; broker |
| Type DeclarationType/AdditionalDeclarationType | ordinary import workflow IM/A | IMP-SCOPE / IMP-XSD; broker |
| Language | configured workflow Finnish (`fi`) | IMP-XSD; broker |
| Submitter/Name; Communication/Identification, Type | shipment.customs_submitter, else configured COMPANY_NAME / COMPANY_EMAIL; email channel EM | IMP-SUBMITTER / IMP-XSD; agent |
| Agent/Function, Identification | company representation mandate; COMPANY_EORI | AEO, IMP-PARTY / IMP-XSD; broker |
| Declarant/Identification, Role | importer for direct representation; configured forwarder for indirect; company role Y | E-1/AEO, IMP-PARTY; broker |
| Importer/Identification, Role | company EORI; company role Y | E-1/AEO, IMP-PARTY; customer |
| Party Address/Line, CityName, Postcode, Country when sent | structured company/shipper address; invoice seller_address, seller_city, seller_postal_code, seller_country | IMP-PARTY / IMP-XSD; customer (exporter/importer), broker (others) |
| Exporter/Name, Address | invoice seller fields, then shipment.parties.shipper; no free-text address parsing guess | IMP-PARTY / IMP-XSD; customer |
| Invoice and CurrencyUnit | extraction.fields.total_amount/currency.value | IMP-INVOICE-TOTAL/CURRENCY; customer |
| AdditionalDocument SupportingDocument/Type, Identification | uploaded document type → official CL213; actual extracted number; N380 invoice, N271 packing list | IMP-INVOICE-REFERENCE, IMP-DOCUMENT-CODE/REFERENCE; customer/broker |
| BorderTransportMeans/Mode, RegistrationNationality | shipment mode; main leg nationality, vessel.flag, extracted vessel_flag/vehicle_country | IMP-BORDER-MODE/TRANSPORT-NATIONALITY (NC0031); broker |
| GoodsShipment/TransactionNature | shipment.transaction_nature or invoice extracted transaction_nature; never guessed from route | IMP-TRANSACTION (CL091, NC0208); customer |
| Destination/Country, ExportCountry/Country | shipment.destination.country and shipment.origin.country | IMP-COUNTRY (CL008); customer |
| Consignment/Container; TransportEquipment/Identification if containerised | actual shipment containers / extracted container_numbers | IMP-CONTAINERS / IMP-XSD; broker |
| ArrivalTransportMeans/Mode | actual arrival leg mode; identifier and identification type only if recorded | IMP-ARRIVAL-MODE / IMP-XSD; broker |
| TransportContractDocument/Type, Identification | document's actual type/number or carrier booking transport_doc_type/no; sea type never guessed | DOC-2, IMP-TRANSPORT-CODE (CL754); broker |
| GoodsLocation/Type and identity/address | explicit inspection/storage fields below | IMP-LOCATION-TYPE/IDENTITY; broker |
| TradeTerms/Condition, LocationName | invoice incoterm/incoterm_place or shipment terms | I-2 / IMP-XSD; customer |
| PresentationOffice/Identification | shipment.office_of_presentation, or recorded broker choice | C-2 / IMP-XSD; broker; no default office inferred from city |
| Goods item/Sequence | invoice line order | IMP-XSD; broker |
| Commodity/Description | extracted invoice line description | S-2 / IMP-XSD; customer |
| Classification HS/CN/TAR and IdentificationType | supplier's printed code, nomenclature confirmation + official TARIC declarability; full 10-digit printed code retained; TARIC lookup's declarable goods_code supplies 00 only when official lookup confirms it | H-1, TARIC-1/2, IMP-TARIC-CODE; broker |
| Commodity/InvoiceLine/ItemCharge, CurrencyUnit | extracted line.amount and invoice currency | IMP-LINE-AMOUNT/CURRENCY (NC0226); customer |
| Commodity/DutyTaxFee/DutyRegime | customs_origin assessment; 100 when no proven preferential origin | IMP-PREFERENCE (CL603), ORIGIN-PREF; broker |
| GovernmentProcedure/Current, Previous, AdditionalProcedure/Procedure | ordinary workflow 40/00/999; overrides validated, unsupported workflow blocked | IMP-PROCEDURE (NCL232), IMP-SCOPE; broker |
| CustomsValuation/Method; ChargeDeduction children if present | transaction-value method 1, actual quote/declared freight and insurance additions | IMP-VALUATION-METHOD, I-5 / IMP-XSD; broker |
| GoodsMeasure/GrossMass, NetNetMass; StatisticalValue | invoice/packing/transport weights; invoice converted with official FX + known freight/insurance | WEIGHT, IMP-NET-MASS, I-5 / IMP-XSD; customer/broker |
| Packaging/Type, Quantity, MarksNumbers | packing line/header, transport extraction or cargo; only known aliases map to CL017; unknown text stays missing | IMP-PACKAGE-CODE/COUNT, IMP-SHIPPING-MARKS (NC0209); customer |
| Item AdditionalDocument/Type, Identification | actual origin proof, licence register or declared supporting documents; Y references use function AdditionalReference and CL380 | IMP-DOCUMENT-CODE/REFERENCE, LIC-1, ORIGIN-PREF; broker |
| DomesticDutyTaxParty/Identification, Role when postponed VAT applies | company VAT registration/number, FR1 | IMP-VAT-ID / IMP-XSD; customer |

Optional groups not selected by this ordinary workflow are not emitted. If an
optional group is emitted, its required children are checked by the XSD preflight.

## DE 5/23: do not infer a place from the office

MIG GoodsLocation/Type explicitly reserves A for customs warehousing. For import
inspection at a customs office it prescribes D plus the actual address. The generic
CL347 and CL521 membership of A alone does **not** authorise its use in H1.

- `shipment.goods_location`: explicit `{type, qualifier, identification}` or
  `{type, address:{street,city,postal_code,country}}` is preserved.
- `shipment.temporary_storage.warehouse_identifier`: B + qualifier EO. Or build
  the warehouse identifier from **all three recorded** fields `eori`,
  `warehouse_code` (3 digits), `location_code` (2 digits). Never use naked EORI.
- `shipment.inspection_location`: an explicitly recorded C or D address, or C
  authorisation/EO identity. NC0035 allows EO for B/C; NC0036 allows Authorisation
  only for C; NC0037 allows addresses for A/C/D. A remains outside this H1 scope.
- A storage authorisation alone does not justify guessing a type; record the actual
  approved location type. A port container field is not an inspection place.
- NR0234 requires a location identity. Address country FI follows NR0019 for the
  supported domestic workflow. The address line contains the location name and
  street; the application never invents them from a city or company billing address.

## Duty in the request versus the response

FI415B's Commodity DutyTaxFee_419 admits DutyRegime, QuotaOrder and SpecificTaxBase,
**not TaxRate or Payment/TaxAssessed** (those belong to response types). Therefore
the tests check official TARIC duty in the stored declaration and sandbox decision,
and preference, HS/CN/TAR, item price and statistical value in the XSD-valid request.
No non-schema duty element is fabricated to satisfy a substring assertion.

## Lifecycle integrity

The review job performs current all-party screening even when drafting was invoked
directly rather than by document.extracted. Every new field contributing to the
message is in the approval fingerprint. The submit boundary re-runs recompute and
preflight. Licence consumption uses the same UTC tariff day as recompute, avoiding
a midnight-Helsinki discrepancy. Missing data yields bilingual, owned rules before
customs_filing approval. The sandbox creates SBX MRNs and simulated release only.
