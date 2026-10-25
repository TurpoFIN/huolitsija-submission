import dictionaries from "@/i18n";
import terms, { TERMS_DATE, TERMS_VERSION } from "./terms";
import privacy, { PRIVACY_DATE, PRIVACY_VERSION } from "./privacy";

// /terms and /privacy publish version 1.1 in force, in both languages. "Lawyer review before go-live" belongs in the
// launch runbook (notes/launch-runbook-domain-addendum.md), not on the page (U17).
const text = (sections) => JSON.stringify(sections);
const DRAFT = /draft|luonnos|lawyer|juristi|asianajaja|legal review|juridinen tarkistus|pending review/i;

test.each([
  ["terms", terms, TERMS_VERSION, TERMS_DATE, 13],
  ["privacy", privacy, PRIVACY_VERSION, PRIVACY_DATE, 12],  // + partner sourcing (Sourcing Autopilot)
])("%s is version 1.1 with the same sections in Finnish and English", (_, doc, version, date, count) => {
  expect(version).toBe("1.1");
  expect(date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  expect(doc.fi.map((s) => s.id)).toEqual(doc.en.map((s) => s.id));
  expect(doc.en).toHaveLength(count);
  for (const s of [...doc.en, ...doc.fi]) {
    expect(s.title).toBeTruthy();
    expect(s.blocks.length).toBeGreaterThan(0);
  }
});

test("the terms rest on NSAB 2015 (PSYM 2015) in both languages", () => {
  for (const locale of ["en", "fi"]) {
    expect(text(terms[locale])).toContain("NSAB 2015");
    expect(text(terms[locale])).toContain("PSYM 2015");
  }
});

test("the privacy notice names the controller and the supervisory authority in both languages", () => {
  expect(text(privacy.en)).toMatch(/GDPR/);
  expect(text(privacy.en)).toMatch(/Data Protection Ombudsman/);
  expect(text(privacy.fi)).toMatch(/Rekisterinpitäjä/);
  expect(text(privacy.fi)).toMatch(/[Tt]ietosuojavaltuutetun/);
});

test("nothing on the legal pages calls the text a draft or pending review", () => {
  for (const doc of [terms, privacy]) for (const locale of ["en", "fi"]) expect(text(doc[locale])).not.toMatch(DRAFT);
  for (const locale of ["en", "fi"]) {
    const keys = Object.keys(dictionaries[locale]).filter((k) => /^pub\.(legal|terms|privacy)\./.test(k) || k === "domain.legal_version");
    expect(keys.length).toBeGreaterThan(5);
    for (const k of keys) expect(`${k}: ${dictionaries[locale][k]}`).not.toMatch(DRAFT);
  }
  expect(dictionaries.fi["domain.legal_version"]).toBe("Versio {v} · Voimassa {date} alkaen");
});

test("the privacy notice covers partner sourcing: legal basis, sources, opt-out and retention", () => {
  const fi = text(privacy.fi.filter((s) => s.id === "partner-sourcing"));
  const en = text(privacy.en.filter((s) => s.id === "partner-sourcing"));
  expect(en).toMatch(/6\(1\)\(f\)/); expect(en).toMatch(/YTJ/); expect(en).toMatch(/one-click/); expect(en).toMatch(/24 months/);
  expect(fi).toMatch(/oikeutettu etu/); expect(fi).toMatch(/YTJ/); expect(fi).toMatch(/kieltolistan/); expect(fi).toMatch(/24 kuukautta/);
});

test("notices name integrations without invented model or company identities", () => {
  for (const locale of ["en", "fi"]) {
    for (const name of ["Emergent", "OpenAI", "Stripe", "Postmark", "Twilio", "Redkik"]) expect(text(privacy[locale])).toContain(name);
    expect(text(privacy[locale])).toContain("/company");
    expect(text(terms[locale])).toContain("/company");
    expect(text(privacy[locale])).not.toMatch(/GPT-|staff|henkilöstömme/);
  }
});
