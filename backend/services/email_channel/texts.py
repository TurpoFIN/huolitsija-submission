"""Finnish and English e-mail texts of the e-mail channel. Every reply is one of these templates filled from
validated fields; the model never writes a reply."""
from __future__ import annotations

from core import config
from core.notify import day, eur

MODE = {"fi": {"sea": "Merirahti", "road": "Maantie", "air": "Lentorahti", "rail": "Rautatie", "multimodal": "Yhdistetty"},
        "en": {"sea": "Sea freight", "road": "Road", "air": "Air freight", "rail": "Rail", "multimodal": "Multimodal"}}
MISSING = {
    "origin": ("lähtöpaikka (kaupunki ja maa)", "origin (city and country)"),
    "destination": ("määränpää (kaupunki ja maa)", "destination (city and country)"),
    "weight_kg": ("kokonaispaino (kg)", "total gross weight (kg)"),
    "volume_m3": ("tilavuus (m³) tai kollien mitat", "volume (m³) or package dimensions"),
    "ready_date": ("valmis noudettavaksi -päivä", "date the goods are ready"),
    "incoterm": ("toimitusehto (esim. FCA, DAP)", "Incoterm (e.g. FCA, DAP)"),
    "packages": ("kollimäärä ja -tyyppi", "number and type of packages"),
    "shipper": ("lähettäjän nimi ja katuosoite", "shipper's name and street address"),
    "consignee": ("vastaanottajan nimi ja katuosoite", "consignee's name and street address"),
    "incoterm_place": ("toimitusehdon paikka", "named place of the Incoterm"),
    "option": ("valitsemasi vaihtoehdon numero", "the number of the option you choose"),
}
BLOCKING = ("origin", "destination", "weight_kg")


def fi(lang: str) -> bool:
    return lang != "en"


def missing_labels(keys: list[str], lang: str) -> list[str]:
    i = 0 if fi(lang) else 1
    return [MISSING[k][i] for k in keys if k in MISSING]


def terms_url(base: str) -> str:
    return f"{base or 'https://huolitsija.fi'}/terms"


def _place(loc: dict | None) -> str:
    loc = loc or {}
    return ", ".join(x for x in [loc.get("name") or loc.get("city") or loc.get("unlocode"), loc.get("country")] if x) or "—"


def cargo_label(text: str, lang: str) -> str:
    if not fi(lang):
        return text
    for en, local in (("EUR pallet", "EUR-lava"), ("FIN pallet", "FIN-lava"), ("carton", "laatikko"), ("crate", "puulaatikko"), ("package", "kolli")):
        text = text.replace(en, local)
    return text


def indicative(o: dict) -> bool:
    """An indicative market rate (services.pricing.label_indicative; older quotes: rate_source), not a carrier price."""
    return o.get("price_status") == "indicative" or (o.get("rate_source") == "indicative" and o.get("price_basis") != "contract")


