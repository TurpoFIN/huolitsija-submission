# Nordea response verification evidence

Captured from the documented public demo GetUserInfo operation at 2026-10-24T22:44:31Z. This is a real bank response for public customer11111111, not generated XML. No payment or upload was performed. The unsigned test clock is fixed to the capture time for deterministic replay; the runtime uses current UTC.

Response SHA256: 8b68fc0ea3e8beeef54773eccf6ed7d3c4eb5b0c7f1e4663272ecae430c909d1

The trusted signing certificate was independently fetched from Nordea's [published certificate ZIP](https://www.nordea.fi/Images/147-549979/File%20Transfer%20Web%20Services%20sha256%202025.zip), linked from [Testing instructions](https://www.nordea.fi/en/business/our-services/mobile-online-services/testing.html). Certificate DER SHA256: 7a52957c9010a6b6b40bda1012d4da6eb8aa9233806322af5ac39a0f11686d79. Valid2025-10-02 through2027-10-02. It is a public verification certificate and contains no private key. Rotate the pinned certificate from the bank's authenticated publication before expiry; do not trust a certificate just because it is embedded in a response.

Both SOAP (Body + Timestamp) and ApplicationResponse signatures verify using RSA-SHA256 and SHA256 digests. Parsing consumes only verifier-returned signed nodes. Request ID, operation and customer/target IDs must match; invalid or absent signatures, duplicate IDs/bodies, unexpected references/transforms, XML entities and expired timestamps fail closed. Other banks require BANK_API_RESPONSE_CERT pointing to an independently trusted PEM certificate. Trust is checked before network transmission.

The earlier claimed bank canonicalization quirk was instead traced to in-tree default-namespace canonicalization: serializing/reparsing each subtree with its namespace map removes libxml2's spurious namespace resets without editing canonical bytes, stripping whitespace, weakening digest checks or trying alternate signatures. Ancestor xml:* attributes are rejected in this restricted WS profile. See [Nordea security specification](https://www.nordea.fi/Images/147-163432/web-services-security-and-communication-description-2024v2.pdf) and [SignXML signed-data guidance](https://xml-security.github.io/signxml/).

