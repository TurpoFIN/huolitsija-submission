import dictionaries from "@/i18n";
import { translate } from "@/lib/i18n";
import { INTENTS, THREAD_STATE, decisionText, intentLabel, reasonText, stateLabel, tagLabel } from "./strings";

const fi = (key, vars) => translate(dictionaries, "fi", key, vars);
const en = (key, vars) => translate(dictionaries, "en", key, vars);

test("every e-mail key exists in both languages", () => {
  const enKeys = Object.keys(dictionaries.en).filter((k) => k.startsWith("email."));
  const missing = enKeys.filter((k) => !(k in dictionaries.fi));
  expect(enKeys.length).toBeGreaterThan(100);
  expect(missing).toEqual([]);
});

test("intents and thread states have Finnish labels", () => {
  expect(INTENTS.map((i) => intentLabel(fi, i))).toEqual([
    "Tarjouspyyntö", "Varaus", "Varauksen tiedot", "Asiakirjat", "Kysymys", "Muutospyyntö", "Peruutus", "Reklamaatio",
    "Maksu", "Kiitos", "Uutiskirjeen peruutus", "Muu",
  ]);
  Object.keys(THREAD_STATE).forEach((s) => expect(stateLabel(fi, s)).not.toMatch(/^email\./));
  expect(stateLabel(fi, "something_new")).toBe("Uusi");
});

test("backend decision strings read as short sentences, never raw keys", () => {
  expect(decisionText(fi, "quote:Q-2026-000123:sent")).toBe("Tarjous Q-2026-000123: Valmis");
  expect(decisionText(fi, "booked:HUO-2026-000061")).toBe("Varattu HUO-2026-000061");
  expect(decisionText(fi, "kyc_link_sent")).toBe("Tunnistautumislinkki lähetetty");
  expect(decisionText(fi, "possible_prompt_injection")).toBe("Mahdollinen kehoteinjektio – viestiä ei käsitelty automaattisesti");
  expect(decisionText(fi, "intent:complaint")).toBe("Vaatii henkilön · Reklamaatio");
  expect(decisionText(fi, "automatic:auto-replied")).toBe("Automaattinen vastaus, ei käsitelty");
  expect(decisionText(fi, "documents_filed:2")).toBe("2 asiakirjaa tallennettu");
  expect(decisionText(fi, "documents_filed:1")).toBe("1 asiakirja tallennettu");
  expect(decisionText(fi, "something_unknown")).toBeNull();
  expect(reasonText(en, "ambiguous")).toBe("Unclear message");
  expect(reasonText(fi, "never_seen_reason")).toBe("Vaatii henkilön");
});

test("outbound tags map to labels; unknown tags stay hidden", () => {
  expect(tagLabel(fi, "email_quote")).toBe("Tarjous");
  expect(tagLabel(fi, "email_kyc_pending")).toBe("Tunnistautumislinkki");
  expect(tagLabel(fi, "some_new_tag")).toBeNull();
});
