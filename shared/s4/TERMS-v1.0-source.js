// Huolitsija Oy general terms. Body text per locale; UI chrome is in i18n/public.js.
// Facts: domain-spec.md §1 (Incoterms 2020), §2 (NSAB 2015 / PSYM 2015 §14, §27, §28, §29), §3.3 (representation),
// §4.2 (postponed import VAT), §7 (liability regimes), §14 #15/#16/#18 (corrected limits, venue, 6-year archive).
export const TERMS_VERSION = "1.0";
export const TERMS_DATE = "2026-10-24";

const PSYM_URL = "https://www.huolintaliitto.fi/media/psym-2015_fi.pdf";

const en = [
  {
    id: "scope",
    title: "Scope and parties",
    blocks: [
      { p: "These general terms apply to all freight forwarding, transport arrangement, customs clearance and related services that Huolitsija Oy (in formation, Y-tunnus pending registration, Helsinki, Finland; \"Huolitsija\", \"we\") provides to business customers (\"you\"), including services quoted and booked at huolitsija.fi and in the customer portal." },
      { p: "Our services are meant for businesses. If you act as a consumer, mandatory consumer protection law takes precedence; contact us at [ops@huolitsija.fi](mailto:ops@huolitsija.fi) before booking." },
    ],
  },
  {
    id: "nsab",
    title: "NSAB 2015 (PSYM 2015)",
    blocks: [
      { p: "All our services are performed under the General Conditions of the Nordic Association of Freight Forwarders, NSAB 2015. In Finland they are published as PSYM 2015 (Pohjoismaisen Speditööriliiton Yleiset Määräykset 2015) by Suomen Huolinta- ja Logistiikkaliitto ry (SHLL). The Finnish text is available from SHLL at [huolintaliitto.fi](" + PSYM_URL + ")." },
      { p: "These general terms supplement NSAB 2015 (PSYM 2015). If they conflict, NSAB 2015 (PSYM 2015) prevails, unless a clause below expressly says that it deviates from it." },
      { p: "Every quote, booking confirmation and invoice refers to NSAB 2015 (PSYM 2015) and to the version of these terms in force on that date." },
    ],
  },
  {
    id: "liability",
    title: "Liability limits (summary)",
    blocks: [
      { p: "This is a summary for convenience. The binding rules are in NSAB 2015 (PSYM 2015) and in the mandatory law that applies to each mode of transport." },
      {
        table: {
          head: ["Situation", "Limit"],
          rows: [
            ["Loss of or damage to goods, Huolitsija as contracting party (principal)", "8.33 SDR per kg of gross weight"],
            ["Delay", "The freight charge for the assignment"],
            ["Other financial loss", "100,000 SDR per assignment"],
            ["Huolitsija as intermediary (agent)", "50,000 SDR per assignment; 500,000 SDR per occurrence in total"],
            ["Goods in storage", "500,000 SDR per occurrence"],
            ["Road carriage under CMR", "8.33 SDR per kg"],
            ["Air carriage under the Montreal Convention", "26 SDR per kg"],
            ["Sea carriage under the Finnish Maritime Code", "667 SDR per package or 2 SDR per kg"],
            ["Rail carriage under CIM", "17 SDR per kg"],
          ],
        },
      },
      { p: "Where it is shown that the loss happened during a stage of transport governed by mandatory law or an international convention, the limits of that law apply to that stage." },
      { p: "SDR is the Special Drawing Right of the International Monetary Fund. Amounts in SDR are converted into euros at the IMF rate on the day the claim is settled." },
      { note: "Our liability does not cover the full value of your goods. If you want that cover, buy cargo insurance; we can arrange it on request and price it in the quote." },
    ],
  },
  {
    id: "quotes",
    title: "Quotes and indicative prices",
    blocks: [
      { p: "Instant prices on our website and in the portal are indicative. They are calculated from our rate cards on the date shown on the quote and are valid until the validity date on the quote." },
      { p: "Prices are in euros and exclude VAT unless stated otherwise. The VAT treatment of each line is shown on the quote and invoice under Finnish VAT law." },
      { p: "Customs duties, import VAT, excise duties and other public charges are not included unless the quote says so. When we pay them on your behalf, we pass them through at cost." },
      { p: "An indicative price becomes binding when we confirm the booking. If carrier surcharges (such as fuel, currency, peak season, emissions or war-risk surcharges) change before confirmation, the price may change; we show the new price before you are committed." },
      { p: "A confirmed price applies to the cargo, route and Incoterms 2020 rule as described. Differences in weight, volume, dimensions, dangerous goods, pickup or delivery conditions, waiting time or storage are charged according to our or the carrier's tariff." },
    ],
  },
  {
    id: "booking",
    title: "Booking and acceptance",
    blocks: [
      { p: "A booking you make in the portal is an offer. The contract is formed when we confirm the booking in writing, in the portal or by e-mail." },
      { p: "We may decline a booking, for example when customer due diligence or sanctions screening is not complete, when no available carrier accepts the cargo, or when the information given is not enough to perform the service safely and lawfully." },
      { p: "If you cancel after confirmation, we charge the costs already incurred and the cancellation fees charged to us by carriers and terminals." },
    ],
  },
  {
    id: "customer",
    title: "Your obligations",
    blocks: [
      {
        ul: [
          "Give us correct and complete information in time: parties and addresses, description of the goods, number and type of packages, gross weight, volume and dimensions, value, and the HS code where you know it.",
          "For containers, give the verified gross mass (VGM) required by the SOLAS Convention before the carrier's cut-off.",
          "Declare dangerous goods with UN number, proper shipping name, class and packing group, and pack, mark and document them under ADR, the IMDG Code or the IATA Dangerous Goods Regulations. Never hand over undeclared dangerous goods.",
          "Pack and mark the goods so that they withstand normal handling and transport.",
          "Obtain the licences, permits and certificates needed for the export, import or transit of the goods, including export licences for dual-use items.",
          "Tell us the Incoterms 2020 rule and named place of your sales contract. We do not assume one for you.",
        ],
      },
      { p: "You are liable for costs, fines and damage caused by incorrect, incomplete or late information, as set out in NSAB 2015 (PSYM 2015)." },
    ],
  },
  {
    id: "payment",
    title: "Invoicing and payment",
    blocks: [
      { p: "Invoices are payable within 14 days net unless another term is agreed in writing. Pay by bank transfer using the Finnish reference number on the invoice, or by card." },
      { p: "Late payment interest is charged under the Finnish Interest Act (633/1982)." },
      { p: "We may set a credit limit, or ask for prepayment of duties, taxes and carrier charges that we pay on your behalf." },
      { p: "Please raise any objection to an invoice before its due date. Undisputed parts of the invoice remain payable on time." },
    ],
  },
  {
    id: "customs",
    title: "Customs representation and power of attorney",
    blocks: [
      { p: "We act as your customs representative under EU customs law (Union Customs Code) and the Finnish Customs Act." },
      { p: "Unless agreed otherwise in writing, we act as your direct representative: in your name and on your behalf. You are the declarant and the debtor for customs duties and taxes." },
      { p: "Indirect representation (in our own name, on your behalf) is available only by separate written agreement. In that case we are jointly and severally liable with you for the customs debt, and we may ask you to pay duties and taxes before we lodge the declaration or release the goods." },
      { p: "Before the first declaration you give us a written power of attorney (valtakirja) stating the type of representation, its scope and its validity. We keep it, and the declarations and supporting documents, for at least six years from the end of the calendar year of the customs decision, as Finnish Customs requires." },
      { p: "You are responsible for the correctness of the documents and data you give us for a declaration, such as the commercial invoice, proof of origin and licences. A customs specialist checks every declaration before we lodge it, and may ask you for more information." },
      { p: "If you are registered for VAT in Finland, import VAT is reported in your own periodic VAT return. Otherwise import VAT is payable to Finnish Customs." },
    ],
  },
  {
    id: "sanctions",
    title: "Sanctions and export control",
    blocks: [
      { p: "We screen customers, parties and shipments against EU and UN sanctions lists and comply with EU restrictive measures, including those concerning Russia and Belarus." },
      { p: "We do not handle shipments that would breach sanctions or export-control rules. You confirm that the goods, the parties, the end use and the payments do not breach them, and you tell us if the goods are dual-use items or otherwise need an export licence." },
      { p: "Where we have reasonable grounds to suspect a breach, we may suspend or cancel a booking or hold the goods without liability to you, and report to the authorities as the law requires." },
    ],
  },
  {
    id: "lien",
    title: "Lien",
    blocks: [
      { p: "Under NSAB 2015 (PSYM 2015) we have a lien over the goods and documents in our possession as security for our claims relating to the assignment and for earlier claims against you. If a claim is not paid when due, we may sell the goods as permitted by NSAB 2015 (PSYM 2015) and Finnish law, after notifying you." },
    ],
  },
  {
    id: "claims",
    title: "Claims and time bars",
    blocks: [
      {
        ul: [
          "Visible loss or damage: report it when the goods are delivered and note it on the delivery document.",
          "Concealed damage: report it in writing within 7 days of delivery.",
          "Other claims: report them in writing within 14 days of the day you learned of the circumstances.",
        ],
      },
      { p: "If you do not report in time, you may have to prove that the loss happened while the goods were in our care." },
      { p: "Claims against us are time-barred one year after delivery, or after the day when delivery should have happened, unless the applicable convention gives another period (for example two years under the Montreal Convention)." },
      { p: "Send claims through the portal or to [ops@huolitsija.fi](mailto:ops@huolitsija.fi) with the shipment reference and supporting documents." },
    ],
  },
  {
    id: "law",
    title: "Governing law and disputes",
    blocks: [
      { p: "Finnish law applies. We first try to settle a dispute by negotiation. If that fails, the dispute is resolved by the general court of Huolitsija's principal place of business, Helsinki, as provided in NSAB 2015 (PSYM 2015)." },
    ],
  },
  {
    id: "changes",
    title: "Changes and contact",
    blocks: [
      { p: "We may update these terms. The version and date are shown at the top of this page; the version in force when you book applies to that booking." },
      { p: "Questions about these terms: [ops@huolitsija.fi](mailto:ops@huolitsija.fi)." },
    ],
  },
];

