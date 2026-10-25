import { displayText } from "@/lib/localizedData";
import triage from "@/i18n/backendText.triage.json";
import dictionaries from "@/i18n";

const { en, fi } = dictionaries;

// backend/tests/test_core_display_text_fi.py requires every English display text from the backend to be catalogued
// or triaged; here every "pattern" entry's sample must really come out in Finnish, and match the backend text's shape.
const shape = (text) => new RegExp(`^${text.split("{…}").map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("[\\s\\S]+")}$`);

test("finance history preserves identifiers and translates system reasons", () => {
  expect(displayText("credit note CN-2026-001 nets the dispute within tolerance", "fi"))
    .toBe("Hyvityslasku CN-2026-001 kattaa riitautetun erotuksen sallituissa rajoissa");
  expect(displayText("invoice paid; 35 d past due", "fi"))
    .toBe("Lasku maksettu; 35 päivää erääntynyt");
  expect(displayText("Customer's original note; 3 d past due", "fi"))
    .toBe("Customer's original note; 3 päivää erääntynyt");
  expect(displayText("invoice paid; 35 d past due", "en")).toBe("invoice paid; 35 d past due");
});

test.each(Object.entries(triage).filter(([, e]) => e.status === "pattern"))("pattern: %s", (text, entry) => {
  expect(entry.sample).toMatch(shape(text));
  const out = displayText(entry.sample, "fi");
  expect(out).not.toBe(entry.sample);
  expect(displayText(entry.sample, "en")).toBe(entry.sample);
});

test("owner-queue titles keep the declaration LRN and booking reference verbatim", () => {
  expect(displayText("Answer customs query 26FI0000HUO000123", "fi")).toBe("Vastaa tullin kyselyyn 26FI0000HUO000123");
  expect(displayText("Review export 26FI0000HUO000123", "fi")).toBe("Tarkista vienti-ilmoitus 26FI0000HUO000123");
  expect(displayText("Booking confirmed · ref KP-2026-1104-002", "fi")).toBe("Varaus vahvistettu · viite KP-2026-1104-002");
  expect(displayText("Read carrier booking reply", "fi")).toBe("Lue kuljetusliikkeen varausvastaus");
  expect(displayText("Review export 26FI0000HUO000123", "en")).toBe("Review export 26FI0000HUO000123");
});

test("route notes from services/geo.py are Finnish", () => {
  const notes = {
    "Routed around closed Russian, Belarusian and Ukrainian airspace": "Reitti kiertää suljetun Venäjän, Valko-Venäjän ja Ukrainan ilmatilan",
    "Via the Kiel Canal": "Kielin kanavan kautta",
    "Routing failed (searoute error); straight line shown": "Reititys epäonnistui (searoute error); näytetään suora viiva",
    "No connected rail corridor between origin and destination; straight line shown":
      "Lähtö- ja määräpaikan välillä ei ole yhtenäistä rautatiekäytävää; näytetään suora viiva",
    "No rail line within 90 km (origin 120 km, destination 4 km); straight line shown":
      "Rautatietä ei ole 90 km:n säteellä (lähtöpaikasta 120 km, määränpäästä 4 km); näytetään suora viiva",
  };
  for (const [src, want] of Object.entries(notes)) expect(displayText(src, "fi")).toBe(want);
});

test("coded entries name a key that exists in both languages", () => {
  const keys = Object.values(triage).filter((e) => e.status === "coded")
    .flatMap((e) => e.reason.match(/\b(?:quotes\.warn|pq\.blocker|shipments\.ms)\.[a-z_.]+[a-z]/g) || []);
  expect(keys.length).toBeGreaterThan(5);
  for (const k of keys) {
    expect(en[k]).toBeTruthy();
    expect(fi[k]).toBeTruthy();
  }
});

test("timeline source and autonomy log labels exist in both languages", () => {
  for (const k of ["domain.actions_log", "shipments.source.billing", "shipments.source.customs"]) {
    expect(en[k]).toBeTruthy();
    expect(fi[k]).toBeTruthy();
  }
  expect(fi["domain.actions_log"]).not.toBe(fi["domain.log"]);
});

test("customs filing approval summary names the VAT route in Finnish (stored codes and words)", () => {
  const s = (route) => `Pyöräosa Testi 10241426 Oy: 5 items, customs value 25,392.85 EUR, duty 1,055.84 EUR, VAT 6,881.43 EUR (${route}), direct representation`;
  const fi = "Pyöräosa Testi 10241426 Oy: 5 nimikettä, tullausarvo 25 392,85 EUR, tulli 1 055,84 EUR, ALV 6 881,43 EUR";
  expect(displayText(s("paid_to_customs"), "fi")).toBe(`${fi} (maksetaan Tullille), suora edustus`);
  expect(displayText(s("payable to Customs"), "fi")).toBe(`${fi} (maksetaan Tullille), suora edustus`);
  expect(displayText(s("tilitys"), "fi")).toBe(`${fi} (tilitysmenettely), suora edustus`);
  expect(displayText(s("postponed via VAT return"), "fi")).toBe(`${fi} (tilitysmenettely), suora edustus`);
  expect(displayText(s("payable to Customs"), "en")).toBe(s("payable to Customs"));
});

