"""Every English text the backend writes into a field the frontend displays is accounted for in the Finnish UI.

The frontend localises API payloads field by field (DISPLAY_FIELDS in frontend/src/lib/localizedData.js). For each
string literal or f-string the backend assigns to such a field, this test requires one of:
  - an exact Finnish entry in frontend/src/i18n/storedText.fi.json or customsChecks.fi.js, or
  - an entry in frontend/src/i18n/backendText.triage.json with a status and a reason:
      pattern     a displayText() pattern translates it (the jest test checks the sample),
      fi_variant  the backend stores a Finnish variant next to it (the reason names the field),
      coded       the UI renders a translation key from the record's code (the reason names the key),
      keep        it stays as is (proper noun, already Finnish, a code, or not shown in the UI; the reason says which).
Offline: reads source files only. A new English display text without one of these fails here."""
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
I18N = ROOT / "frontend/src/i18n"
STATUSES = {"pattern", "fi_variant", "coded", "keep"}
# Helpers whose positional arguments become displayed text: notifications (title, body) and approvals (title).
POSITIONAL = {"notify_user": slice(1, 3), "notify_company": slice(1, 3), "notify_role": slice(1, 3),
              "request_approval": slice(3, 4)}


def display_fields() -> set[str]:
    src = (ROOT / "frontend/src/lib/localizedData.js").read_text()
    return {f.strip().strip("'") for f in re.search(r"DISPLAY_FIELDS=new Set\(\[(.*?)\]\)", src).group(1).split(",")}


def _text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return ["".join(p.value if isinstance(p, ast.Constant) else "{…}" for p in node.values)]
    if isinstance(node, ast.IfExp):
        return _text(node.body) + _text(node.orelse)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):  # "a" + ("b" if x else "c")
        left, right = _text(node.left), _text(node.right)
        return [a + b for a in left for b in right] if left and right else left + right
    return []


def backend_display_literals() -> dict[str, str]:
    """{text: "file:line"} for English-looking literals assigned to display fields anywhere in the backend."""
    fields, found = display_fields(), {}
    for f in sorted((ROOT / "backend").rglob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        if "/tests/" in rel:
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            pairs = []
            if isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id in fields or isinstance(x, ast.Subscript)
                                                 and isinstance(x.slice, ast.Constant) and x.slice.value in fields
                                                 for x in n.targets):
                pairs = [("", n.value)]  # note = "...", leg["path_note"] = f"..."
            elif isinstance(n, ast.Return) and rel.endswith("services/geo.py") and isinstance(n.value, ast.Tuple):
                pairs = [("", e) for e in n.value.elts]  # route helpers return (path, source, note, ...)
            elif isinstance(n, ast.Dict):
                pairs = [(k.value, v) for k, v in zip(n.keys, n.values) if isinstance(k, ast.Constant) and k.value in fields]
            elif isinstance(n, ast.Call):
                pairs = [(k.arg, k.value) for k in n.keywords if k.arg in fields]
                name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                pairs += [("", a) for a in n.args[POSITIONAL.get(name, slice(0, 0))]]
            for _key, value in pairs:
                for s in _text(value):
                    if len(s) >= 4 and re.search(r"[a-z]{3,} [a-z]{2,}", s, re.I):
                        found.setdefault(s, f"{rel}:{getattr(value, 'lineno', n.lineno)}")
    return found


def _catalogued(text: str, stored: dict, customs: str) -> bool:
    return text in stored or f'"{text}"' in customs


def test_every_backend_display_text_is_translated_or_triaged():
    stored = json.loads((I18N / "storedText.fi.json").read_text())
    customs = (I18N / "customsChecks.fi.js").read_text()
    triage = json.loads((I18N / "backendText.triage.json").read_text())
    found = backend_display_literals()
    missing = sorted(f"{where}  {text}" for text, where in found.items()
                     if not _catalogued(text, stored, customs) and text not in triage)
    assert len(found) > 100 and not missing, "\n" + "\n".join(missing)


