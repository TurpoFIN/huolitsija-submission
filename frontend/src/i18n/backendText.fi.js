import { globalQuoteTextFi } from "./globalQuoteText.fi";
// Finnish for system texts the backend composes with values (route notes, quote line notes, exception and approval
// summaries). Exact texts live in storedText.fi.json; customs checks in customsChecks.fi.js. Every pattern here has a
// sample in backendText.triage.json that the tests run through displayText.

const num = (s) => s.replace(/,/g, " ").replace(/\.(?=\d+$)/, ",");
const date = (iso) => new Date(`${iso}T12:00:00Z`).toLocaleDateString("fi-FI", { timeZone: "Europe/Helsinki" });
const HOLD = { Customs: "tulli", Inspection: "tarkastus", Documents: "asiakirjat", Sanctions: "pakotteet", Lien: "pidätysoikeus",
  Payment: "maksu", Other: "muu" };
// Import VAT route in the customs filing approval summary: services/customs.py codes (older approvals) and words.
const VAT_ROUTE = { tilitys: "tilitysmenettely", paid_to_customs: "maksetaan Tullille", postponed: "tilitysmenettely",
  customs: "maksetaan Tullille", "postponed via VAT return": "tilitysmenettely", "payable to Customs": "maksetaan Tullille" };
