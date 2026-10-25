"""Power of attorney (valtakirja) for customs representation, signed electronically in the portal.

The PDF states the principal, the representative, the type of representation (direct: in the name and on
behalf of the principal; indirect: in the representative's own name on behalf of the principal, with joint
liability for the customs debt), the scope, the validity and the electronic signature evidence (typed name,
consent checkbox, UTC timestamp, IP address, user agent, account e-mail). Retention: 6 years from the end of the
year of the last release it was used for (Tulli archiving rule).
"""
from __future__ import annotations

import hashlib
import io

from core import config

TEXT_VERSION = "poa-2026.1"

TYPE_TEXT = {
    "direct": {
        "en": "Direct representation: the representative acts in the name and on behalf of the principal. The principal "
              "is the declarant and the debtor of any customs debt (Union Customs Code, Reg (EU) 952/2013 art. 18 and "
              "77, now Reg (EU) 2026/2108).",
        "fi": "Suora edustus: edustaja toimii päämiehen nimissä ja lukuun. Päämies on tulli-ilmoituksen tekijä ja "
              "tullivelan velallinen.",
    },
    "indirect": {
        "en": "Indirect representation: the representative acts in its own name but on behalf of the principal. The "
              "representative is the declarant and is jointly and severally liable with the principal for any customs "
              "debt (Reg (EU) 952/2013 art. 18, 77(3) and 84, now Reg (EU) 2026/2108). For import VAT the principal "
              "remains the taxable person.",
        "fi": "Välillinen edustus: edustaja toimii omissa nimissään mutta päämiehen lukuun. Edustaja on tulli-ilmoituksen "
              "tekijä ja vastaa tullivelasta yhteisvastuullisesti päämiehen kanssa. Maahantuonnin arvonlisäverosta "
              "verovelvollinen on päämies.",
    },
}
SCOPE_TEXT = {"import": "Import declarations (release for free circulation) and related correspondence",
              "export": "Export and re-export declarations",
              "transit": "Transit declarations (NCTS)",
              "ens": "Entry summary declarations (ICS2) at house level",
              "correspondence": "Receiving decisions, requests for documents and invoices from Finnish Customs"}


SCOPE_TEXT_FI = {"import": "Tuonti-ilmoitukset (luovutus vapaaseen liikkeeseen) ja niihin liittyvä asiointi",
                 "export": "Vienti- ja jälleenvienti-ilmoitukset",
                 "transit": "Passitusilmoitukset (NCTS)",
                 "ens": "Saapumisen yleisilmoitukset (ICS2) lähetystasolla",
                 "correspondence": "Tullin päätösten, asiakirjapyyntöjen ja laskujen vastaanottaminen"}


UNDERTAKINGS = {
    "en": ["The principal gives the representative correct and complete information and documents for each declaration "
           "and tells it without delay about any change (value, origin, classification, licences).",
           "The principal keeps the commercial documents for 6 years from the end of the year in which the goods were "
           "released and shows them to Finnish Customs on request.",
           "The representative files declarations only after a person has approved them, and keeps the principal informed "
           "of Customs decisions, controls and payment demands.",
           "The principal pays the customs duties and charges arising from declarations made under this power of attorney.",
           "Either party can revoke this power of attorney in writing; revocation does not affect declarations already lodged.",
           "Services are performed according to NSAB 2015 (PSYM 2015)."],
    "fi": ["Päämies antaa edustajalle kustakin ilmoituksesta oikeat ja täydelliset tiedot ja asiakirjat ja ilmoittaa "
           "viipymättä muutoksista (arvo, alkuperä, tullinimike, luvat).",
           "Päämies säilyttää kaupalliset asiakirjat 6 vuotta sen vuoden lopusta, jona tavarat luovutettiin, ja esittää ne "
           "pyydettäessä Tullille.",
           "Edustaja jättää ilmoituksen vasta, kun henkilö on hyväksynyt sen, ja kertoo päämiehelle Tullin päätöksistä, "
           "tarkastuksista ja maksuvaatimuksista.",
           "Päämies maksaa tämän valtakirjan nojalla annetuista ilmoituksista määrätyt tullit ja maksut.",
           "Kumpi tahansa osapuoli voi peruuttaa valtakirjan kirjallisesti; peruutus ei koske jo jätettyjä ilmoituksia.",
           "Palvelut suoritetaan PSYM 2015 (NSAB 2015) -ehtojen mukaisesti."],
}


# The representative's own identifiers exist only after incorporation; say so instead of printing a dash.
PENDING = "rekisteröinti kesken / pending registration"

def consent_text(poa_type: str, company_name: str, lang: str = "en") -> str:
    if lang == "fi":
        kind = "suoraa" if poa_type == "direct" else "välillistä"
        return (f"Minulla on oikeus allekirjoittaa yrityksen {company_name} puolesta, ja annan {config.COMPANY_NAME}:lle "
                f"valtakirjan {kind} tulliedustusta varten edellä kuvatulla tavalla. Nimeni kirjoittaminen alle on "
                f"sähköinen allekirjoitukseni.")
    return (f"I am authorised to sign for {company_name} and I give {config.COMPANY_NAME} a power of attorney for "
            f"{poa_type} customs representation as described. Typing my name below is my electronic signature.")