def quote_mail(q: dict, lang: str, base: str) -> tuple[str, list[str]]:
    req = q.get("request") or {}
    route = f"{_place(req.get('origin'))} → {_place(req.get('destination'))}"
    opts = [o for o in q.get("options") or []][:6]
    f = fi(lang)
    lines = [("Kiitos tarjouspyynnöstä. Tässä hintamme:" if f else "Thank you for your request. Here is our price:"),
             f"{route}" + (f" · {cargo_label(req.get('cargo_summary'), lang)}" if req.get("cargo_summary") else "")]
    for i, o in enumerate(opts, 1):
        mode = MODE["fi" if f else "en"].get(o.get("mode"), o.get("mode") or "")
        transit = (f"{o.get('transit_days')} vrk" if f else f"{o.get('transit_days')} days") if o.get("transit_days") else ""
        firm = o.get("firmness")
        est = (" · ARVIO, ei vielä varattavissa" if f else " · ESTIMATE, not bookable yet") if firm and firm != "firm" else ""
        if not est and indicative(o):
            est = " · Ohjeellinen hinta – vahvistetaan varauksen yhteydessä" if f else " · Indicative price, confirmed at booking"
        valid = (f" · voimassa {day(o.get('valid_until') or q.get('valid_until'), 'fi')}" if f
                 else f" · valid until {day(o.get('valid_until') or q.get('valid_until'), 'en')}")
        lines.append(f"{i}) {mode}" + (f" · {o.get('carrier_name')}" if o.get("carrier_name") else "")
                     + (f" · {transit}" if transit else "") + f" · {eur(o.get('total_cents'), 'fi' if f else 'en')}"
                     + (" (alv 0 %)" if f else " (excl. VAT)") + valid + est)
    if (q.get("sourcing") or {}).get("status") in ("requested", "partial"):
        lines.append("Pyydämme parhaillaan kumppaneiltamme sitovaa hintaa; lähetämme sen tähän ketjuun heti kun se on valmis."
                     if f else "We are asking our partners for a firm rate and will send it to this thread as soon as it is ready.")
    lines.append(("Varaa vastaamalla tähän viestiin: VARAA ja vaihtoehdon numero (esim. VARAA 1), sekä lähettäjän ja "
                  "vastaanottajan nimi ja katuosoite. Voit liittää samaan viestiin kauppalaskun ja pakkausluettelon.")
                 if f else
                 ("To book, reply to this e-mail with BOOK and the option number (e.g. BOOK 1) and give the shipper's and "
                  "consignee's name and street address. You can attach the commercial invoice and packing list."))
    lines.append((f"Varaamalla hyväksyt NSAB 2015 (PSYM 2015) -yleiset määräykset ja ehtomme: {terms_url(base)}. "
                  "Hinnat ilman arvonlisäveroa; tullit ja tuonnin arvonlisävero eivät sisälly, ellei niitä ole eritelty.")
                 if f else
                 (f"By booking you accept NSAB 2015 and our terms: {terms_url(base)}. Prices exclude VAT; duties and "
                  "import VAT are not included unless listed."))
    subject = (f"Tarjous {q.get('number')}: {route}" if f else f"Quote {q.get('number')}: {route}")
    return subject, lines


def need_info(keys: list[str], lang: str, summary: str | None = None) -> tuple[str, list[str]]:
    f = fi(lang)
    lines = [("Kiitos viestistä. Hinnoittelua varten tarvitsemme vielä:" if f else
              "Thank you. To price this shipment we still need:")]
    lines += [f"– {x}" for x in missing_labels(keys, lang)]
    if summary:
        lines.append(("Ymmärsimme pyynnöstäsi näin: " if f else "What we understood so far: ") + summary)
    lines.append("Vastaa tähän viestiin, niin lähetämme hinnan heti." if f else "Reply to this e-mail and we will send the price right away.")
    return ("Tarvitsemme vielä muutaman tiedon" if f else "We need a few more details"), lines


def unresolved_place(queries: list[str], lang: str) -> list[str]:
    f = fi(lang)
    return [(f"Emme tunnistaneet paikkaa: {', '.join(queries)}. Kerro kaupunki ja maa." if f else
             f"We could not identify the place: {', '.join(queries)}. Please give the city and country.")]


def quote_review(q: dict, lang: str) -> tuple[str, list[str]]:
    f = fi(lang)
    return ((f"Tarjous {q.get('number')} tarkistetaan" if f else f"Quote {q.get('number')} is being reviewed"),
            [("Kiitos pyynnöstä. Asiantuntijamme tarkistaa hinnan ennen lähettämistä; saat tarjouksen tähän viestiketjuun "
              "yleensä saman arkipäivän aikana." if f else
              "Thank you. Our specialist checks this price before we send it; you will get the quote in this thread, "
              "usually within the same working day.")])


def cannot_quote(reason: str, lang: str) -> tuple[str, list[str]]:
    f = fi(lang)
    return (("Emme voineet hinnoitella pyyntöä automaattisesti" if f else "We could not price this request automatically"),
            [("Syy: " if f else "Reason: ") + reason,
             "Asiantuntijamme katsoo pyynnön ja vastaa tähän ketjuun." if f else
             "A specialist will look at the request and answer in this thread."])


def booking_need(keys: list[str], lang: str) -> tuple[str, list[str]]:
    f = fi(lang)
    lines = [("Kiitos! Varausta varten tarvitsemme vielä:" if f else "Thank you! To complete the booking we still need:")]
    lines += [f"– {x}" for x in missing_labels(keys, lang)]
    lines.append("Vastaa tähän viestiin näillä tiedoilla." if f else "Please reply to this e-mail with these details.")
    return ("Varaus: puuttuvat tiedot" if f else "Booking: details needed"), lines


