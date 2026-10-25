import { localizePayload } from "./localizedData";

// GET /api/notifications as core/notify.py stores it (N4): the bell shows the reader's UI language.
const payload = {
  unread: 2,
  items: [
    { title: "Customs declaration drafted", body: "Check the goods data for HUO-2026-000061",
      title_i18n: { fi: "Tulli-ilmoitus luonnosteltu", en: "Customs declaration drafted" },
      body_i18n: { fi: "Tarkista lähetyksen HUO-2026-000061 tavaratiedot", en: "Check the goods data for HUO-2026-000061" } },
    { title: "Uusi viesti: Kysymys toimituksesta", body: "Milloin kontti saapuu?",
      title_i18n: { fi: "Uusi viesti: Kysymys toimituksesta", en: "New message: Kysymys toimituksesta" },
      body_i18n: { fi: "Milloin kontti saapuu?", en: "Milloin kontti saapuu?" } },
  ],
};

test("the bell shows Finnish titles and bodies in the Finnish UI", () => {
  const [a, b] = localizePayload(payload, "fi").items;
  expect([a.title, a.body]).toEqual(["Tulli-ilmoitus luonnosteltu", "Tarkista lähetyksen HUO-2026-000061 tavaratiedot"]);
  expect([b.title, b.body]).toEqual(["Uusi viesti: Kysymys toimituksesta", "Milloin kontti saapuu?"]);
});

test("the bell shows English in the English UI, and the customer's own words as written", () => {
  const [a, b] = localizePayload(payload, "en").items;
  expect([a.title, a.body]).toEqual(["Customs declaration drafted", "Check the goods data for HUO-2026-000061"]);
  expect([b.title, b.body]).toEqual(["New message: Kysymys toimituksesta", "Milloin kontti saapuu?"]);
});
