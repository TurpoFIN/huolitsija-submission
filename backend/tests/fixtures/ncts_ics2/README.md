# NCTS/ICS2 test data

`samples.py` contains explicit **synthetic sandbox** shipments and parties. They
are not captured Customs decisions and must never be submitted to production.

`official-fi/` contains unchanged examples from Finnish Customs' national
transit MIG 2.2.0, downloaded on 2026-10-24. Each URL and SHA256 is in
`official-fi/SOURCE.json`. Reproduce with:

```
python scripts/capture_ncts_examples.py --out <new-empty-directory>
```

Official FI015A, FI007A and FI044A examples validate against the current XSDs.
Several examples are older than the schemas: FI028A has a date with a timezone
and fraction rejected by the current date pattern; FI014A also omits Submitter;
FI043A contains whitespace before its XML declaration. The files are preserved
unchanged. Tests explicitly verify rejection of stale FI028A. When a test builds
a corrected FI014A it supplies a synthetic submitter and an explicit compatible
date; this is not described as a captured or accepted authority message.

The lifecycle tests use real MongoDB, agent decisions, jobs, event handlers and
XSD validation. Only sanctions-list retrieval/name matching is replaced by a
controlled boundary double; this does not prove live sanctions clearance.