def terms(company: dict, lang: str = "en") -> dict:
    """The POA terms the customer signs, in their language (the same texts go into the signed PDF)."""
    lang = "fi" if lang == "fi" else "en"
    return {"version": TEXT_VERSION, "lang": lang,
            "representative": {"name": config.COMPANY_NAME, "business_id": getattr(config, "COMPANY_BUSINESS_ID", None) or None},
            "types": {t: {"text": TYPE_TEXT[t][lang], "consent": consent_text(t, company.get("name") or "", lang)}
                      for t in ("direct", "indirect")},
            "scopes": {k: (SCOPE_TEXT_FI if lang == "fi" else SCOPE_TEXT)[k] for k in SCOPE_TEXT},
            "undertakings": UNDERTAKINGS[lang],
            "retention": ("Valtakirja ja sen allekirjoitustiedot säilytetään 6 vuotta sen kalenterivuoden lopusta, jona "
                          "tavarat on luovutettu" if lang == "fi" else
                          "The power of attorney and its signature evidence are kept for 6 years from the end of the "
                          "calendar year in which the goods were released")}


def render_poa_pdf(company: dict, poa: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    ss = getSampleStyleSheet()
    norm = ParagraphStyle("n", parent=ss["Normal"], fontName="Helvetica", fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=norm, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica", fontSize=16, leading=20, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=10, leading=13, spaceBefore=10,
                        spaceAfter=4)
    e = lambda s: (str(s) if s not in (None, "") else "—").replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title="Power of attorney for customs representation",
                            author=config.COMPANY_NAME)
    a = company.get("address") or {}
    t = poa.get("type") or "direct"
    sig = poa.get("signature") or {}
    rows = [
        ["Principal / Päämies", Paragraph(f"<b>{e(company.get('name'))}</b><br/>Business ID {e(company.get('business_id'))} · "
                                          f"EORI {e(company.get('eori'))} · VAT {e(company.get('vat_id'))}<br/>"
                                          f"{e(a.get('street'))}, {e(a.get('postal_code'))} {e(a.get('city'))}, "
                                          f"{e(a.get('country') or 'FI')}", norm)],
        ["Representative / Edustaja", Paragraph(f"<b>{e(config.COMPANY_NAME)}</b><br/>Business ID {e(config.COMPANY_BUSINESS_ID or PENDING)}"
                                                f" · EORI {e(config.COMPANY_EORI or PENDING)}<br/>{e(config.COMPANY_ADDRESS)}", norm)],
        ["Type / Edustustapa", Paragraph(f"<b>{'Direct' if t == 'direct' else 'Indirect'} representation</b><br/>"
                                         f"{e(TYPE_TEXT[t]['en'])}<br/><i>{e(TYPE_TEXT[t]['fi'])}</i>", norm)],
        ["Scope / Laajuus", Paragraph("<br/>".join(f"• {e(SCOPE_TEXT.get(s, s))}" for s in poa.get("scope") or []), norm)],
        ["Validity / Voimassa", Paragraph(f"{e(poa.get('valid_from'))} – {e(poa.get('valid_to'))}. Either party may revoke it "
                                          f"in writing; revocation does not affect declarations already lodged.", norm)],
    ]
    tb = Table(rows, colWidths=[42 * mm, 132 * mm])
    tb.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONT", (0, 0), (0, -1), "Helvetica", 8.5),
                            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
                            ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#dddddd")),
                            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story = [Paragraph(e(config.COMPANY_NAME), small),
             Paragraph("Power of attorney for customs representation", h1),
             Paragraph("Valtakirja tulliasioiden hoitamiseen", ParagraphStyle("sub", parent=norm, fontSize=11, leading=14)),
             Spacer(1, 6), tb,
             Paragraph("Undertakings / Sitoumukset", h2),
             *[Paragraph("• " + e(u), norm) for u in UNDERTAKINGS["fi" if sig.get("consent_lang") == "fi" else "en"]],
             Paragraph("Electronic signature / Sähköinen allekirjoitus", h2)]
    sig_rows = [["Signed by", f"{sig.get('typed_name') or '—'}{', ' + sig['title'] if sig.get('title') else ''}"],
                ["Account", sig.get("email") or "—"], ["Signed at (UTC)", sig.get("signed_at") or "—"],
                ["IP address", sig.get("ip") or "—"], ["Consent", sig.get("consent_text") or "—"],
                ["Text version", TEXT_VERSION]]
    st = Table([[k, Paragraph(e(v), small)] for k, v in sig_rows], colWidths=[42 * mm, 132 * mm])
    st.setStyle(TableStyle([("FONT", (0, 0), (0, -1), "Helvetica", 8), ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [st, Spacer(1, 10),
              Paragraph(f"Evidence hash (SHA-256 of principal, type, scope, validity and signature): "
                        f"{evidence_hash(company, poa)}", small)]
    doc.build(story)
    return buf.getvalue()


def evidence_hash(company: dict, poa: dict) -> str:
    sig = poa.get("signature") or {}
    payload = "|".join(str(x) for x in [company.get("id"), company.get("business_id"), poa.get("type"),
                                         ",".join(poa.get("scope") or []), poa.get("valid_from"), poa.get("valid_to"),
                                         sig.get("typed_name"), sig.get("email"), sig.get("signed_at"), sig.get("ip"),
                                         TEXT_VERSION])
    return hashlib.sha256(payload.encode()).hexdigest()