def test_triage_entries_are_valid_and_current():
    triage = json.loads((I18N / "backendText.triage.json").read_text())
    found = backend_display_literals()
    bad = [t for t, e in triage.items() if e.get("status") not in STATUSES or len(e.get("reason") or "") < 8]
    stale = [t for t in triage if t not in found]
    patterns_without_sample = [t for t, e in triage.items() if e.get("status") == "pattern" and not e.get("sample")]
    assert not bad and not stale and not patterns_without_sample, (bad, stale, patterns_without_sample)


def test_every_milestone_code_has_a_label_in_both_languages():
    """The timeline shows t("shipments.ms.<code>") and falls back to the stored English label when the key is missing."""
    src = (ROOT / "backend/services/shipments.py").read_text()
    codes = set(ast.literal_eval(re.search(r"^MILESTONE_LABELS = (\{.*?\n\})", src, re.S | re.M).group(1)))
    billing = (ROOT / "backend/services/billing.py").read_text()
    codes |= {c.lower() for c in re.findall(r'_shipment_milestone\([^,]+, "([A-Z_]+)"', billing)}
    ui = (ROOT / "frontend/src/i18n/shipments.js").read_text()
    missing = sorted(c for c in codes if ui.count(f'"shipments.ms.{c}":') != 2)
    assert {"invoiced", "paid"} <= codes and not missing, missing


# Notifications (N4): core.notify stores title_i18n / body_i18n, so every notify_* title and body must be bi(fi, en),
# same(text) for text the user wrote (no English words of our own in it), or a local name assigned from one of those.
NOTIFY = {"notify_user", "notify_company", "notify_role"}
EN_WORDS = re.compile(r"\b(the|for|was|is|are|and|of|to|needs|ready|review|drafted|released|submitted|invoice|quote)\b", re.I)


def _english(node) -> bool:
    return any(len(s) >= 4 and re.search(r"[a-z]{3,} [a-z]{2,}", s, re.I) for s in _text(node))


def _bilingual(node, assigned: set[str]) -> bool:
    if isinstance(node, ast.Call):
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == "bi":
            fi, en = (node.args + [None, None])[:2]
            fi_text = _text(fi) if fi is not None else []
            worded = any(re.search(r"[A-Za-zÄÖäö]{3,}", t) for t in fi_text)  # identical "{…} · {…}" is fine
            return len(node.args) == 2 and not (worded and fi_text == _text(en)) and not any(EN_WORDS.search(t) for t in fi_text)
        return name == "same" and not any(_english(a) for a in node.args)
    if isinstance(node, ast.IfExp):
        return _bilingual(node.body, assigned) and _bilingual(node.orelse, assigned)
    return isinstance(node, ast.Name) and node.id in assigned


def notification_calls() -> tuple[int, list[str]]:
    """(notify_* calls outside core/notify.py, "file:line" of each title/body that is not bilingual)."""
    calls, bad = set(), set()
    for f in sorted((ROOT / "backend").rglob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        if "/tests/" in rel or rel.endswith("core/notify.py"):
            continue
        for fn in ast.walk(ast.parse(f.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigned = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign) and _bilingual(n.value, set())
                        for t in n.targets if isinstance(t, ast.Name)}
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and (getattr(call.func, "id", None) or getattr(call.func, "attr", None)) in NOTIFY:
                    calls.add(f"{rel}:{call.lineno}")
                    args = list(call.args[1:3]) + [k.value for k in call.keywords if k.arg in ("title", "body")]
                    bad |= {f"{rel}:{a.lineno}" for a in args if not _bilingual(a, assigned)}
    return len(calls), sorted(bad)


def test_every_notification_title_and_body_is_bilingual():
    n, bad = notification_calls()
    assert n >= 20 and not bad, "\n" + "\n".join(bad)
