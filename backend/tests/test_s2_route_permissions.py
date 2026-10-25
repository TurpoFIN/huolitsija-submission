"""Every permission a route asks for exists in core.permissions.PERMISSIONS. A misspelt name (row 27: the carrier test
events route asked for "shipments.read", which no role has) answers 403 to every user and no other test notices."""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.permissions import PERMISSIONS  # noqa: E402

BACKEND = os.path.join(os.path.dirname(__file__), "..")


def test_every_required_permission_exists():
    used: dict[str, set[str]] = {}
    for path in glob.glob(os.path.join(BACKEND, "routes", "*.py")) + glob.glob(os.path.join(BACKEND, "core", "*.py")):
        for m in re.finditer(r"require_perm\(\s*[\"']([^\"']+)[\"']", open(path, encoding="utf-8").read()):
            used.setdefault(m.group(1), set()).add(os.path.basename(path))
    assert len(used) > 10
    assert {p: sorted(f) for p, f in used.items() if p not in PERMISSIONS} == {}
