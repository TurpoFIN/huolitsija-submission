"""Role-based B2B e-mail addresses from a company's own website (homepage + contact page).

Only the website the company registered in YTJ is read (domains are never guessed). robots.txt is respected for our
User-Agent, one request per second per host, 10 s timeout, 500 kB per page, at most MAX_PAGES pages, every hop must
resolve to a public address (no SSRF into internal services).

Only ROLE addresses on the company's own domain are kept (info@, myynti@, sales@, logistiikka@, osto@ ...). Personal
addresses (firstname.lastname@, initials) are never collected: role addresses of a legal person may receive B2B
direct marketing with an opt-out (Laki sähköisen viestinnän palveluista 917/2014, 200 § 3 mom.), personal ones may not
be harvested for this purpose.
"""
import asyncio
import html
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "HuolitsijaCRM/1.0 (+https://huolitsija.fi; B2B role-address lookup, respects robots.txt)"
ROBOTS_AGENT = "HuolitsijaCRM"
MAX_PAGES = 6
MAX_BYTES = 500_000
MIN_INTERVAL_S = 1.0
CONTACT_PATHS = ("/yhteystiedot", "/ota-yhteytta", "/fi/yhteystiedot", "/contact", "/contact-us", "/en/contact", "/kontakt")
CONTACT_LINK = re.compile(r"yhteys|yhteystied|ota-yhteytta|contact|kontakt", re.I)
# role local parts (whole local part, optionally with a short suffix like -fi or 2), most relevant to freight first
ROLES = [("logistiikka", "logistics"), ("logistics", "logistics"), ("logistic", "logistics"), ("lahettamo", "logistics"),
         ("shipping", "logistics"), ("kuljetukset", "logistics"), ("varasto", "logistics"), ("warehouse", "logistics"),
         ("osto", "purchasing"), ("ostot", "purchasing"), ("hankinta", "purchasing"), ("hankinnat", "purchasing"),
         ("purchasing", "purchasing"), ("procurement", "purchasing"), ("purchase", "purchasing"),
         ("vienti", "export"), ("export", "export"), ("tuonti", "import"), ("import", "import"),
         ("myynti", "sales"), ("sales", "sales"), ("tilaukset", "orders"), ("orders", "orders"), ("order", "orders"),
         ("tarjous", "sales"), ("tarjoukset", "sales"),
         ("asiakaspalvelu", "customer_service"), ("customerservice", "customer_service"), ("service", "customer_service"),
         ("palvelu", "customer_service"), ("info", "general"), ("toimisto", "general"), ("office", "general"),
         ("contact", "general"), ("yhteys", "general"), ("kontakt", "general"), ("hello", "general"), ("moi", "general")]
ROLE_RANK = {r: i for i, (r, _) in enumerate(ROLES)}
ROLE_KIND = dict(ROLES)
ROLE_RX = re.compile(r"(" + "|".join(sorted(ROLE_RANK, key=len, reverse=True)) + r")([._-]?(fi|en|se|sv|\d{1,2}))?")
EMAIL_RX = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
OBFUSCATED_RX = re.compile(r"([A-Z0-9._%+\-]+)\s*(?:\(|\[|\{)\s*(?:at|ät|miuku)\s*(?:\)|\]|\})\s*([A-Z0-9.\-]+\.[A-Z]{2,})", re.I)


