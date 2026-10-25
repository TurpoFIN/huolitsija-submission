# TARIC reference data (official source)

| File | Source URL | Fetched (UTC) | sha256 |
|---|---|---|---|
| `measure_types_en.json` | https://ec.europa.eu/taxation_customs/dds2/taric/meas_type_combo/meas_typ_combo_en_20261024.js (the measure-type list of the EU TARIC consultation) | 2026-10-24T19:30:28Z | file `4024fcf580baff74dda6efe214e08f20cea853eb8a0a2eba0fd2cdf8d85bac5d`; source JS `2a37b887d5f13cceb660ba6eb1a00a043a9f0dbfbbd01196d616c77d5e3cc8e6` |

Produced by `scripts/taric_capture.py` (149 measure types: code, description, flow). `services/taric.py` loads the live
list of the day first and uses this copy only when the consultation cannot be reached.

The captured consultation pages used by `backend/tests/test_s11_taric.py` are in `backend/tests/fixtures/taric/`
(`manifest.json`: url -> file, sha256, fetched_at), written unchanged by the same script.
