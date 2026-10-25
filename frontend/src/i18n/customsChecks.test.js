import { displayText } from "@/lib/localizedData";

// Live review defects (U7): check messages stayed English in the Finnish UI.
test("blocking checks seen on live are Finnish", () => {
  expect(displayText("Customer KYC is not_started; it must be approved before filing", "fi"))
    .toBe("Asiakkaan tuntemistarkistus: aloittamatta; se on hyväksyttävä ennen ilmoittamista");
  expect(displayText("Customer sanctions status is not_screened; compliance must clear it (RULE K-1)", "fi"))
    .toBe("Asiakkaan pakotelistatarkistus: tarkistamatta; compliance-tiimin on hyväksyttävä se (RULE K-1)");
  expect(displayText("Set the customs office of presentation (DE 5/26)", "fi")).toBe("Määritä esittämistullitoimipaikka (DE 5/26)");
  expect(displayText("Dual-use hint: integrated circuits. Check the item against Annex I of Reg (EU) 2021/821; this is a hint, not a determination.", "fi"))
    .toBe("Kaksikäyttövihje: integroidut piirit. Tarkista nimike asetuksen (EU) 2021/821 liitteestä I; tämä on vihje, ei päätös.");
});

test("numbers use Finnish separators and English stays English", () => {
  expect(displayText("Declared gross weight 1,288.7 kg differs 2.2 % from the transport document (1,260.0 kg)", "fi"))
    .toBe("Ilmoitettu bruttopaino 1 288,7 kg poikkeaa 2,2 % kuljetusasiakirjasta (1 260,0 kg)");
  expect(displayText("Package counts differ: commercial invoice 52, packing list 48", "fi")).toBe("Kollimäärät eroavat: kauppalasku 52, pakkausluettelo 48");
  expect(displayText("Set the customs office of presentation (DE 5/26)", "en")).toBe("Set the customs office of presentation (DE 5/26)");
  expect(displayText("Transport document number please", "fi")).toBe("Transport document number please");
});

// Live scan 502a0c7: seeded timeline notes and actors stayed English on the portal shipment page.
test("seeded timeline notes and actors are Finnish", () => {
  expect(displayText("Declaration accepted and released", "fi")).toBe("Tulli-ilmoitus hyväksytty ja tavara luovutettu");
  expect(displayText("POD received", "fi")).toBe("Vastaanottokuittaus saatu");
  expect(displayText("Customs broker", "fi")).toBe("Tulliasiantuntija");
  expect(displayText("Container train Vuosaari–Oulu", "fi")).toBe("Konttijuna Vuosaari–Oulu");
  expect(displayText("POD received", "en")).toBe("POD received");
});

test("seeded party hours and instructions are Finnish through the API projection", () => {
  const { localizePayload } = require("@/lib/localizedData");
  const party = { name: "Kymen Teräskauppa Oy", opening_hours: "Mon–Fri 7–15", instructions: "Call 30 min before arrival; forklift on site" };
  expect(localizePayload(party, "fi")).toEqual({ name: "Kymen Teräskauppa Oy", opening_hours: "ma–pe 7–15", instructions: "Soita 30 min ennen saapumista; trukki paikalla" });
  expect(localizePayload({ instructions: "Ring the bell twice" }, "fi").instructions).toBe("Ring the bell twice");
});
