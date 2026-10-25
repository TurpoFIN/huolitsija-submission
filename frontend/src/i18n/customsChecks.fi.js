// Finnish for system-generated customs check, flag and valuation texts (services/customs.py validate(),
// services/hs.py flags_for(), valuation notes, DOC-3 document checks). Stored declarations keep the English
// source; displayText() projects it here. Exact strings first, then patterns with their variable parts.

const KYC = { not_started: "aloittamatta", "not started": "aloittamatta", in_review: "tarkistettavana", pending: "odottaa",
  rejected: "hylätty", needs_info: "lisätietoja tarvitaan", expired: "vanhentunut", approved: "hyväksytty" };
const SANCTIONS = { not_screened: "tarkistamatta", "not screened": "tarkistamatta", potential_match: "mahdollinen osuma",
  match: "osuma", pending: "kesken", clear: "ei osumia" };
const REP = { direct: "suora", indirect: "välillinen" };
const DOC = { "commercial invoice": "kauppalasku", "packing list": "pakkausluettelo", "bill of lading": "konossementti",
  "sea waybill": "merirahtikirja", "air waybill": "lentorahtikirja", cmr: "CMR-rahtikirja", cim: "CIM-rahtikirja" };
const DUAL_USE = {
  "machine tools (laser, ultrasonic, EDM and similar)": "työstökoneet (laser, ultraääni, kipinätyöstö ym.)",
  "machining centres": "koneistuskeskukset", lathes: "sorvit",
  "machine tools for drilling, boring and milling": "poraus-, avarrus- ja jyrsintäkoneet",
  "computers with cryptographic functions (mass-market items are normally released by the Cryptography Note)":
    "salaustoimintoja sisältävät tietokoneet (massamarkkinatuotteet on yleensä vapautettu salaushuomautuksella)",
  "computer parts with cryptographic functions (mass-market items are normally released)":
    "salaustoimintoja sisältävät tietokoneen osat (massamarkkinatuotteet on yleensä vapautettu)",
  "network equipment with cryptographic functions (mass-market items are normally released)":
    "salaustoimintoja sisältävät verkkolaitteet (massamarkkinatuotteet on yleensä vapautettu)",
  "cameras and transmission apparatus (thermal / high-performance imaging)": "kamerat ja lähetinlaitteet (lämpö- / suorituskykyinen kuvantaminen)",
  "radar and radio navigation apparatus": "tutka- ja radionavigointilaitteet", "integrated circuits": "integroidut piirit",
  "electrical machines with individual functions": "erillistoimintoiset sähkökoneet", "lasers and optical devices": "laserit ja optiset laitteet",
  "navigation instruments": "navigointilaitteet", "surveying instruments": "mittauslaitteet", analysers: "analysaattorit",
  "nuclear reactors and parts": "ydinreaktorit ja niiden osat", "radioactive elements and isotopes": "radioaktiiviset alkuaineet ja isotoopit",
  "prepared explosives": "valmistetut räjähdysaineet", "detonators and igniters": "nallit ja sytyttimet",
  "unmanned aircraft (drones)": "miehittämättömät ilma-alukset (droonit)",
};

// "1,288.7" (English source) -> "1 288,7"
const num = (s) => s.replace(/,/g, " ").replace(/\.(?=\d+$)/, ",");
const date = (iso) => new Date(`${iso}T12:00:00Z`).toLocaleDateString("fi-FI", { timeZone: "Europe/Helsinki" });
const docName = (s) => DOC[s.toLowerCase()] || s;

