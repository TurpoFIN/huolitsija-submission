# ISO 20022 payment schemas (official, unmodified)

Downloaded 2026-10-24 from the ISO 20022 message archive (iso20022.org, Registration Authority). Used by
`services/sepa.py` and `services/payables.py` to validate every generated pain.001 file and every pain.002 /
camt.054 file we ingest (lxml.etree.XMLSchema).

| File | Message | URL | sha256 |
|---|---|---|---|
| pain.001.001.09.xsd | CustomerCreditTransferInitiationV09 (default; EPC SCT C2PSP IG 2023, Finance Finland ISO 20022 Payments Guide 2025) | https://www.iso20022.org/sites/default/files/documents/messages/pain/schemas/pain.001.001.09.xsd | de038b373e47b0077b1832ddd81f4b2f1eb25d35721f62da1e38b7f5a09fda24 |
| pain.002.001.10.xsd | CustomerPaymentStatusReportV10 (status report for .001.09) | https://www.iso20022.org/sites/default/files/documents/messages/pain/schemas/pain.002.001.10.xsd | 2f9f8d0e9891fa9f31ccf0576397afe501614384d688ae6e43ba694b3d24b0cf |
| camt.054.001.08.xsd | BankToCustomerDebitCreditNotificationV08 | https://www.iso20022.org/sites/default/files/documents/messages/camt/schemas/camt.054.001.08.xsd | 13d220337d47e22cf25788807c136794a76955791df612c6947277272d440da6 |
| pain.001.001.03.xsd | CustomerCreditTransferInitiationV03 (legacy; still the version many Finnish bank channels take, SEPA_PAIN001_VERSION=pain.001.001.03) | https://www.iso20022.org/sites/default/files/documents/messages/pain/schemas/pain.001.001.03.xsd | 6bb5c6f24250ab807f31f6164142bafd6d43bad8d162a926e258ff4c11e128af |
| pain.002.001.03.xsd | CustomerPaymentStatusReportV03 (status report for .001.03) | https://www.iso20022.org/sites/default/files/documents/messages/pain/schemas/pain.002.001.03.xsd | dfb211e63f09825ea89f0ceee870db9e82ab2840fbe8b8148d9c4c2a9a99fd87 |
| camt.054.001.02.xsd | BankToCustomerDebitCreditNotificationV02 (what Finnish banks deliver today) | https://www.iso20022.org/sites/default/files/documents/messages/camt/schemas/camt.054.001.02.xsd | 7ff18dd9ceb927ea6576cc1c367b1e14aa10df3332b21629e1ca5d001fa4cada |

Usage rules followed (Finance Finland "ISO 20022 Payments Guide", 18 June 2025,
https://www.finanssiala.fi/wp-content/uploads/2025/08/iso-20022-payments-guide-2025-en.pdf, and the 2020 guide for
.001.03, https://www.finanssiala.fi/wp-content/uploads/2021/03/ISO20022_Payment_Guide.pdf):

- Finnish banks "currently use pain.001.001.02 and pain.001.001.03"; .09 per bank schedule. Version is a parameter
  (env `SEPA_PAIN001_VERSION`, default pain.001.001.09); both generate files that validate against the schemas above.
- Initiating party / debtor id: bank-assigned payer service code in `Othr/Id` with `SchmeNm/Cd = BANK`
  (env `BANK_SERVICE_CODE`); business ID (Y-tunnus, 8 digits, no hyphen) as a repeated `Othr` with `Issr = YTJ`.
- Creditor reference: `RmtInf/Strd/CdtrRefInf/Tp/CdOrPrtry/Cd = SCOR`; Finnish reference without `Issr`;
  RF creditor reference (ISO 11649) with `Issr = ISO`; reference written without spaces.
- SEPA: `SvcLvl/Cd = SEPA`, `ChrgBr = SLEV`. Non-SEPA / non-EUR: no SEPA service level, `ChrgBr = SHAR`.
- No Scandinavian letters, no leading `/` and no `//` in MsgId, PmtInfId, InstrId, EndToEndId.

Do not edit these files. To update, download the new version from the same archive and add a row here.