def base_domain(host: str) -> str:
    parts = (host or "").lower().removeprefix("www.").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in {"co.uk", "com.cn", "com.pl", "co.jp", "com.tr", "com.au"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""


def role_of(email: str) -> str | None:
    """Role kind of a role address, or None for anything that could identify a person."""
    local = email.split("@")[0].lower()
    m = ROLE_RX.fullmatch(local)
    return ROLE_KIND[m.group(1)] if m else None


def extract(text: str, domain: str) -> list[dict]:
    found = {e.lower().strip(".") for e in EMAIL_RX.findall(text)}
    found |= {f"{a}@{b}".lower() for a, b in OBFUSCATED_RX.findall(text)}
    out = []
    for e in sorted(found):
        host = e.split("@")[1]
        if base_domain(host) != domain or re.search(r"\.(png|jpe?g|gif|svg|webp)$", e):
            continue
        role = role_of(e)
        if role:
            out.append({"email": e, "role": role, "rank": ROLE_RANK[ROLE_RX.fullmatch(e.split('@')[0]).group(1)]})
    return out


async def _public(host: str) -> bool:
    try:
        infos = await asyncio.get_running_loop().run_in_executor(None, socket.getaddrinfo, host, 443)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


class _Fetcher:
    def __init__(self, client: httpx.AsyncClient):
        self.client, self.last, self.robots = client, {}, {}

    async def _wait(self, host: str):
        gap = MIN_INTERVAL_S - (time.monotonic() - self.last.get(host, 0))
        if gap > 0:
            await asyncio.sleep(gap)
        self.last[host] = time.monotonic()

    async def _raw(self, url: str) -> tuple[int, str | None, str]:
        """GET with manual redirects (max 3), each hop public and http(s). -> (status, text, final url)."""
        for _ in range(4):
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname or not await _public(p.hostname):
                return 0, None, url
            await self._wait(p.hostname)
            async with self.client.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200:
                    return r.status_code, None, url
                ctype = r.headers.get("content-type", "")
                if "html" not in ctype and "text" not in ctype:
                    return r.status_code, None, url
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    chunks.append(chunk)
                    if size > MAX_BYTES:
                        break
                return 200, b"".join(chunks).decode(r.encoding or "utf-8", errors="replace"), url
        return 0, None, url

    async def allowed(self, url: str) -> bool:
        p = urlparse(url)
        key = f"{p.scheme}://{p.netloc}"
        if key not in self.robots:
            rp = RobotFileParser()
            try:
                status, text, _ = await self._raw(key + "/robots.txt")
            except httpx.HTTPError:
                status, text = 0, None
            if status == 200 and text is not None:
                rp.parse(text.splitlines())
            elif status in (401, 403):
                rp.disallow_all = True
            else:
                rp.allow_all = True
            self.robots[key] = rp
        return self.robots[key].can_fetch(ROBOTS_AGENT, url)

    async def page(self, url: str) -> tuple[str | None, str, str]:
        if not await self.allowed(url):
            return None, url, "robots.txt disallows"
        try:
            status, text, final = await self._raw(url)
        except httpx.HTTPError as exc:
            return None, url, f"failed: {type(exc).__name__}"
        return (html.unescape(text), final, "ok") if text is not None else (None, final, f"HTTP {status or 'refused'}")


def _contact_links(text: str, base: str, host: str) -> list[str]:
    links = []
    for href, label in re.findall(r"<a[^>]+href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", text, re.I | re.S):
        if CONTACT_LINK.search(href) or CONTACT_LINK.search(re.sub(r"<[^>]+>", "", label)):
            url = urljoin(base, href.strip())
            if urlparse(url).hostname and base_domain(urlparse(url).hostname) == base_domain(host) and url not in links:
                links.append(url)
    return links[:3]


async def find_role_addresses(website: str) -> dict:
    """-> {website, domain, emails [{email, role, source_url}], best, checked [path + result]}."""
    p = urlparse(website or "")
    if not p.hostname:
        return {"website": website, "domain": None, "emails": [], "best": None, "checked": ["no website"]}
    domain = base_domain(p.hostname)
    emails: dict[str, dict] = {}
    checked: list[str] = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
        f = _Fetcher(client)
        text, final, why = await f.page(website)
        if text is None and p.scheme == "https":
            text, final, why2 = await f.page(website.replace("https://", "http://", 1))
            why = f"{why}; http: {why2}"
        checked.append(f"{website} {why}")
        queue: list[str] = []
        if text is not None:
            for e in extract(text, domain):
                emails.setdefault(e["email"], {**e, "source_url": final})
            queue = _contact_links(text, final, p.hostname)
        root = f"{urlparse(final).scheme}://{urlparse(final).netloc}"
        queue += [root + path for path in CONTACT_PATHS if root + path not in queue]
        for url in queue[: MAX_PAGES - 1]:
            if len(checked) >= MAX_PAGES or (emails and any(e["role"] != "general" for e in emails.values())):
                break
            text, final_c, why = await f.page(url)
            checked.append(f"{url} {why}")
            if text is None:
                continue
            for e in extract(text, domain):
                emails.setdefault(e["email"], {**e, "source_url": final_c})
    ranked = sorted(emails.values(), key=lambda e: (e["rank"], e["email"]))
    for e in ranked:
        e.pop("rank", None)
    return {"website": website, "domain": domain, "emails": ranked, "best": ranked[0] if ranked else None, "checked": checked}