export const EXACT = {
  "Transport document type is missing: bill of lading, sea waybill, AWB, CMR or CIM (DE 2/1)": "Kuljetusasiakirjan laji puuttuu: konossementti, merirahtikirja, AWB, CMR tai CIM (DE 2/1)",
  "Anti-dumping risk: enter a TARIC additional code or record the evidence clearing the risk": "Polkumyyntiriski: syötä TARIC-lisäkoodi tai kirjaa näyttö, joka poistaa riskin",
  "Commercial invoice attached": "Kauppalasku liitetty",
  "Commodity code missing": "Tavaranimike puuttuu",
  "Confirm the commercial invoice fields (some were unclear)": "Vahvista kauppalaskun tiedot (osa oli epäselviä)",
  "Consignment value is EUR 150 or less: for distance sales a fixed EUR 3 duty per goods item applies from 1.7.2026 (Reg (EU) 2026/382) and an EUR 2 handling fee (Y126) from 1.11.2026; confirm the regime":
    "Lähetyksen arvo on enintään 150 €: etämyynnissä sovelletaan 1.7.2026 alkaen kiinteää 3 € tullia tavaraerää kohden (asetus (EU) 2026/382) ja 1.11.2026 alkaen 2 € käsittelymaksua (Y126); vahvista menettely",
  "Country of origin missing (DE 5/15)": "Alkuperämaa puuttuu (DE 5/15)",
  "Customer KYC approved": "Asiakkaan tuntemistarkistus hyväksytty",
  "Customer sanctions screening clear": "Asiakkaan pakotelistatarkistus: ei osumia",
  "Duty rate not estimated; look it up in TARIC": "Tullitariffia ei ole arvioitu; tarkista se TARICista",
  "Freight to the EU border included": "Rahti EU:n rajalle sisältyy",
  "Gross weight missing": "Bruttopaino puuttuu",
  "Incoterm and named place are required (DE 4/1)": "Toimituslauseke ja nimetty paikka vaaditaan (DE 4/1)",
  "Indirect representation: Huolitsija is the declarant and jointly liable for the customs debt (UCC art. 77(3), 84)":
    "Välillinen edustus: Huolitsija on ilmoittaja ja yhteisvastuussa tullivelasta (UCC 77 art. 3 kohta ja 84 art.)",
  "Invoice fields read and checked": "Laskun tiedot luettu ja tarkistettu",
  "Item value could not be converted to EUR": "Nimikkeen arvoa ei voitu muuntaa euroiksi",
  "Net weight exceeds gross weight": "Nettopaino ylittää bruttopainon",
  "No goods items: the invoice has no readable lines": "Ei tavaranimikkeitä: laskulla ei ole luettavia rivejä",
  "Seller sanctions screening could not run; run it before filing": "Myyjän pakotelistatarkistusta ei voitu ajaa; aja se ennen ilmoittamista",
  "Seller screened against EU, UN and OFAC lists: no match": "Myyjä tarkistettu EU:n, YK:n ja OFACin pakotelistoja vasten: ei osumia",
  "Set the customs office of presentation (DE 5/26)": "Määritä esittämistullitoimipaikka (DE 5/26)",
  "Set the location of goods (DE 5/23)": "Määritä tavaroiden sijainti (DE 5/23)",
  "TARIC trade-defence assessment recorded": "TARIC-kaupan suojatoimien arvio kirjattu",
  "Transport document number (B/L, AWB or CMR) is missing (DE 2/1)": "Kuljetusasiakirjan numero (konossementti, AWB tai CMR) puuttuu (DE 2/1)",
  "Upload the commercial invoice": "Lataa kauppalasku",
  "Weights stated": "Painot ilmoitettu",
  "Confirm the 8-digit CN code against TARIC on the declaration date": "Vahvista 8-numeroinen CN-koodi TARICista ilmoituspäivän mukaan",
  "Choose the 8-digit CN subdivision and confirm it against TARIC on the declaration date": "Valitse 8-numeroinen CN-alanimike ja vahvista se TARICista ilmoituspäivän mukaan",
  "Arms and ammunition (chapter 93): import needs a licence; compliance review required before filing.": "Aseet ja ammukset (93. ryhmä): tuonti vaatii luvan; compliance-tarkistus tarvitaan ennen ilmoittamista.",
  "Goods appear to come from Russian-occupied areas of Ukraine, which are under EU embargo — compliance review required.": "Tavarat näyttävät olevan peräisin Venäjän miehittämiltä Ukrainan alueilta, joita koskee EU:n kauppasaarto – compliance-tarkistus tarvitaan.",
  "Air freight: only the share set in IA Annex 23-01 for the departure airport belongs in the customs value — check before filing.": "Lentorahti: tullausarvoon kuuluu vain täytäntöönpanoasetuksen liitteessä 23-01 lähtölentoasemalle määritetty osuus – tarkista ennen ilmoittamista.",
  "Import VAT: postponed via VAT return (tilitysmenettely)": "Tuonnin arvonlisävero: tilitysmenettely (ilmoitetaan arvonlisäveroilmoituksella)",
  "Import VAT: payable to Customs": "Tuonnin arvonlisävero: maksetaan Tullille",
  "Estimate: duty rates are AI estimates until a broker confirms them in TARIC": "Arvio: tullitariffit ovat tekoälyn arvioita, kunnes tulliasiantuntija vahvistaa ne TARICista",
  "Package counts match": "Kollimäärät täsmäävät",
  "Container numbers match": "Konttinumerot täsmäävät",
  "Container numbers differ between documents": "Konttinumerot eroavat asiakirjojen välillä",
  "Consignee matches on every document": "Vastaanottaja on sama kaikissa asiakirjoissa",
  "Packing list refers to the commercial invoice": "Pakkausluettelo viittaa kauppalaskuun",
  "Packing list refers to a different invoice number than the commercial invoice": "Pakkausluettelo viittaa eri laskunumeroon kuin kauppalasku",
};

