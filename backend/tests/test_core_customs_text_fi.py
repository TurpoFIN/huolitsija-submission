"""Every fixed customs check, flag and valuation text the backend can show has a Finnish entry in the frontend's
customsChecks.fi.js (review U7). Offline: reads source files only. A new English message without a translation
fails here instead of reaching the Finnish UI."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FI = (ROOT / "frontend/src/i18n/customsChecks.fi.js").read_text()


def _literals(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _literals(node.body) + _literals(node.orelse)
    return []


def test_every_fixed_validation_message_has_finnish():
    tree = ast.parse((ROOT / "backend/services/customs.py").read_text())
    texts = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_v" and len(n.args) >= 3:
            texts.update(t for t in _literals(n.args[2]) if t)
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "msg" for t in n.targets):
            texts.update(t for t in _literals(n.value) if t)
    missing = sorted(t for t in texts if f'"{t}"' not in FI)
    assert len(texts) > 20 and not missing, missing


def test_fixed_flag_and_label_texts_have_finnish():
    hs = (ROOT / "backend/services/hs.py").read_text()
    for text in ("Arms and ammunition (chapter 93): import needs a licence; compliance review required before filing.",
                 "Goods appear to come from Russian-occupied areas of Ukraine, which are under EU embargo — compliance review required."):
        assert f'"{text}"' in FI
    for text in ("Import VAT: postponed via VAT return (tilitysmenettely)", "Import VAT: payable to Customs",
                 "Estimate: duty rates are AI estimates until a broker confirms them in TARIC"):
        assert f'"{text}"' in FI
    for label in ("integrated circuits", "unmanned aircraft (drones)", "lathes"):
        assert label in hs and (f'"{label}"' in FI or f"{label}:" in FI)
