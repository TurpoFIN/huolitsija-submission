"""Every fixed milestone note, actor, party instruction, leg service and rate name the demo seed writes has a Finnish
entry in the frontend's storedText.fi.json, so the Finnish UI shows no English seed text. Offline: reads source files
only. Carrier service brands (liner loop codes and product names) stay as published."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FI = json.loads((ROOT / "frontend/src/i18n/storedText.fi.json").read_text())
PROPER_NOUNS = {"Huolitsija", "Air Connect", "Atlantic Loop AL5", "Silk / AE10", "AE7 Asia–Europe", "CEM Asia–North Europe",
                "FAL1 French Asia Line", "FAL3 French Asia Line", "FE2 Asia–North Europe", "FE3 Asia–North Europe",
                "FP1 Asia–North Europe"}


def _strings(nodes):
    return {n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _missing(texts):
    return sorted(t for t in texts - PROPER_NOUNS if t not in FI)


def test_seed_shipment_texts_have_finnish():
    tree = ast.parse((ROOT / "backend/services/seed_shipments.py").read_text())
    texts = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "achieve":
            texts |= _strings(n.args[3:4] + [k.value for k in n.keywords if k.arg == "note"])
        if isinstance(n, ast.Call):
            texts |= _strings([k.value for k in n.keywords if k.arg == "service"])
        if isinstance(n, ast.Dict):
            texts |= _strings([v for k, v in zip(n.keys, n.values)
                               if isinstance(k, ast.Constant) and k.value in ("opening_hours", "instructions")])
    assert len(texts) >= 40 and not _missing(texts), _missing(texts)


def test_seed_rate_names_have_finnish():
    tree = ast.parse((ROOT / "backend/services/seed_rates.py").read_text())
    params = {f.name: [a.arg for a in f.args.args] for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
    texts = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and "name" in params.get(getattr(n.func, "id", None), []):
            texts |= _strings(n.args[params[n.func.id].index("name"):][:1])
    assert len(texts) >= 80 and not _missing(texts), _missing(texts)