// Trade defence measures (services/hs.py _TRADE_DEFENCE): product, country and detail texts of the H-1 flag.
const AD_PRODUCT = {
  "bicycles (not motorised)": "polkupyörät (ei moottoroidut)",
  "essential bicycle parts (frames, front forks, derailleurs, crank-gear, free-wheel sprocket-wheels, brakes, brake levers, complete wheels, handlebars)":
    "polkupyörän olennaiset osat (rungot, etuhaarukat, vaihtajat, kampisarjat, vapaarattaiden ketjupyörät, jarrut, jarruvivut, kokonaiset pyörät, ohjaustangot)",
  "electric bicycles (pedal assistance, auxiliary electric motor)": "sähköpolkupyörät (polkuavustus, apusähkömoottori)",
  "iron or steel fasteners (not stainless): screws, bolts with heads, washers": "raudasta tai teräksestä valmistetut kiinnittimet (ei ruostumaton): ruuvit, kannalliset pultit, aluslevyt",
  "aluminium extrusions (bars, rods, profiles, tubes, <= 99.3 % Al)": "alumiinipursotteet (tangot, profiilit, putket, <= 99,3 % Al)",
};
const AD_COUNTRY = { China: "Kiina", Indonesia: "Indonesia", Malaysia: "Malesia", "Sri Lanka": "Sri Lanka", Tunisia: "Tunisia",
  Cambodia: "Kambodža", Pakistan: "Pakistan", "the Philippines": "Filippiinit", Taiwan: "Taiwan", "Viet Nam": "Vietnam" };
