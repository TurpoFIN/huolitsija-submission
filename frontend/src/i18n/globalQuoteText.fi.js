// Generated pricing prose: preserve amounts, dates and source names while translating the surrounding language.
export function globalQuoteTextFi(text, tr) {
  const replacements = [
    [/Derived from (.+) scaled by distance (.+) km and trade direction/g, 'Johdettu lähteestä $1; etäisyys $2 km ja kuljetussuunta'],
    [/TITR through tariff (.+) USD (.+)\/40' \+ rail model to\/from Khorgos and (.+)/g, 'TITR-läpikuljetustariffi $1, $2 USD/40 jalkaa sekä rautatiemalli: Khorgos ja $3'],
    [/intermodal rail (.+) km incl\. lifts/g, 'yhdistetty rautatiekuljetus $1 km, sisältää nostot'],
    [/China–North Europe \(weekly\)/g, 'Kiina–Pohjois-Eurooppa (viikoittainen)'],
    [/China–North America \(weekly\)/g, 'Kiina–Pohjois-Amerikka (viikoittainen)'],

    [/DHL Express Worldwide export from Finland, zone (.+)/g, 'DHL Express Worldwide, vienti Suomesta, vyöhyke $1'],
    [/No index level stored; route-length model at USD 2 200 per FEU scale/g, 'Indeksihavainto puuttuu; reittipituusmalli, lähtötaso 2 200 USD/FEU'],
    [/No air index stored; distance model/g, 'Lentorahti-indeksi puuttuu; etäisyysmalli'],
    [/Road model (.+): EUR ([\d.]+)\/km per full trailer/g, 'Maantiemalli $1: $2 EUR/km täydelle perävaunulle'],
    [/Rail model (.+): EUR ([\d.]+) per FEU-km/g, 'Rautatiemalli $1: $2 EUR/FEU-km'],
    [/Middle Corridor model EUR 0.75\/FEU-km \(no TITR tariff stored\)/g, 'Keskikäytävän malli 0,75 EUR/FEU-km (TITR-tariffia ei ole)'],
    [/Derived from (.+) scaled by route length (.+) nm, trade direction and lane density/g, 'Johdettu lähteestä $1; reitin pituus $2 mpk, kuljetussuunta ja reitin liikennetiheys'],
    [/kg chargeable \(1:5000\)/g, 'kg rahdituspaino (1:5000)'],
    [/kg chargeable \(1:6000\)/g, 'kg rahdituspaino (1:6000)'],
    [/kg chargeable \(333 kg\/m³, 1 850 kg\/ldm\)/g, 'kg rahdituspaino (333 kg/m³, 1 850 kg/lvm)'],
    [/small-shipment break ×/g, 'pienlähetyskerroin ×'],
    [/index all-in incl\. fuel\/security/g, 'indeksihinta sisältää polttoaine- ja turvalisän'],
    [/index all-in: base freight incl\. BAF\/CAF\/PSS/g, 'indeksihinta sisältää perusrahdin ja BAF/CAF/PSS-lisät'],
    [/Finnish export tariff of the destination zone/g, 'määrävyöhykkeen Suomen vientitariffi'],
    [/express model EUR/g, 'pikakuljetusmalli EUR'],
    [/groupage share of a truck/g, 'kappaletavaran osuus autosta'],
    [/container truck/g, 'konttiauto'],
    [/full trailer \(13\.6 ldm\)/g, 'täysi perävaunu (13,6 lvm)'],
    [/1\/25 of the 40' (level|charge)/g, '1/25 40 jalan kontin hinnasta'],
    [/feeder port/g, 'syöttösatama'],
    [/minimum charge/g, 'minimiveloitus'],
    [/ notice \(/g, ' tiedote ('],
    [/^Air via /, 'Lentorahti reitillä '], [/^Rail container via /, 'Konttijuna reitillä '],
    [/^Full truck$/, 'Täyskuorma'], [/^Road groupage$/, 'Maantien kappaletavara'],
    [/^Express courier$/, 'Pikakuriiri'], [/^(FCL|LCL) via /, '$1 reitillä '],
  ];
  let out = text;
  for (const [rx, to] of replacements) out = out.replace(rx, to);
  return out === text ? null : out;
}