def kyc_link(url: str, lang: str, days: int) -> tuple[str, list[str], str]:
    f = fi(lang)
    lines = ([
        "Kiitos varauksesta. Ennen ensimmäistä kuljetusta tunnistamme asiakasyrityksen (rahanpesulaki ja tullin "
        "edustusvaltakirja). Se vie noin viisi minuuttia:",
        "1) yrityksen tiedot ja Y-tunnus, 2) tosiasialliset edunsaajat, 3) nimenkirjoittaja, 4) tullin edustusvaltakirja.",
        f"Linkki on voimassa {days} päivää eikä vaadi salasanaa. Varaus vahvistetaan tähän viestiketjuun heti, kun "
        "tarkistukset ovat valmiit.",
    ] if f else [
        "Thank you for booking. Before the first shipment we verify your company (anti-money-laundering rules and the "
        "customs power of attorney). It takes about five minutes:",
        "1) company details and business ID, 2) beneficial owners, 3) signatory, 4) customs power of attorney.",
        f"The link is valid for {days} days and needs no password. We confirm the booking in this thread as soon as "
        "the checks are complete.",
    ])
    return ("Yrityksen tunnistus ennen varausta" if f else "Company verification before booking"), lines, \
        ("Tunnista yritys" if f else "Verify company")


def kyc_state(state: str, lang: str) -> tuple[str, list[str]]:
    f = fi(lang)
    if state == "in_review":
        return (("Yrityksen tunnistus tarkistetaan" if f else "Company verification under review"),
                ["Kiitos tiedoista. Osa tarkistuksista vaatii vielä asiantuntijan katsomaan ne; vahvistamme varauksen "
                 "tähän ketjuun heti kun se on valmis." if f else
                 "Thank you. Some checks need a specialist to look at them; we confirm the booking in this thread as soon "
                 "as that is done."])
    return (("Yrityksen tunnistusta ei voitu hyväksyä" if f else "Company verification could not be approved"),
            ["Emme voi tällä hetkellä vahvistaa varausta. Asiantuntijamme ottaa sinuun yhteyttä." if f else
             "We cannot confirm the booking at the moment. A specialist will contact you."])


def booking_failed(reason: str, lang: str) -> tuple[str, list[str]]:
    f = fi(lang)
    return (("Varausta ei voitu vahvistaa vielä" if f else "The booking could not be confirmed yet"),
            [("Syy: " if f else "Reason: ") + reason,
             "Vastaa tähän viestiin korjatuilla tiedoilla, niin jatkamme." if f else
             "Reply to this e-mail with the corrected details and we continue."])


def docs_received(names: list[str], ref: str | None, lang: str, *, later: bool = False) -> tuple[str, list[str]]:
    f = fi(lang)
    head = (f"Kiitos, vastaanotimme {len(names)} asiakirjaa" if f else f"Thank you, we received {len(names)} document(s)")
    lines = [head + (f" lähetykselle {ref}:" if f and ref else f" for shipment {ref}:" if ref else ":")]
    lines += [f"– {n}" for n in names]
    lines.append(("Liitämme ne lähetykseen, kun varaus on vahvistettu." if f else "We attach them to the shipment once the booking is confirmed.")
                 if later else
                 ("Luemme ne ja tarkistamme tullitiedot; ilmoitamme, jos jotain puuttuu." if f else
                  "We read them and check the customs data; we let you know if anything is missing."))
    return (("Asiakirjat vastaanotettu" if f else "Documents received"), lines)


def received_for_owner(lang: str) -> tuple[str, list[str]]:
    f = fi(lang)
    return (("Viestisi on vastaanotettu" if f else "We received your message"),
            ["Kiitos viestistä. Asiantuntijamme vastaa tähän ketjuun, yleensä saman arkipäivän aikana." if f else
             "Thank you for your message. A specialist answers in this thread, usually within the same working day."])


def pay_line(url: str, lang: str) -> str:
    return (f"Maksa kortilla (Stripe): {url}" if fi(lang) else f"Pay by card (Stripe): {url}")


def footer(lang: str) -> str:
    return (f"{config.COMPANY_NAME} · {config.COMPANY_ADDRESS} · Vastaa suoraan tähän viestiin. Palvelut PSYM 2015 -ehdoin"
            if fi(lang) else
            f"{config.COMPANY_NAME} · {config.COMPANY_ADDRESS} · Reply directly to this e-mail. Services under NSAB 2015")