const AD_DETAIL = {
  "anti-dumping duty up to 48.5 %, also extended to bicycles consigned from Indonesia, Malaysia, Sri Lanka, Tunisia, Cambodia, Pakistan and the Philippines unless made by an exempted producer":
    "polkumyyntitulli enintään 48,5 %, laajennettu myös Indonesiasta, Malesiasta, Sri Lankasta, Tunisiasta, Kambodžasta, Pakistanista ja Filippiineiltä lähetettyihin polkupyöriin, ellei valmistaja ole vapautettu",
  "the bicycle anti-dumping duty (48.5 %) is extended to these parts; assemblers registered under the exemption scheme of Reg. (EC) No 88/97 can import them duty-suspended. Parts consigned from China count as Chinese origin unless another origin is proven":
    "polkupyörien polkumyyntitulli (48,5 %) on laajennettu näihin osiin; asetuksen (EY) N:o 88/97 vapautusjärjestelmään rekisteröityneet kokoonpanijat voivat tuoda ne tullia suspendoiden. Kiinasta lähetetyt osat katsotaan kiinalaisiksi, ellei muuta alkuperää osoiteta",
  "anti-dumping duty from 9.9 % to 70.1 % depending on the producer, alongside a countervailing duty":
    "polkumyyntitulli 9,9–70,1 % valmistajasta riippuen sekä tasoitustulli",
  "anti-dumping duty from 22.1 % to 86.5 % depending on the producer": "polkumyyntitulli 22,1–86,5 % valmistajasta riippuen",
  "anti-dumping duty from 21.2 % to 32.1 %; kept in force during the expiry review opened on 27.3.2026 (notice C/2026/1920)":
    "polkumyyntitulli 21,2–32,1 %; voimassa 27.3.2026 alkaneen voimassaolon päättymistä koskevan tarkastelun ajan (ilmoitus C/2026/1920)",
};
const upperFirst = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const lowerFirst = (s) => s.charAt(0).toLowerCase() + s.slice(1);
const adDetail = (s) => {
  const key = Object.keys(AD_DETAIL).find((k) => lowerFirst(s).startsWith(`${k} (`));
  return key ? upperFirst(AD_DETAIL[key]) + s.slice(key.length) : s;
};
const TD_LEAD = { "Only part of this CN code is covered (TARIC subdivision decides). ": "Koskee vain osaa tästä CN-koodista (TARIC-alanimike ratkaisee). ",
  "Depends on the CN code and the exact product. ": "Riippuu CN-koodista ja tarkasta tuotteesta. " };