const fi = [
  {
    id: "scope",
    title: "Soveltamisala ja osapuolet",
    blocks: [
      { p: "Näitä yleisiä sopimusehtoja sovelletaan kaikkiin huolinta-, kuljetusten järjestämis-, tullaus- ja niihin liittyviin palveluihin, joita Huolitsija Oy (perusteilla, Y-tunnus: rekisteröinti vireillä, Helsinki; ”Huolitsija”, ”me”) tarjoaa yritysasiakkailleen (”te”), myös palveluihin, jotka on hinnoiteltu ja varattu osoitteessa huolitsija.fi tai asiakasportaalissa." },
      { p: "Palvelumme on tarkoitettu yrityksille. Jos toimitte kuluttajana, pakottava kuluttajansuojalainsäädäntö on etusijalla; ottakaa yhteyttä osoitteeseen [ops@huolitsija.fi](mailto:ops@huolitsija.fi) ennen varausta." },
    ],
  },
  {
    id: "nsab",
    title: "NSAB 2015 (PSYM 2015)",
    blocks: [
      { p: "Kaikki palvelumme tuotetaan Pohjoismaisen Speditööriliiton yleisten määräysten NSAB 2015 mukaisesti. Suomessa ne julkaisee nimellä PSYM 2015 (Pohjoismaisen Speditööriliiton Yleiset Määräykset 2015) Suomen Huolinta- ja Logistiikkaliitto ry (SHLL). Suomenkielinen teksti on saatavilla SHLL:n sivuilta: [huolintaliitto.fi](" + PSYM_URL + ")." },
      { p: "Nämä yleiset sopimusehdot täydentävät NSAB 2015 (PSYM 2015) -ehtoja. Ristiriitatilanteessa NSAB 2015 (PSYM 2015) on etusijalla, ellei jäljempänä nimenomaisesti todeta poikkeamaa siitä." },
      { p: "Jokaisessa tarjouksessa, varausvahvistuksessa ja laskussa viitataan NSAB 2015 (PSYM 2015) -ehtoihin ja kyseisenä päivänä voimassa olevaan näiden ehtojen versioon." },
    ],
  },
  {
    id: "liability",
    title: "Vastuunrajoitukset (yhteenveto)",
    blocks: [
      { p: "Tämä on yhteenveto. Sitovat säännöt ovat NSAB 2015 (PSYM 2015) -ehdoissa ja kuhunkin kuljetusmuotoon sovellettavassa pakottavassa lainsäädännössä." },
      {
        table: {
          head: ["Tilanne", "Raja"],
          rows: [
            ["Tavaran katoaminen tai vahingoittuminen, Huolitsija sopimusosapuolena", "8,33 SDR bruttopainokilolta"],
            ["Viivästys", "Toimeksiannon rahdin määrä"],
            ["Muu taloudellinen vahinko", "100 000 SDR toimeksiantoa kohden"],
            ["Huolitsija välittäjänä", "50 000 SDR toimeksiantoa kohden; yhteensä 500 000 SDR vahinkotapahtumaa kohden"],
            ["Varastoitu tavara", "500 000 SDR vahinkotapahtumaa kohden"],
            ["Maantiekuljetus (CMR)", "8,33 SDR/kg"],
            ["Lentokuljetus (Montrealin yleissopimus)", "26 SDR/kg"],
            ["Merikuljetus (merilaki)", "667 SDR kollilta tai 2 SDR/kg"],
            ["Rautatiekuljetus (CIM)", "17 SDR/kg"],
          ],
        },
      },
      { p: "Jos osoitetaan, että vahinko tapahtui kuljetuksen vaiheessa, johon sovelletaan pakottavaa lakia tai kansainvälistä yleissopimusta, kyseisen vaiheen vastuurajat määräytyvät sen mukaan." },
      { p: "SDR on Kansainvälisen valuuttarahaston erityinen nosto-oikeus. SDR-määräiset summat muunnetaan euroiksi IMF:n kurssilla korvauksen maksupäivänä." },
      { note: "Vastuumme ei kata tavaranne koko arvoa. Jos haluatte sellaisen turvan, hankkikaa rahtivakuutus; järjestämme sen pyynnöstä ja hinnoittelemme sen tarjoukseen." },
    ],
  },
  {
    id: "quotes",
    title: "Tarjoukset ja suuntaa antavat hinnat",
    blocks: [
      { p: "Verkkosivujen ja portaalin hinta heti -hinnat ovat suuntaa antavia. Ne lasketaan hinnastoistamme tarjouksessa näkyvän päivän tiedoilla, ja ne ovat voimassa tarjouksessa näkyvään päivään asti." },
      { p: "Hinnat ovat euroissa ja ilman arvonlisäveroa, ellei toisin mainita. Jokaisen rivin alv-käsittely näkyy tarjouksessa ja laskussa arvonlisäverolain mukaisesti." },
      { p: "Tullit, maahantuonnin arvonlisävero, valmisteverot ja muut julkiset maksut eivät sisälly hintaan, ellei tarjouksessa toisin sanota. Kun maksamme ne puolestanne, veloitamme ne omakustannushintaan." },
      { p: "Suuntaa antava hinta muuttuu sitovaksi, kun vahvistamme varauksen. Jos kuljetusyhtiöiden lisät (esimerkiksi polttoaine-, valuutta-, sesonki-, päästö- tai sotariskilisät) muuttuvat ennen vahvistusta, hinta voi muuttua; näytämme uuden hinnan ennen kuin sitoudutte." },
      { p: "Vahvistettu hinta koskee kuvattua tavaraa, reittiä ja Incoterms 2020 -lauseketta. Painon, tilavuuden, mittojen, vaarallisten aineiden, nouto- tai toimitusolosuhteiden, odotusajan tai varastoinnin poikkeamat laskutetaan meidän tai kuljetusyhtiön hinnaston mukaan." },
    ],
  },
  {
    id: "booking",
    title: "Varaus ja sen hyväksyminen",
    blocks: [
      { p: "Portaalissa tekemänne varaus on tarjous. Sopimus syntyy, kun vahvistamme varauksen kirjallisesti portaalissa tai sähköpostilla." },
      { p: "Voimme kieltäytyä varauksesta esimerkiksi, jos asiakkaan tuntemis- tai pakotetarkistus on kesken, jos mikään käytettävissä oleva kuljetusyhtiö ei hyväksy tavaraa tai jos annetut tiedot eivät riitä palvelun turvalliseen ja lailliseen suorittamiseen." },
      { p: "Jos peruutatte vahvistetun varauksen, veloitamme jo syntyneet kulut sekä kuljetusyhtiöiden ja terminaalien meiltä perimät peruutusmaksut." },
    ],
  },
  {
    id: "customer",
    title: "Asiakkaan velvollisuudet",
    blocks: [
      {
        ul: [
          "Antakaa meille ajoissa oikeat ja täydelliset tiedot: osapuolet ja osoitteet, tavaran kuvaus, kollien määrä ja tyyppi, bruttopaino, tilavuus ja mitat, arvo sekä tullinimike, jos se on tiedossa.",
          "Konteista on ilmoitettava SOLAS-yleissopimuksen edellyttämä varmennettu bruttopaino (VGM) ennen kuljetusyhtiön määräaikaa.",
          "Ilmoittakaa vaaralliset aineet UN-numeroineen, virallisine kuljetusnimineen, luokkineen ja pakkausryhmineen, ja pakatkaa, merkitkää ja dokumentoikaa ne ADR-, IMDG- tai IATA DGR -säännösten mukaisesti. Ilmoittamattomia vaarallisia aineita ei saa luovuttaa kuljetukseen.",
          "Pakatkaa ja merkitkää tavara niin, että se kestää tavanomaisen käsittelyn ja kuljetuksen.",
          "Hankkikaa tavaran vientiin, tuontiin tai passitukseen tarvittavat luvat, todistukset ja sertifikaatit, myös kaksikäyttötuotteiden vientiluvat.",
          "Kertokaa meille kauppasopimuksenne Incoterms 2020 -lauseke ja nimetty paikka. Emme oleta sitä puolestanne.",
        ],
      },
      { p: "Vastaatte virheellisten, puutteellisten tai myöhästyneiden tietojen aiheuttamista kuluista, seuraamuksista ja vahingoista NSAB 2015 (PSYM 2015) -ehtojen mukaisesti." },
    ],
  },
  {
    id: "payment",
    title: "Laskutus ja maksaminen",
    blocks: [
      { p: "Laskut maksetaan 14 päivän kuluessa netto, ellei kirjallisesti ole sovittu muuta maksuaikaa. Maksu tilisiirtona laskun viitenumerolla tai kortilla." },
      { p: "Viivästyneestä maksusta peritään viivästyskorkoa korkolain (633/1982) mukaisesti." },
      { p: "Voimme asettaa luottorajan tai pyytää ennakkomaksun tulleista, veroista ja kuljetusyhtiöiden maksuista, jotka maksamme puolestanne." },
      { p: "Esittäkää mahdolliset huomautukset laskusta ennen eräpäivää. Laskun kiistämättömät osat on maksettava ajallaan." },
    ],
  },
  {
    id: "customs",
    title: "Tulliedustus ja valtakirja",
    blocks: [
      { p: "Toimimme tulliedustajananne EU:n tullilainsäädännön (unionin tullikoodeksi) ja tullilain mukaisesti." },
      { p: "Ellei kirjallisesti toisin sovita, toimimme suorana edustajananne: teidän nimissänne ja puolestanne. Te olette ilmoittaja ja tullien ja verojen velallinen." },
      { p: "Välillinen edustus (omissa nimissämme teidän puolestanne) on mahdollinen vain erillisellä kirjallisella sopimuksella. Silloin vastaamme tullivelasta yhteisvastuullisesti kanssanne, ja voimme pyytää teitä maksamaan tullit ja verot ennen ilmoituksen jättämistä tai tavaran luovutusta." },
      { p: "Ennen ensimmäistä tulli-ilmoitusta annatte meille kirjallisen valtakirjan, josta käyvät ilmi edustustapa, laajuus ja voimassaolo. Säilytämme sen sekä ilmoitukset ja niiden liitteet vähintään kuusi vuotta sen kalenterivuoden lopusta, jona tullipäätös tehtiin, kuten Tulli edellyttää." },
      { p: "Vastaatte ilmoitusta varten antamienne asiakirjojen ja tietojen, kuten kauppalaskun, alkuperäselvityksen ja lupien, oikeellisuudesta. Tulliasiantuntija tarkistaa jokaisen ilmoituksen ennen sen jättämistä ja voi pyytää lisätietoja." },
      { p: "Jos olette arvonlisäverovelvollisten rekisterissä, maahantuonnin arvonlisävero ilmoitetaan omassa kausiveroilmoituksessanne. Muussa tapauksessa maahantuonnin arvonlisävero maksetaan Tullille." },
    ],
  },
  {
    id: "sanctions",
    title: "Pakotteet ja vientivalvonta",
    blocks: [
      { p: "Tarkistamme asiakkaat, osapuolet ja lähetykset EU:n ja YK:n pakotelistoja vasten ja noudatamme EU:n rajoittavia toimenpiteitä, myös Venäjää ja Valko-Venäjää koskevia." },
      { p: "Emme käsittele lähetyksiä, jotka rikkoisivat pakotteita tai vientivalvontasäännöksiä. Vakuutatte, etteivät tavara, osapuolet, loppukäyttö tai maksut riko niitä, ja kerrotte meille, jos tavara on kaksikäyttötuote tai muuten vientiluvanvarainen." },
      { p: "Jos meillä on perusteltu syy epäillä rikkomusta, voimme keskeyttää tai perua varauksen tai pidättää tavaran ilman vastuuta teille ja ilmoittaa asiasta viranomaisille lain edellyttämällä tavalla." },
    ],
  },
  {
    id: "lien",
    title: "Pantti- ja pidätysoikeus",
    blocks: [
      { p: "NSAB 2015 (PSYM 2015) -ehtojen mukaisesti meillä on pantti- ja pidätysoikeus hallussamme oleviin tavaroihin ja asiakirjoihin toimeksiantoon liittyvien ja aiempien teihin kohdistuvien saataviemme vakuutena. Jos saatavaa ei makseta eräpäivänä, voimme myydä tavaran NSAB 2015 (PSYM 2015) -ehtojen ja Suomen lain sallimalla tavalla ilmoitettuamme siitä teille." },
    ],
  },
  {
    id: "claims",
    title: "Reklamaatiot ja vanhentuminen",
    blocks: [
      {
        ul: [
          "Näkyvä katoaminen tai vahinko: ilmoittakaa heti vastaanoton yhteydessä ja merkitkää se luovutusasiakirjaan.",
          "Piilevä vahinko: ilmoittakaa kirjallisesti viimeistään seitsemän (7) päivän kuluessa vastaanotosta.",
          "Muut vaatimukset: ilmoittakaa kirjallisesti 14 päivän kuluessa siitä, kun saitte tiedon asiasta.",
        ],
      },
      { p: "Jos ilmoitusta ei tehdä ajoissa, joudutte mahdollisesti näyttämään toteen, että vahinko tapahtui tavaran ollessa vastuullamme." },
      { p: "Meihin kohdistuvat vaatimukset vanhenevat vuoden kuluttua tavaran luovutuksesta tai päivästä, jona se olisi pitänyt luovuttaa, ellei sovellettava yleissopimus määrää muuta aikaa (esimerkiksi kaksi vuotta Montrealin yleissopimuksen mukaan)." },
      { p: "Lähettäkää vaatimukset portaalin kautta tai osoitteeseen [ops@huolitsija.fi](mailto:ops@huolitsija.fi) lähetyksen viitteen ja tositteiden kanssa." },
    ],
  },
  {
    id: "law",
    title: "Sovellettava laki ja riidat",
    blocks: [
      { p: "Sopimukseen sovelletaan Suomen lakia. Pyrimme ratkaisemaan erimielisyydet ensin neuvottelemalla. Jos se ei onnistu, riita ratkaistaan Huolitsijan pääasiallisen liikepaikan yleisessä tuomioistuimessa Helsingissä NSAB 2015 (PSYM 2015) -ehtojen mukaisesti." },
    ],
  },
  {
    id: "changes",
    title: "Muutokset ja yhteystiedot",
    blocks: [
      { p: "Voimme päivittää näitä ehtoja. Versio ja päivämäärä näkyvät sivun yläosassa; varaukseen sovelletaan varaushetkellä voimassa ollutta versiota." },
      { p: "Kysymykset ehdoista: [ops@huolitsija.fi](mailto:ops@huolitsija.fi)." },
    ],
  },
];

export default { en, fi };