const REP = { direct: "suora", indirect: "välillinen" };
// Plain amounts like 1234.50 (claims.py writes :.2f) in Finnish grouping: 1 234,50.
const eur = (s) => num(Number(s).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const CLAIM_TYPE = { damage: "vaurio", loss: "katoaminen", delay: "viivästys" };
// Quote review card (routes/quotes.py apply_approval_policy): reasons joined with ", ", and the cargo summary that
// services/pricing.cargo_summary writes ("2 × EUR pallet · 600 kg · 2.4 m³").
const QUOTE_REASON = { "total above the approval threshold": "summa ylittää hyväksyntärajan", "dangerous goods": "vaarallisia aineita",
  "no rates loaded for this lane": "reitille ei ole hinnastoa",
  "an option charges no freight (the Incoterm leaves the main carriage to the other party)":
    "vaihtoehto ei sisällä rahtia (toimituslauseke jättää päärahdin toiselle osapuolelle)" };
const QUOTE_REASONS_RE = new RegExp(`^(?:${Object.keys(QUOTE_REASON).map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})(?:, |$)`);
const PKG = { "EUR pallet": "EUR-lava", "FIN pallet": "FIN-lava", carton: "laatikko", crate: "puulaatikko", package: "kolli" };
const cargoFi = (s) => s.split(" · ").map((p) => p
  .replace(/^(\d+) × (EUR pallet|FIN pallet|carton|crate|package)$/, (_, n, k) => `${n} × ${PKG[k]}`)
  .replace(/^(\d+)\.(\d) m³$/, "$1,$2 m³")
  .replace(/^DG (.*?)\s*class (\S+)$/, (_, un, c) => `VAK ${un ? `${un} ` : ""}luokka ${c}`)).join(" · ");
const quoteReasonsFi = (text) => {
  const parts = [];
  let rest = text;
  while (rest) {
    const m = rest.match(QUOTE_REASONS_RE);
    if (!m) return null;
    const key = m[0].replace(/, $/, "");
    parts.push(QUOTE_REASON[key]);
    rest = rest.slice(m[0].length);
  }
  return parts.join(", ");
};

const PATTERNS = [
  // Owner-queue card titles with a customs declaration's LRN (kept verbatim).
  [/^Answer customs query ?(\S*)$/, (m) => `Vastaa tullin kyselyyn ${m[1]}`.trimEnd()],
  [/^Review export ?(\S*)$/, (m) => `Tarkista vienti-ilmoitus ${m[1]}`.trimEnd()],
  // Ferry purchase/booking reasons; operator names and route identifiers remain source data.
  [/^ferry crossing needs a separate booking with (.+?)(; route is outside the accepted quote's coverage)?$/,
    (m) => `Lauttamatka tarvitsee erillisen varauksen: ${m[1]}${m[2] ? "; reitti ei kuulu hyväksytyn tarjouksen piiriin" : ""}`],
  [/^ferry crossing needs a separate booking; operator must be confirmed(; route is outside the accepted quote's coverage)?$/,
    (m) => `Lauttamatka tarvitsee erillisen varauksen; operaattori on vahvistettava${m[1] ? "; reitti ei kuulu hyväksytyn tarjouksen piiriin" : ""}`],
  [/^Ferry (.+)–(.+)$/, (m) => `Lautta ${m[1]}–${m[2]}`],
  [/^credit note (.+) nets the dispute within tolerance$/, (m) => `Hyvityslasku ${m[1]} kattaa riitautetun erotuksen sallituissa rajoissa`],
  [/^(.+); (\d+) d past due$/, (m, tr) => `${tr(m[1])}; ${m[2]} päivää erääntynyt`],
  [/^Swissport HEL (import|export) THC$/, (m) => `Swissport HEL ${m[1] === "import" ? "tuonnin" : "viennin"} terminaalikäsittely (THC)`],
  [/^Provider tariff could not be verified \([^)]+\)$/, () => "Kuljetusliikkeen tariffia ei voitu vahvistaa."],
  [/^(.+) wrote in the thread of (.+)$/, (m) => `${m[1]} kirjoitti asiakkaan ${m[2]} sähköpostiketjuun`],
  // Demo world customer-broker release: the native tracking note names the simulated document
  [/^World-simulated customer broker release; document (\S*)$/, (m) => `Simuloitu asiakkaan tulliasiamiehen vapautus; asiakirja ${m[1]}`],
  // Demo world booking confirmation (services/demo_world/observer.py)
  [/^Booking confirmed · ref ?(\S*)$/, (m) => `Varaus vahvistettu · viite ${m[1]}`.trimEnd()],
  // Demo offer option service name (services/demo_autoquote/driver.py)
  [/^Huolitsija door-to-door (road|sea|air|rail) freight$/, (m) => `Huolitsijan ovelta ovelle -${{ road: "maantie", sea: "meri", air: "lento", rail: "rautatie" }[m[1]]}kuljetus`],
  // Demo owner-queue card when every partner rate expires before the ready date (services/demo_autoquote/driver.py)
  [/^(\d+) of (\d+) partners quoted; every rate expires before the ready date (\d{4}-\d{2}-\d{2})\. Best: (.+) EUR ([\d,]+\.\d{2}), valid until (\d{4}-\d{2}-\d{2})\.$/,
    (m) => `${m[1]}/${m[2]} kumppanista antoi hinnan; jokainen hinta vanhenee ennen valmiuspäivää ${date(m[3])}. Paras: ${m[4]} ${eur(m[5].replace(/,/g, ""))} EUR, voimassa ${date(m[6])} asti.`],
  // Demo owner-queue card when a partner's real reply beats its accepted rate card (services/demo_autoquote/driver.py)
  [/^(.+) replied EUR ([\d,]+\.\d{2}) after Huolitsija had accepted an earlier rate from them; (\d+) of (\d+) partners quoted\. A person decides which rate to buy\.$/,
    (m) => `${m[1]} vastasi ${eur(m[2].replace(/,/g, ""))} EUR, vaikka Huolitsija oli jo hyväksynyt heiltä aiemman hinnan; ${m[3]}/${m[4]} kumppanista antoi hinnan. Ihminen päättää, mikä hinta ostetaan.`],
  // Demo owner-queue card when every partner that quoted has declined (services/demo_autoquote/driver.py)
  [/^(\d+) of (\d+) partners declined; the other (\d+) have no usable rate for this request\. A person decides\.$/,
    (m) => `${m[1]}/${m[2]} kumppanista kieltäytyi; muilla ${m[3]} kumppanilla ei ole tähän pyyntöön käyttökelpoista hintaa. Ihminen päättää.`],
  // Route notes (services/geo.py, path_source great_circle / osrm)
  [/^(.+); straight line shown$/, (m, tr) => `${tr(m[1])}; näytetään suora viiva`],
  [/^Road router unavailable \(([\s\S]*)\)$/, (m) => `Tiereititin ei ole käytettävissä (${m[1]})`],
  [/^Routing failed \(([\s\S]*)\)$/, (m) => `Reititys epäonnistui (${m[1]})`],
  [/^No rail line within 90 km \(origin (\d+) km, destination (\d+) km\)$/,
    (m) => `Rautatietä ei ole 90 km:n säteellä (lähtöpaikasta ${m[1]} km, määränpäästä ${m[2]} km)`],
  [/^Road route includes (\d+) km by ferry; book it as a separate sea leg$/,
    (m) => `Maantiereittiin sisältyy ${m[1]} km lauttamatkaa; varaa se erillisenä meriosuutena`],
  // Shipment timeline notes
  [/^Leg (\d+) accepted by (.+)$/, (m) => `Osuuden ${m[1]} hyväksyi ${m[2]}`],
  [/^POD signed by (.+)$/, (m) => `Vastaanottokuittauksen allekirjoitti ${m[1]}`],
  [/^(Customs|Inspection|Documents|Sanctions|Lien|Payment|Other) hold released$/, (m) => `Pidätys vapautettu (${HOLD[m[1]]})`],
  // Quote and invoice line notes (services/pricing.py)
  [/^(\d+) × full trailer \(13\.6 ldm\)$/, (m) => `${m[1]} × täysi perävaunu (13,6 lvm)`],
  [/^(.+), minimum charge$/, (m, tr) => `${tr(m[1])}, minimiveloitus`],
  [/^items beyond (\d+)$/, (m) => `lisänimikkeet (yli ${m[1]})`],
  [/^ICC \(([ABC])\), insured value ([\d,]+) EUR \(110 %\)$/, (m) => `ICC (${m[1]}), vakuutusarvo ${num(m[2])} EUR (110 %)`],
  [/^Paid by the (seller|buyer) under (.+)$/, (m) => `Maksaa ${m[1] === "seller" ? "myyjä" : "ostaja"} toimituslausekkeen ${m[2]} mukaan`],
  [/^([\d.]+) % of ([\d,]+) EUR, payable to Customs$/, (m) => `${num(m[1])} % summasta ${num(m[2])} EUR, maksetaan Tullille`],
  // Approval and exception summaries
  [/^(.+): (\d+) items, customs value ([\d,.]+) EUR, duty ([\d,.]+) EUR, VAT ([\d,.]+) EUR \(([^)]+)\), (direct|indirect) representation$/,
    (m) => `${m[1]}: ${m[2]} nimikettä, tullausarvo ${num(m[3])} EUR, tulli ${num(m[4])} EUR, ALV ${num(m[5])} EUR `
      + `(${VAT_ROUTE[m[6]] || m[6]}), ${REP[m[7]]} edustus`],
  [/^(.+) · (damage|loss|delay) · claimed (\d+\.\d{2}) EUR · propose (?:(\d+\.\d{2}) EUR|rejection)$/,
    (m) => `${m[1]} · ${CLAIM_TYPE[m[2]]} · vaadittu ${eur(m[3])} EUR · ehdotus: ${m[4] ? `${eur(m[4])} EUR` : "hylkäys"}`],
  [/^(.+): ([\d.]+) EUR due (\d{4}-\d{2}-\d{2})\.$/, (m) => `${m[1]}: ${num(m[2])} EUR, eräpäivä ${date(m[3])}.`],
  [/^AI assessment failed: ([\s\S]+)$/, (m) => `AI-arvio epäonnistui: ${m[1]}`],
  // Document reading and screening
  [/^The file could not be read \(([\s\S]*)\)\. Upload the original file\.$/, (m) => `Tiedostoa ei voitu lukea (${m[1]}). Lataa alkuperäinen tiedosto.`],
  [/^The AI reader did not return a usable answer \(([\s\S]*)\)\. Retry, or enter the fields by hand\.$/,
    (m) => `AI-lukija ei palauttanut käyttökelpoista vastausta (${m[1]}). Yritä uudelleen tai syötä kentät käsin.`],
  [/^No sanctions list could be loaded \(([\s\S]*)\)\. Screening was not performed\.$/,
    (m) => `Pakotelistoja ei voitu ladata (${m[1]}). Seulontaa ei tehty.`],
  [/^File import declaration (\S+) \((.*)\)$/, (m) => `Lähetä tuonti-ilmoitus ${m[1]} (${m[2]})`],
  [/^Rate expired on (\d{4}-\d{2}-\d{2})$/, (m) => `Hinta vanheni ${date(m[1])}`],
  [/^Rate valid from (\d{4}-\d{2}-\d{2})$/, (m) => `Hinta voimassa ${date(m[1])} alkaen`],
  [/^([\d.]+) % of customs value ([\d,]+) EUR \(your rate; final at classification\)$/,
    (m) => `${num(m[1])} % tullausarvosta ${num(m[2])} EUR (ilmoittamasi tullikanta; lopullinen luokittelussa)`],
  // Printed HS code on the invoice vs the suggestion (services/hs.py hint check)
  [/^Printed code (\S+) does not exist in (.+)\.$/, (m) => `Painettua nimikettä ${m[1]} ei ole nimikkeistössä ${m[2]}.`],
  [/^Printed code (\S+) differs from the suggested subheading (\S+); a person must decide\.$/,
    (m) => `Painettu nimike ${m[1]} poikkeaa ehdotetusta alanimikkeestä ${m[2]}; henkilön on päätettävä.`],
  // CN subdivision notes (services/hs.py resolve_cn, RULE H-1)
  [/^(\d{4} \d{2} \d{2}) is not a (CN \d{4}) code under (\d+)\. ([\s\S]+)$/,
    (m, tr) => `${m[1]} ei ole ${m[2]} -nimikkeistössä nimikkeen ${m[3]} alla. ${tr(m[4])}`],
  [/^(\d+) has one CN subdivision \((\d+)\); confirm it in TARIC \(RULE H-1\)\.$/,
    (m) => `Nimikkeellä ${m[1]} on yksi CN-alanimike (${m[2]}); vahvista se TARICista (sääntö H-1).`],
  [/^Choose the CN subdivision of (\d+) \((\d+) options\) and confirm it in TARIC \(RULE H-1\)\.$/,
    (m) => `Valitse nimikkeen ${m[1]} CN-alanimike (${m[2]} vaihtoehtoa) ja vahvista se TARICista (sääntö H-1).`],
  [/^No (CN \d{4}) subdivision found for (\d+); choose the CN\/TARIC code in TARIC \(RULE H-1\)\.$/,
    (m) => `Nimikkeelle ${m[2]} ei löytynyt ${m[1]} -alanimikettä; valitse CN/TARIC-koodi TARICista (sääntö H-1).`],
  [/^AI classification failed \(([\s\S]*)\); pick the code from the candidates\.$/,
    (m) => `AI-luokittelu epäonnistui (${m[1]}); valitse nimike ehdokkaista.`],
  [/^Screened without (.+) \(list not loaded\); re-screen when it is available\.$/,
    (m) => `Seulottu ilman luetteloa ${m[1]} (luetteloa ei ladattu); seulo uudelleen, kun se on saatavilla.`],
  // Notifications (bell): core/notify.py callers
  [/^Customs draft (\S+) \((.*)\)$/, (m) => `Tulli-ilmoitusluonnos ${m[1]} (${m[2]})`],
  [/^(\d+) goods items drafted for (.+)$/, (m) => `${m[1]} tavaranimikettä luonnosteltu: ${m[2]}`],
  [/^Check the goods data for (.+)$/, (m) => `Tarkista tavaratiedot: ${m[1] === "your shipment" ? "lähetyksesi" : m[1]}`],
  [/^(\S+) ready for review$/, (m) => `${m[1]} valmis tarkistettavaksi`],
  [/^(.+) confirmed the goods data$/, (m) => `${m[1]} vahvisti tavaratiedot`],
  [/^Released by Customs: (.+)$/, (m) => `Tulli luovutti: ${m[1]}`],
  [/^Invoice draft to review: (.+)$/, (m) => `Tarkistettava laskuluonnos: ${m[1]}`],
  [/^Payment needs review: (.+)$/, (m) => `Maksu vaatii tarkistuksen: ${m[1]}`],
  [/^Quote to review: (.+)$/, (m) => `Tarkistettava tarjous: ${m[1]}`],
  [/^Quote (\S+) could not be confirmed$/, (m) => `Tarjousta ${m[1]} ei voitu vahvistaa`],
  [/^Quote (Q-\d{4}-\d+) · (.+)$/, (m) => `Tarjous ${m[1]} · ${m[2]}`],
  [/^([^·]+?) · (.+) · (\d+) options$/, (m) => `${m[1]} · ${cargoFi(m[2])} · ${m[3]} ${m[3] === "1" ? "vaihtoehto" : "vaihtoehtoa"}`],
  [/^(?:total above the approval threshold|dangerous goods|no rates loaded for this lane|an option charges no freight \(the Incoterm leaves the main carriage to the other party\))(?:, .+)?$/,
    (m) => quoteReasonsFi(m[0])],
  [/^Refund on (.+)$/, (m) => `Palautus: ${m[1]}`],
  [/^([\d.]+) EUR refunded in Stripe\. Issue a credit note if the charge is not owed\.$/,
    (m) => `${num(m[1])} EUR palautettu Stripessä. Laadi hyvityslasku, jos veloitus ei ollut aiheellinen.`],
  [/^Overdue: (.+)$/, (m) => `Erääntynyt: ${m[1]}`],
  [/^(.+) · (.+) was due (\d{4}-\d{2}-\d{2})$/, (m) => `${m[1]} · ${m[2]} erääntyi ${date(m[3])}`],
  // Autopilot job queue: why a job waits (core/jobs.JobDeferred reasons)
  [/^waiting for carrier confirmation on legs (.+)$/, (m) => `odotetaan kuljetusliikkeen vahvistusta osuuksille ${m[1]}`],
  [/^(\S+) is being handled by another worker$/, (m) => `${m[1]}: toinen työprosessi käsittelee tätä jo`],
  // Integrations (Ops settings)
  [/^Tulli message exchange \((.+)\) over mutual TLS with XML signature$/,
    (m) => `Tullin sanomanvaihto (${m[1]}) kaksisuuntaisella TLS-yhteydellä ja XML-allekirjoituksella`],
];

/** Finnish for a composed backend text, or null. `tr` translates an inner part with the full displayText chain. */
export function backendTextFi(text, tr) {
  const global = globalQuoteTextFi(text, tr);
  if (global) return global;
  for (const [re, fn] of PATTERNS) {
    const m = text.match(re);
    if (m) return fn(m, tr);
  }
  return null;
}
