"""Partner contact discovery: safe manual redirects, contact-page fallback, same-domain commercial addresses only.
Offline: httpx MockTransport and a fake resolver; no network."""
import asyncio

import httpx

from services import partner_network as pn

PUBLIC, PRIVATE = "93.184.216.34", "10.0.0.5"


def _setup(monkeypatch, pages: dict, resolve=None):
    def handler(request: httpx.Request):
        spec = pages.get(str(request.url))
        if spec is None:
            return httpx.Response(404)
        if isinstance(spec, tuple):
            return httpx.Response(spec[0], headers={"location": spec[1]})
        return httpx.Response(200, text=spec)

    monkeypatch.setattr(pn, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    table = resolve or {}
    monkeypatch.setattr(pn.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", (table.get(host, PUBLIC), port))])


def _run(carrier):
    return asyncio.run(pn.published_contact(carrier))


def test_redirect_is_followed_to_the_contact_address(monkeypatch):
    _setup(monkeypatch, {"https://carrier.example/": (302, "https://www.carrier.example/en"),
                         "https://www.carrier.example/en": '<a href="mailto:sales@carrier.example">Sales</a>'})
    email, evidence = _run({"website": "https://carrier.example/"})
    assert email == "sales@carrier.example" and evidence == "https://www.carrier.example/en"


def test_redirect_to_internal_address_or_plain_http_is_refused(monkeypatch):
    _setup(monkeypatch, {"https://carrier.example/": (302, "https://intranet.carrier.example/"),
                         "https://intranet.carrier.example/": "rahti@carrier.example",
                         "https://carrier.example/contact": (301, "http://carrier.example/contact")},
           resolve={"intranet.carrier.example": PRIVATE})
    email, evidence = _run({"website": "https://carrier.example/"})
    assert email is None
    assert "public address" in evidence and "left HTTPS" in evidence


def test_contact_page_fallback_prefers_sales_and_skips_privacy(monkeypatch):
    _setup(monkeypatch, {"https://rahti.example.fi/": "<p>Tervetuloa</p>",
                         "https://rahti.example.fi/yhteystiedot": "tietosuoja@rahti.example.fi asiakaspalvelu@rahti.example.fi "
                                                                  "ceo@otherdomain.fi myynti@rahti.example.fi"})
    email, _ = _run({"website": "https://rahti.example.fi/"})
    assert email in {"asiakaspalvelu@rahti.example.fi", "myynti@rahti.example.fi"}


def test_nothing_published_is_reported_with_what_was_checked(monkeypatch):
    _setup(monkeypatch, {"https://quiet.example/": "<p>No addresses here</p>"})
    email, evidence = _run({"website": "https://quiet.example/"})
    assert email is None and "checked: / ok; /contact HTTP 404" in evidence
