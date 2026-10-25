// Renders the ops recourse block for the states ops meet (no recourse yet, approved but awaiting the carrier contract,
// pending approval) and checks the texts and amounts a person reads in Finnish and English, not the internals.
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { TextDecoder, TextEncoder } from "util";
import { I18nProvider } from "@/lib/i18n";

Object.assign(global, { TextEncoder, TextDecoder });  // react-router 7 needs them; jsdom has none
const { MemoryRouter } = require("react-router-dom");
const RecoursePanel = require("./RecoursePanel").default;

jest.mock("@/lib/auth", () => ({ useAuth: () => ({ user: { id: "u1", permissions: ["claims.approve", "billing.write"] } }) }));
jest.mock("@/lib/api", () => ({ apiPost: jest.fn() }));

const letter = { subject: "Takautumisvaatimus: lähetys HUO-1, CMR CMR-1 (HUO-CLM-1)", body: "Vaadittu määrä: 999,60 €" };
const base = {
  carrier_id: "car-1", carrier_name: "Tie Kuljetus Oy", regime: "CMR", regime_label: "CMR", cap_sdr: 833, cap_eur_cents: 99960,
  cap_basis: "CMR art. 23: 8.33 SDR × 100 kg gross", paid_cents: 200000, amount_cents: 99960, recovered_cents: 0,
  transport_doc_label: "CMR", transport_doc_no: "CMR-1", letter_fi: letter, letter_en: { subject: "Recourse claim", body: "x" },
};

function html(recourse, locale = "fi") {
  localStorage.setItem("huo.locale", locale);
  return renderToStaticMarkup(
    <MemoryRouter><I18nProvider><RecoursePanel claim={{ id: "cl-1", status: "paid", recourse }} /></I18nProvider></MemoryRouter>,
  );
}

test("a paid claim without recourse offers to draft one", () => {
  const out = html(undefined);
  expect(out).toContain("Laadi takautumisvaatimus");
  expect(out).toContain("ei ole vielä laadittu takautumisvaatimusta");
});

test("approved but no carrier contract: status, reason, amounts, send and recovery controls", () => {
  const out = html({ ...base, status: "approved", awaiting: "carrier_contract", approval_id: "ap-1" });
  expect(out).toContain("Hyväksytty");
  expect(out).toContain("Odottaa kuljetusliikkeen sopimusta");
  expect(out).toContain("Tie Kuljetus Oy");
  expect(out).toMatch(/999,60\s€/);
  expect(out).toMatch(/2\s000,00\s€/);
  expect(out).toContain("Lähetä kirje");
  expect(out).toContain("Kirjaa suoritus");
  expect(out).toContain("/ops/carriers/car-1");
  expect(out).toContain(letter.subject);
});

test("pending approval links to the owner-queue card; English labels in English", () => {
  const out = html({ ...base, status: "pending_approval", approval_id: "ap-9" }, "en");
  expect(out).toContain("Awaiting approval");
  expect(out).toContain("/ops/approvals?focus=ap-9");
  expect(out).not.toContain("Record recovery");
});

test("the cap formula follows the language: Finnish formula in Finnish, English in English", () => {
  const r = { ...base, status: "pending_approval", approval_id: "ap-9", cap_basis_fi: "CMR 23 art.: 8,33 SDR × 100 kg bruttopainoa" };
  expect(html(r)).toContain("CMR 23 art.: 8,33 SDR × 100 kg bruttopainoa");
  expect(html(r)).not.toContain("CMR art. 23: 8.33 SDR");
  expect(html(r, "en")).toContain("CMR art. 23: 8.33 SDR × 100 kg gross");
  expect(html(base)).toContain("CMR art. 23: 8.33 SDR × 100 kg gross");  // a record from before cap_basis_fi
});
