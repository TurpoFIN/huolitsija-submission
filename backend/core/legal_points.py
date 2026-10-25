"""Legally required human decisions. An agent that meets one of these always puts the work in the owner queue,
whatever Autopilot says: core.agent_runtime.decide(..., legal_point="<code>").

Each entry names the rule that makes a person necessary. The readable list is shared/legal-human-points.md
(drafted by s2; legal wording finalised by s4 and the company's lawyer, see the launch runbook).
"""

LEGAL_POINTS: dict[str, dict] = {
    "customs_filing_not_delegated": {
        "title_fi": "Tulli-ilmoituksen jättäminen edustajana",
        "title_en": "Lodging a customs declaration as representative",
        "basis": "Union Customs Code Reg (EU) 952/2013 art. 15(2), 18-19 (repealed by Reg (EU) 2026/2108, applied in "
                 "stages; old article numbers labelled as such); Tullilaki 304/2016 § 18",
        "reason_fi": "Edustaja vastaa ilmoituksen tietojen oikeellisuudesta. Johto ei ole antanut järjestelmälle pysyvää "
                     "valtuutusta jättää ilmoituksia, joten henkilön on hyväksyttävä jokainen ilmoitus.",
        "reason_en": "The representative is responsible for the accuracy of the declaration. The director has not given "
                     "the system a standing delegation to lodge declarations, so a person must approve each one.",
    },
    "customs_indirect_representation": {
        "title_fi": "Epäsuora edustus: yhteisvastuu tullivelasta",
        "title_en": "Indirect representation: joint liability for the customs debt",
        "basis": "Reg (EU) 952/2013 art. 18(1), 77(3), 84 (old numbering); RULE R-1 of the domain spec",
        "reason_fi": "Epäsuorassa edustuksessa Huolitsija on ilmoittaja ja vastaa tullivelasta yhteisvastuullisesti. "
                     "Valtuutus ei kata epäsuoraa edustusta, joten omistajan on hyväksyttävä ilmoitus.",
        "reason_en": "In indirect representation Huolitsija is the declarant and jointly and severally liable for the "
                     "customs debt. The delegation does not cover indirect representation, so the owner must approve.",
    },
    "sanctions_potential_match": {
        "title_fi": "Mahdollinen pakoteosuma",
        "title_en": "Potential sanctions match",
        "basis": "Council Reg (EU) 269/2014 art. 2, Reg (EU) 833/2014; Laki 659/1967; Rikoslaki 46 luku; RULE K-1",
        "reason_fi": "Mahdollista pakoteosumaa ei saa ohittaa automaattisesti. Henkilön on päätettävä, onko osuma väärä "
                     "hälytys vai estetäänkö asiakas.",
        "reason_en": "A potential sanctions match may never be cleared automatically. A person decides whether it is a "
                     "false positive or the customer is blocked.",
    },
    "ru_by_trade": {
        "title_fi": "Venäjä tai Valko-Venäjä lähtö- tai määrämaana",
        "title_en": "Russia or Belarus as origin or destination",
        "basis": "Council Reg (EU) 833/2014 and 765/2006 (as amended, 21st package 23.7.2026); RULE K-2",
        "reason_fi": "Venäjän ja Valko-Venäjän kauppaan liittyy laajoja kieltoja. Pakotevastaavan on tarkistettava "
                     "jokainen tapaus; oletuksena kielletty.",
        "reason_en": "Trade with Russia and Belarus is widely prohibited. A compliance person must review each case; "
                     "the default is deny.",
    },
    "dual_use_export": {
        "title_fi": "Kaksikäyttötuotteen vienti",
        "title_en": "Export of dual-use goods",
        "basis": "Reg (EU) 2021/821 art. 3 and Annex I; export authorisation from the Ministry for Foreign Affairs; RULE K-3",
        "reason_fi": "Kaksikäyttötuotteen vienti vaatii ulkoministeriön luvan. Henkilön on tarkistettava lupa ennen vientiä.",
        "reason_en": "Exporting dual-use goods needs an authorisation from the Ministry for Foreign Affairs. A person "
                     "must check the licence before export.",
    },
    "pep_relationship": {
        "title_fi": "Poliittisesti vaikutusvaltainen henkilö",
        "title_en": "Politically exposed person",
        "basis": "Laki rahanpesun ja terrorismin rahoittamisen estämisestä 444/2017, 3 luku 13 § (applied as good practice; "
                 "obliged-entity status to be confirmed by the lawyer, runbook D5)",
        "reason_fi": "Asiakkuus poliittisesti vaikutusvaltaisen henkilön kanssa vaatii ylimmän johdon hyväksynnän.",
        "reason_en": "A business relationship with a politically exposed person needs senior management approval.",
    },
    "claim_above_insurance_limit": {
        "title_fi": "Korvausvaatimus yli vakuutuksen ratkaisuvaltuuden",
        "title_en": "Claim above the insurer's settlement authority",
        "basis": "Vakuutussopimuslaki 543/1994 and the liability insurance policy terms (no admission of liability or "
                 "settlement above the delegated authority without the insurer's consent); NSAB 2015 / PSYM 2015 § 22-28",
        "reason_fi": "Vastuuvakuutuksen ehtojen mukaan korvausta ei saa myöntää yli vakuutusyhtiön antaman ratkaisuvaltuuden "
                     "ilman sen suostumusta. Omistaja ilmoittaa vahingon vakuutusyhtiölle ja päättää.",
        "reason_en": "Under the liability insurance terms, liability may not be admitted above the insurer's delegated "
                     "settlement authority without its consent. The owner notifies the insurer and decides.",
    },
    "director_signature": {
        "title_fi": "Yhtiön puolesta allekirjoittaminen",
        "title_en": "Signing on behalf of the company",
        "basis": "Osakeyhtiölaki 624/2006, 6 luku 25-27 § (the board, managing director or persons with a right of "
                 "representation sign for the company)",
        "reason_fi": "Sopimuksen allekirjoittaa hallitus, toimitusjohtaja tai edustamisoikeuden haltija, ei järjestelmä.",
        "reason_en": "The contract is signed by the board, the managing director or a holder of the right of "
                     "representation, not by the system.",
    },
    "dangerous_goods": {
        "title_fi": "Vaarallisten aineiden kuljetus",
        "title_en": "Dangerous goods transport",
        "basis": "IMDG Code 1.3 and 5.4.1; ADR 1.3 and 5.4.1; IATA DGR 1.5 (trained personnel)",
        "reason_fi": "Vaarallisten aineiden tiedot ja hyväksyntä tarkistaa koulutettu henkilö.",
        "reason_en": "Dangerous goods data and acceptance are checked by a trained person.",
    },
    "aml_suspicion_report": {
        "title_fi": "Epäilyttävästä liiketoimesta ilmoittaminen",
        "title_en": "Suspicious transaction report",
        "basis": "Laki 444/2017, 9 luku 1-4 § (report to the Financial Intelligence Unit via goAML; no tipping off)",
        "reason_fi": "Ilmoituksen rahanpesun selvittelykeskukselle tekee henkilö; asiakkaalle ei kerrota.",
        "reason_en": "A person files the report with the Financial Intelligence Unit; the customer is not told.",
    },
}


def legal_reason(code: str | None, lang: str = "fi") -> str | None:
    p = LEGAL_POINTS.get(code or "")
    if not p:
        return None
    return p["reason_fi" if lang == "fi" else "reason_en"]


def public_list() -> list[dict]:
    return [{"code": k, **v} for k, v in LEGAL_POINTS.items()]
