"""s2: SMTP2GO refuses a custom header value over 255 characters; a long e-mail-channel References header (10 ids,
464 chars on production 04:57 UTC) failed every threaded mail. The References header is trimmed to the thread's
first id and the newest ids that fit; nothing else changes."""
import asyncio
import os
import sys
import uuid

os.environ["DB_NAME"] = f"huo_s2_smtp2go_{uuid.uuid4().hex[:8]}"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)

import pytest  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:
    pytest.skip("run test_s2_smtp2go_headers.py in its own pytest process", allow_module_level=True)

IDS = [f"<em-{uuid.uuid4()}@inbound.postmarkapp.com>" for _ in range(10)]


def teardown_module(_m):
    from core.db import client
    _LOOP.run_until_complete(client.drop_database(os.environ["DB_NAME"]))


def test_fit_references_keeps_root_and_newest_within_255():
    from core.email import fit_references
    out = fit_references(" ".join(IDS))
    assert len(out) <= 255 and out.split()[0] == IDS[0] and out.split()[-1] == IDS[-1]
    assert fit_references(IDS[0]) == IDS[0] and fit_references(" ".join(IDS[:2])) == " ".join(IDS[:2])


def test_smtp2go_send_trims_only_the_long_references_header(monkeypatch):
    from core import email
    sent = {}

    class Resp:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"data": {"succeeded": 1, "failed": 0, "email_id": "x1"}}

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.update(json)
            return Resp()

    monkeypatch.setattr(config, "SMTP2GO_API_KEY", "test-key")
    monkeypatch.setattr(email.httpx, "AsyncClient", Client)
    hdr = {"Message-ID": IDS[-1], "In-Reply-To": IDS[-2], "References": " ".join(IDS), "X-Huolitsija-Channel": "email"}
    rec = _LOOP.run_until_complete(email.send_email("marc+huolitsija-s2@heliosone.fi", "Re: Tarjous", "<p>x</p>", "x",
                                                    headers=dict(hdr), provider="smtp2go", from_addr="Huolitsija <a@b.fi>"))
    assert rec["status"] == "sent"
    custom = {h["header"]: h["value"] for h in sent["custom_headers"]}
    assert all(len(v) <= 255 for v in custom.values())
    assert custom["In-Reply-To"] == IDS[-2] and custom["Message-ID"] == IDS[-1]
    assert custom["References"].split()[0] == IDS[0] and custom["References"].split()[-1] == IDS[-1]
