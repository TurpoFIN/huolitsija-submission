# Goods-based sanctions data (CN-coded annexes of the EU country regulations + CHPI)

Built by `scripts/build_goods_sanctions_data.py --on 2026-10-24` on 2026-10-24 (UTC). Re-run after every sanctions
package; the script picks the newest consolidated text in force on `--on` (Publications Office SPARQL) and fetches it
from the Publications Office Cellar (same document as EUR-Lex). The live TARIC lookup (`services/taric.py`) is the
primary source at runtime; these files are the regulation-text source next to it and are what the service falls back
to when TARIC cannot be reached.

| File | Regulation | Consolidated CELEX (in force 2026-10-24) | Source sha256 |
|---|---|---|---|
| `ru.json` | Council Regulation (EU) No 833/2014 (Russia) | 02014R0833-20260724 | `2682ecf3442d2f16a3b36ef4d9bbb6fde43fd4d6743bcd991bd4a8e14857770b` |
| `by.json` | Council Regulation (EC) No 765/2006 (Belarus) | 02006R0765-20261015 | `4387a58c81ebe06d6d712e5d75ffe97577ec05bddc4fbd8a18843f1e1b19ef11` |
| `ir.json` | Council Regulation (EU) No 267/2012 (Iran) | 02012R0267-20260801 | `4f2d477c43b286cc569d92e399f8ac7bcd637002ec4529cb68d7556690f3b107` |
| `kp.json` | Council Regulation (EU) 2017/1509 (DPRK) | 02017R1509-20251217 | `dbb874c9d7faa1c31bf8f9848dd48fdb56ebc25542d8ebfaebfb3ef53a2b3ad5` |
| `sy.json` | Council Regulation (EU) No 36/2012 (Syria) | 02012R0036-20260520 | `ee52d53521ef91e6ec66f3fbbcd4f6eecc2c8cd009fe5b849bed4203b14f0815` |
| `chpi.json` | Common High Priority Items: Annex XL of Reg 833/2014 (legal list, 50 HS6 codes) + tiers from the Commission list | — | — |
| `source_chpi.pdf` | European Commission, "List of Common High Priority Items (Version of February 2024)", https://finance.ec.europa.eu/document/download/5a2494db-d874-4e2b-bf2a-ec5a191d2dc0_en | — | `8c4bc8dbf0faa4573116395a3a84fa1aae66bf672a2910cbb282e9667c5d51f8` |

EUR-Lex links: `https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:<celex>`.

## How an annex becomes data
* Every table row of an annex whose first (or second) cell is a CN code (`8471`, `8471 30`, `ex 8471 30 00`,
  `Chapter 71`) becomes an item `{cn, ex, description, part}`. `ex` codes cover only part of the code: a match is a
  review, not a block.
* The flow of an annex is derived from the article paragraphs that cite it: "It shall be prohibited to sell, supply,
  transfer or export … listed in Annex XXIII" → `export`; "purchase, import or transfer … Annex XXI" → `import`;
  "transit" → `transit`; "contractually prohibit re-exportation" / "risks of exportation" → `due_diligence`. The
  paragraphs are stored under `basis` so every block cites its article. A price table (Annex XXVIII) is `information`.
* Examples (2026-10-24): RU Annex XXIII (Art. 3k) export, 521 rows; RU Annex XXI (Art. 3i) import, 271 rows;
  RU Annex XVIII (Art. 3h, luxury) export; RU Annex XL (Arts 2, 2a, 12g) CHPI; BY Annex XVIII (Art. 1bb) export.
* Prohibitions by Annex I of Reg 2021/821 (dual-use to RU/BY/KP/IR) are handled by `services/dual_use.py`
  (destination bans from the EU Sanctions Map + the regulation articles).

## CHPI cross-check
Annex XL of 833/2014 and the Commission's February 2024 list contain the same 50 HS6 codes (`only_in_*` lists are
empty); each item carries its tier (1, 2, 3.A, 3.B, 4.A, 4.B).
