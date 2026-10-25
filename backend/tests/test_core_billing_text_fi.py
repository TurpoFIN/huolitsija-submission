"""Every warning the billing engine stores on a draft invoice has a Finnish text for the ops UI (draft banner and audit trail)."""
import ast
from pathlib import Path

from services.billing import text_i18n

BILLING = Path(__file__).resolve().parents[1] / "services/billing.py"
SAMPLE = {"quote.get('number')": "Q-2026-000001", "skipped_payer": "2"}


def _warning_texts() -> list[str]:
    out = []
    for n in ast.walk(ast.parse(BILLING.read_text())):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "append" and getattr(n.func.value, "id", None) == "warnings":
            a = n.args[0]
            if isinstance(a, ast.Constant):
                out.append(a.value)
            elif isinstance(a, ast.JoinedStr):
                out.append("".join(p.value if isinstance(p, ast.Constant) else SAMPLE[ast.unparse(p.value)] for p in a.values))
    return out


def test_billing_warnings_have_finnish():
    texts = _warning_texts()
    untranslated = [w for w in texts if text_i18n(w)["fi"] == w]
    assert len(texts) >= 6 and not untranslated, untranslated


def test_issue_errors_stored_as_warnings_have_finnish():
    """Auto-issue stores issue_invoice / validate_customs_invoice errors as warnings (lookups and status races excluded)."""
    src = BILLING.read_text()
    errors = []
    for fn in ("issue_invoice", "validate_customs_invoice"):
        body = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == fn)
        errors += [n.exc.args[0].value for n in ast.walk(body) if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
                   and getattr(n.exc.func, "id", None) == "BillingError" and isinstance(n.exc.args[0], ast.Constant)
                   and n.exc.args[0].value != "Invoice not found"]
    untranslated = [e for e in errors if text_i18n(e)["fi"] == e]
    assert len(errors) >= 6 and not untranslated, untranslated