export const PATTERNS = [
  [/^COL-toimipaikan rooli ja voimassaolo \/ COL office role and validity:\s*(.*)$/, (m) => `Tullitoimipaikan rooli ja voimassaolo: ${m[1]}`],
  [/^(.+) is not the sandbox register's EORI for this company \((.+)\)$/, (m) => `${m[1]} ei vastaa tämän yrityksen EORI-numeroa testirekisterissä (${m[2]})`],
  [/^File export\s*(.*)$/, (m) => `Jätä vienti-ilmoitus ${m[1]}`],
  [/^Customer KYC is (.+); it must be approved before filing$/, (m) => `Asiakkaan tuntemistarkistus: ${KYC[m[1]] || m[1]}; se on hyväksyttävä ennen ilmoittamista`],
  [/^Customer sanctions status is (.+); compliance must clear it \(RULE K-1\)$/, (m) => `Asiakkaan pakotelistatarkistus: ${SANCTIONS[m[1]] || m[1]}; compliance-tiimin on hyväksyttävä se (RULE K-1)`],
  [/^Importer EORI (\S+) is valid in format$/, (m) => `Tuojan EORI ${m[1]} on muodoltaan oikein`],
  [/^Importer EORI (?:\S+ )?is missing or malformed: add it in Company \(apply in Tulli's EORI Number Registration Service if you have none\)$/,
    () => "Tuojan EORI puuttuu tai on virheellinen: lisää se kohtaan Yritys (hae Tullin EORI-rekisteröintipalvelussa, jos sinulla ei ole sitä)"],
  [/^Power of attorney on file: (direct|indirect) representation, valid to (\d{4}-\d{2}-\d{2})$/, (m) => `Valtakirja voimassa: ${REP[m[1]]} edustus, voimassa ${date(m[2])} asti`],
  [/^A valid power of attorney for (direct|indirect) representation covering imports is needed before filing \(Company → Power of attorney\)$/,
    (m) => `Ennen ilmoittamista tarvitaan voimassa oleva tuonnin kattava valtakirja (${REP[m[1]]} edustus) (Yritys → Valtakirja)`],
  [/^Delivery terms ([A-Z]{3}) (.+)$/, (m) => `Toimitusehto ${m[1]} ${m[2]}`],
  [/^Exchange rate (\S+) (\S+) per EUR \((.+), (.+)\)$/, (m) => `Valuuttakurssi ${m[1]} ${m[2]} / EUR (${m[3]}, ${m[4] === "n/a" ? "päivä ei tiedossa" : m[4]})`],
  [/^No customs exchange rate for (.+); enter Tulli's monthly rate$/, (m) => `Valuutalle ${m[1]} ei ole tullikurssia; syötä Tullin kuukausikurssi`],
  [/^This incoterm: freight and insurance to the EU border are not known; enter them under valuation$/, () => "Tämä toimituslauseke: rahti ja vakuutus EU:n rajalle puuttuvat. Syötä ne tullausarvon tietoihin."],
  [/^Customs office of presentation (.+)$/, (m) => `Esittämistullitoimipaikka ${m[1]}`],
  [/^Location of goods (.+)$/, (m) => `Tavaroiden sijainti: ${m[1]}`],
  [/^Transport document ([A-Z0-9][A-Z0-9/ -]{3,})$/, (m) => `Kuljetusasiakirja ${m[1]}`],
  [/^EORI verified manually by (.+) on (\d{4}-\d{2}-\d{2}): ([\s\S]*)$/, (m) => `EORI varmennettu käsin (${m[1]}, ${date(m[2])}): ${m[3]}`],
  [/^Seller '(.+)' needs a compliance decision: ([\s\S]*)$/, (m) => `Myyjä '${m[1]}' vaatii compliance-päätöksen: ${m[2]}`],
  [/^Origin ([A-Z]{2})$/, (m) => `Alkuperä ${m[1]}`],
  [/^CN (\S+) confirmed in TARIC$/, (m) => `CN ${m[1]} vahvistettu TARICista`],
  [/^CN (\S+) is not a (.+) code: choose the CN subdivision again$/, (m) => `CN ${m[1]} ei ole ${m[2]}-koodi: valitse CN-alanimike uudelleen`],
  [/^CBAM goods: importer's authorised CBAM declarant (.+)$/, (m) => `CBAM-tavaroita: tuojan valtuutettu CBAM-ilmoittaja ${m[1] === "status: under 50 t / year declared" ? "– ilmoitettu alle 50 t vuodessa" : m[1]}`],
  [/^CBAM goods on item\(s\) (.+): the importer needs an authorised CBAM declarant ID, or a statement that CBAM imports stay under 50 t a year$/,
    (m) => `CBAM-tavaroita nimikkeillä ${m[1]}: tuoja tarvitsee valtuutetun CBAM-ilmoittajan tunnuksen tai vakuutuksen, että CBAM-tuonti jää alle 50 tonnin vuodessa`],
  [/^Shipment from ([A-Z]{2}): mandatory compliance review, default deny \(RULE K-2\)$/, (m) => `Lähetys maasta ${m[1]}: pakollinen compliance-tarkistus, oletuksena hylätään (RULE K-2)`],
  [/^Declared gross weight ([\d,.]+) kg ties to the transport document$/, (m) => `Ilmoitettu bruttopaino ${num(m[1])} kg vastaa kuljetusasiakirjaa`],
  [/^Declared gross weight ([\d,.]+) kg differs ([\d.]+) % from the transport document \(([\d,.]+) kg\)$/,
    (m) => `Ilmoitettu bruttopaino ${num(m[1])} kg poikkeaa ${num(m[2])} % kuljetusasiakirjasta (${num(m[3])} kg)`],
  [/^The EU EORI registry did not find (.+); check the number, or a broker can record a documented manual verification$/,
    (m) => `EU:n EORI-rekisteri ei löytänyt numeroa ${m[1]}; tarkista numero, tai tulliasiantuntija voi kirjata dokumentoidun käsin tehdyn varmennuksen`],
  [/^(.+) is not a valid (.+) code\. Check the commodity code\.$/, (m) => `${m[1]} ei ole kelvollinen ${m[2]}-koodi. Tarkista tavaranimike.`],
  [/^CBAM goods \((.+), Reg \(EU\) 2023\/956 Annex I\): the importer needs CBAM authorised declarant status above 50 t net per year; record net mass, origin and installation\.$/,
    (m) => `CBAM-tavaroita (${m[1]}, asetus (EU) 2023/956, liite I): tuoja tarvitsee valtuutetun CBAM-ilmoittajan aseman, jos nettomäärä ylittää 50 t vuodessa; kirjaa nettomassa, alkuperä ja laitos.`],
  [/^Possible CBAM goods \((.+)\): depends on the CN8 code; check Annex I of Reg \(EU\) 2023\/956 for this subheading\.$/,
    (m) => `Mahdollisia CBAM-tavaroita (${m[1]}): riippuu CN8-koodista; tarkista asetuksen (EU) 2023/956 liite I tämän alanimikkeen osalta.`],
  [/^Dual-use hint: (.+)\. Check the item against Annex I of Reg \(EU\) 2021\/821; this is a hint, not a determination\.$/,
    (m) => `Kaksikäyttövihje: ${DUAL_USE[m[1]] || m[1]}. Tarkista nimike asetuksen (EU) 2021/821 liitteestä I; tämä on vihje, ei päätös.`],
  [/^Origin ([A-Z]{2}): Russia\/Belarus sanctions \(Reg 833\/2014, 765\/2006\) — mandatory compliance review; default deny\.$/,
    (m) => `Alkuperä ${m[1]}: Venäjän ja Valko-Venäjän pakotteet (asetukset 833/2014 ja 765/2006) – pakollinen compliance-tarkistus; oletuksena hylätään.`],
  [/^EU anti-dumping\/countervailing measures exist for (.+?) (originating in|consigned from) (.+?)( \(measure extended from China\))?; the duty estimate does not include them — check TARIC measures and additional codes\. (Only part of this CN code is covered \(TARIC subdivision decides\)\. |Depends on the CN code and the exact product\. )?([\s\S]*)$/,
    (m) => `EU:lla on polkumyynti- tai tasoitustulleja tuotteelle ${AD_PRODUCT[m[1]] || m[1]} (${m[2] === "originating in" ? "alkuperä" : "lähetetty maasta"} ${AD_COUNTRY[m[3]] || m[3]}${m[4] ? ", toimenpide laajennettu Kiinasta" : ""}); tulliarvio ei sisällä niitä – tarkista TARIC-toimenpiteet ja lisäkoodit. ${m[5] ? TD_LEAD[m[5]] : ""}${adDetail(m[6])}`],
  [/^([A-Z]{3}): the seller's price already includes carriage to the named place; costs after the EU border may be deducted if shown separately\.$/,
    (m) => `${m[1]}: myyjän hintaan sisältyy jo kuljetus nimettyyn paikkaan; EU:n rajan jälkeiset kulut voidaan vähentää, jos ne on eritelty.`],
  [/^Gross weight ties out across (\d+) documents \(([\d,.]+)–([\d,.]+) kg\)$/, (m) => `Bruttopaino täsmää ${m[1]} asiakirjassa (${num(m[2])}–${num(m[3])} kg)`],
  [/^Gross weight differs by ([\d.]+) % across documents \(([\d,.]+) vs ([\d,.]+) kg\); tolerance ([\d.]+) %([\s\S]*)$/,
    (m) => `Bruttopaino eroaa ${num(m[1])} % asiakirjojen välillä (${num(m[2])} / ${num(m[3])} kg); toleranssi ${num(m[4])} %${m[5].replace(/^\. Booked weight ([\d,.]+) kg differs by (\d+) % — update the booking if the documents are right$/, (_, b, p) => `. Varauksen paino ${num(b)} kg poikkeaa ${p} % — päivitä varaus, jos asiakirjat ovat oikein`)}`],
  [/^Only one document states the gross weight \(([\d,.]+) kg\)$/, (m) => `Vain yksi asiakirja ilmoittaa bruttopainon (${num(m[1])} kg)`],
  [/^Package counts differ: (.+)$/, (m) => `Kollimäärät eroavat: ${m[1].split(", ").map((p) => p.replace(/^(.+) (\d+)$/, (_, t, n) => `${docName(t)} ${n}`)).join(", ")}`],
  [/^Consignee differs between documents: (.+)$/, (m) => `Vastaanottaja eroaa asiakirjojen välillä: ${m[1]}`],
];

export function customsTextFi(text) {
  if (EXACT[text]) return EXACT[text];
  for (const [re, fn] of PATTERNS) {
    const m = text.match(re);
    if (m) return fn(m);
  }
  return null;
}