test("claim decision approval summary (services/claims.py) is Finnish with Finnish amounts", () => {
  expect(displayText("Pyöräosa Testi Oy · damage · claimed 900.00 EUR · propose 600.00 EUR", "fi"))
    .toBe("Pyöräosa Testi Oy · vaurio · vaadittu 900,00 EUR · ehdotus: 600,00 EUR");
  expect(displayText("Pyöräosa Testi Oy · delay · claimed 12500.50 EUR · propose rejection", "fi"))
    .toBe("Pyöräosa Testi Oy · viivästys · vaadittu 12 500,50 EUR · ehdotus: hylkäys");
  expect(displayText("A · B Oy · loss · claimed 1234567.00 EUR · propose 1000.00 EUR", "fi"))
    .toBe("A · B Oy · katoaminen · vaadittu 1 234 567,00 EUR · ehdotus: 1 000,00 EUR");
  expect(displayText("Pyöräosa Testi Oy · damage · claimed 900.00 EUR · propose 600.00 EUR", "en"))
    .toBe("Pyöräosa Testi Oy · damage · claimed 900.00 EUR · propose 600.00 EUR");
});

test("CN subdivision notes (services/hs.py, RULE H-1) are Finnish, with the invalid-code prefix", () => {
  expect(displayText("8714 99 99 is not a CN 2026 code under 871499. Choose the CN subdivision of 871499 (4 options) and confirm it in TARIC (RULE H-1).", "fi"))
    .toBe("8714 99 99 ei ole CN 2026 -nimikkeistössä nimikkeen 871499 alla. Valitse nimikkeen 871499 CN-alanimike (4 vaihtoehtoa) ja vahvista se TARICista (sääntö H-1).");
  expect(displayText("871200 has one CN subdivision (87120030); confirm it in TARIC (RULE H-1).", "fi"))
    .toBe("Nimikkeellä 871200 on yksi CN-alanimike (87120030); vahvista se TARICista (sääntö H-1).");
  expect(displayText("No CN 2026 subdivision found for 999999; choose the CN/TARIC code in TARIC (RULE H-1).", "fi"))
    .toBe("Nimikkeelle 999999 ei löytynyt CN 2026 -alanimikettä; valitse CN/TARIC-koodi TARICista (sääntö H-1).");
  expect(displayText("Proposed by AI from the CN 2026 subdivisions; confirm the CN/TARIC code against TARIC on the declaration date (RULE H-1)", "fi"))
    .toMatch(/^AI:n ehdotus CN 2026 -alanimikkeistä/);
});

test("every package type the backend stores has a count label in both languages (cargo panel: '58 laatikkoa')", () => {
  const fs = require("fs");
  const path = require("path");
  const pricing = fs.readFileSync(path.resolve(__dirname, "../../../backend/services/pricing.py"), "utf8");
  const quoteCodes = [...(/^PACKAGE_LABEL = \{([^}]*)\}/m.exec(pricing)[1]).matchAll(/"([a-z_]+)":/g)].map((m) => m[1]);
  const seedCodes = ["pallets", "cartons", "boxes", "bags", "crates", "coils", "bundles"];
  expect(quoteCodes).toEqual(expect.arrayContaining(["carton", "pallet_eur", "other"]));
  for (const code of [...quoteCodes, ...seedCodes]) {
    expect([code, dictionaries.en[`enum.package_type.${code}`]]).toEqual([code, expect.any(String)]);
    expect([code, dictionaries.fi[`enum.package_type.${code}`]]).toEqual([code, expect.any(String)]);
  }
  expect(dictionaries.fi["enum.package_type.carton"]).toBe("laatikkoa");
});

test("the quote review card (routes/quotes.py approval) reads in Finnish", () => {
  const card = {
    "Quote Q-2026-000062 · Hamburg → Helsinki Vuosaari Harbour": "Tarjous Q-2026-000062 · Hamburg → Helsinki Vuosaari Harbour",
    "Pohjolan Kaluste Oy · 2 × EUR pallet · 600 kg · 2.4 m³ · 2 options": "Pohjolan Kaluste Oy · 2 × EUR-lava · 600 kg · 2,4 m³ · 2 vaihtoehtoa",
    "total above the approval threshold, an option charges no freight (the Incoterm leaves the main carriage to the other party)":
      "summa ylittää hyväksyntärajan, vaihtoehto ei sisällä rahtia (toimituslauseke jättää päärahdin toiselle osapuolelle)",
    "dangerous goods": "vaarallisia aineita",
  };
  for (const [src, want] of Object.entries(card)) expect(displayText(src, "fi")).toBe(want);
});

test("Autopilot job waiting reasons (core/jobs JobDeferred) and agent names are Finnish", () => {
  const want = {
    "waiting for carrier confirmation on legs 1, 2": "odotetaan kuljetusliikkeen vahvistusta osuuksille 1, 2",
    "invoice:0de3257b-4d7e-4de8-b99c-f6b4d90176df is being handled by another worker":
      "invoice:0de3257b-4d7e-4de8-b99c-f6b4d90176df: toinen työprosessi käsittelee tätä jo",
    "waiting for Tulli's decision before the customs lines can be invoiced": "odotetaan Tullin päätöstä ennen kuin tullirivit voidaan laskuttaa",
    "waiting for the proof of delivery": "odotetaan luovutuskuittausta",
    "Autopilot · Invoicing": "Autopilotti · Laskutus",
    "Autopilot · Customs": "Autopilotti · Tulli",
  };
  for (const [src, fi] of Object.entries(want)) {
    expect(displayText(src, "fi")).toBe(fi);
    expect(displayText(src, "en")).toBe(src);
  }
});
