# Finnish Customs official business schemas

- Import: MIG 2.1.7, internal XSD version 3.0.37, FI415B and response families.
- Export/AES: MIG schema release 1.1.1, internal XSD version 1.0.90, FI515A and response families.
- Download script: `scripts/fetch_tulli_specs.py`. Each release has URLs, dates, SHA256 and untouched XSDs/examples.
- Validate using `xmlschema`: the libxml2/lxml build in this environment rejects the official `\P{Cc}` Unicode pattern
  for ordinary text. Do not edit the authority's XSD to work around a validator defect.
- Some import authority examples do not match the current schema (e.g. example NetMass versus required NetNetMass
  spelling). These files remain unmodified; generated application messages use the current official schema.
- Runtime builder coverage is the normal import/export declaration subset, not all conditional MIG rules.
- Direct-message **transport schemas are not included**: the linked v1.21 ZIP returns Cloudflare HTTP 403 even with
  curl_cffi and headed Chromium. `test` and `production` gateways fail closed. See `notes/s11/golive-tulli.md`.
