# Official TARIC captured responses

Source: https://ec.europa.eu/taxation_customs/dds2/taric/ . Each response's exact URL,
capture timestamp, SHA-256 and byte count are in `manifest.json` beside these files.
The test fixture checks every hash before replaying the HTTP response through the production parser.
No HTML page or duty value here is synthetic.

Capture: `PYTHONPATH=backend /home/user/work/venv/bin/python scripts/taric_capture.py`.
Append official Finnish certificate descriptions with the same command and `--conditions-only`.
The Finnish pages were added on 2026-10-24. The script records the exact timestamp per response.
The date in a consultation URL is its effective date, not necessarily its capture date.

Cases: Chinese bicycles (including nondeclarable parent), Vietnamese footwear / EVFTA,
zero-duty laptops, wine in EUR/hl, citrus entry-price conditions, steel safeguard quota,
Russian export restrictions and Meursing agricultural components. The committed tests
state the independently read official-page values next to their assertions.
