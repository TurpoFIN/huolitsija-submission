# Tulli export acceptance specimen

`export-declaration.json` is explicitly synthetic sandbox business input, not an official declaration or real customer.
It describes a newly manufactured Finnish ordinary pedal bicycle. Its declared exemption statements Y935, Y903 and
Y160 correspond to the real export requirements returned by the EU TARIC consultation for 87120030 → US, recorded
unchanged in `taric/` by `scripts/capture_tulli_export_taric.py`. The manifest records URL, timestamp and SHA256.
No control service is mocked to return success. Full official sanctions list captures are in `../controls/` and
parsed by the actual screening service. Official Tulli XSDs and authority examples are in `backend/data/xsd/tulli/`.
