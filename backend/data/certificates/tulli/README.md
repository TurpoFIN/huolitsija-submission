# Tulli server trust

Public DVV CA certificates fetched over authenticated HTTPS from the official DVV FineID directory. SOURCE.json records source URLs, fingerprints and validity. These are public trust-chain certificates, not client credentials.

Tulli technical guide 25 June 2026 section 10 requires DVV CA certificates. The platform root store did not trust the actual test server. The production endpoint also omitted the G5R intermediate, so both the root and intermediate are bundled. Only the Tulli transport loads these certificates; hostname and chain verification remain enabled, minimum TLS 1.2. Updates must come from the authenticated official directory and preserve provenance.

Read-only repeatable probe (from repository root):

```sh
PYTHONPATH=backend python scripts/tulli_server_trust_probe.py
```

The 24 October 2026 probe verified both official endpoints using TLS 1.3. It sends no SOAP or declaration and proves neither client authentication nor Tulli customer-test acceptance. Those still require the company DVV client certificate, assigned identifiers and Tulli customer status through Authorisations and Decisions.
