import fs from "fs";
import path from "path";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import dictionaries from "@/i18n";
import commercial from "@/i18n/commercial";
import { I18nProvider } from "@/lib/i18n";
import { OPS_NAV } from "@/nav";
import { COMMERCIAL_TABS, COVERS, ComError, LeadState, ProviderMark } from "./common";

// Jest 27 (CRA 5) cannot resolve react-router v7's package exports; the router is not under test here.
jest.mock("react-router-dom", () => ({ Link: ({ to, ...p }) => <a href={to} {...p} />, useLocation: () => ({ pathname: "/ops" }) }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const SRC = path.resolve(__dirname, "..", "..");
const files = [
  ...fs.readdirSync(__dirname).filter((f) => f.endsWith(".jsx")).map((f) => path.join(__dirname, f)),
  path.join(SRC, "pages", "ops", "Commercial.jsx"),
];
const staticKeys = [...new Set(files.flatMap((f) => [...fs.readFileSync(f, "utf8").matchAll(/\bt\("(com\.[A-Za-z0-9_.]+)"/g)].map((m) => m[1])))];
const dynamicKeys = [
  ...COMMERCIAL_TABS.map((k) => `com.tab.${k}`),
  ...["activate", "expire"].flatMap((v) => [`com.tariff.${v}_q`, `com.tariff.${v}_done`]),
  ...COVERS.map((c) => `com.ins.cover_hint.${c}`),
];

test("every commercial text the UI asks for exists in Finnish and English", () => {
  expect(staticKeys.length).toBeGreaterThan(150);
  const missing = [...staticKeys, ...dynamicKeys].flatMap((k) => ["fi", "en"].filter((l) => dictionaries[l][k] === undefined).map((l) => `${l}:${k}`));
  expect(missing).toEqual([]);
});

test("the Finnish and English commercial catalogues have the same keys", () => {
  const fi = Object.keys(commercial.fi).sort();
  const en = Object.keys(commercial.en).sort();
  expect(fi.filter((k) => !commercial.en[k])).toEqual([]);
  expect(en.filter((k) => !commercial.fi[k])).toEqual([]);
});

test("Myynti sits right after Quotes in the ops nav, labelled in both languages", () => {
  const items = OPS_NAV.flatMap((g) => g.items);
  const i = items.findIndex((x) => x.to === "/ops/commercial");
  expect(items[i - 1].to).toBe("/ops/quotes");
  expect([dictionaries.fi[items[i].label], dictionaries.en[items[i].label]]).toEqual(["Myynti", "Commercial"]);
});

function render(el) {
  const div = document.createElement("div");
  document.body.appendChild(div);
  const root = createRoot(div);
  act(() => root.render(<I18nProvider>{el}</I18nProvider>));
  return { div, unmount: () => act(() => root.unmount()) };
}

test("an insurer without its contract is shown as waiting for it, an unknown status never as live", () => {
  localStorage.setItem("huo.locale", "fi");
  const { div, unmount } = render(<ProviderMark status="live_ready" name="Redkik" />);
  expect(div.textContent).toContain("Valmis, odottaa vakuutussopimusta");
  unmount();
  const r2 = render(<ProviderMark status={null} name="x" />);
  expect(r2.div.textContent).toContain("Ei määritetty");
  r2.unmount();
  const r3 = render(<ProviderMark status="live" name="Redkik" />);
  expect(r3.div.textContent).toContain("Tuotanto");
  r3.unmount();
});

test("the providers shape reads the API's state (live / live_ready), never inventing a stub", () => {
  const { providers } = require("./shapes");
  const rows = providers({ active: "redkik", providers: [{ provider: "redkik", label: "Redkik", state: "live_ready", configured: false,
    blocked_on_fi: "Vakuutussopimus Redkikin kanssa", active: true }] });
  expect(rows[0].status).toBe("live_ready");
  expect(rows[0].active).toBe(true);
  expect(providers({ providers: [{ provider: "x", configured: false }] })[0].status).toBe("not_configured");
});

test("a missing backend endpoint and a missing permission are said plainly", () => {
  localStorage.setItem("huo.locale", "fi");
  const a = render(<ComError error={{ status: 404 }} />);
  expect(a.div.textContent).toContain("ei ole vielä käytössä");
  a.unmount();
  const b = render(<ComError error={{ status: 403 }} perm="customers.write" />);
  expect(b.div.textContent).toContain("customers.write");
  b.unmount();
});

test("every opportunity stage of services/crm.py STAGES has a Finnish and an English label", () => {
  const { STAGE_ORDER } = require("./shapes");
  for (const st of STAGE_ORDER) {
    expect(dictionaries.fi[`com.enum.stage.${st}`]).toBeTruthy();
    expect(dictionaries.en[`com.enum.stage.${st}`]).toBeTruthy();
    expect(dictionaries.fi[`com.enum.stage.${st}`]).not.toEqual(dictionaries.en[`com.enum.stage.${st}`]);
  }
});

test("a lead without an opportunity shows its own status, never a dash; with one it shows the stage", () => {
  const { LEAD_STATUSES, lead: normLead } = require("./shapes");
  for (const st of LEAD_STATUSES) {
    expect(dictionaries.fi[`com.enum.lead.${st}`]).toBeTruthy();
    expect(dictionaries.en[`com.enum.lead.${st}`]).toBeTruthy();
  }
  // Shape of a PRH-discovered prospect as GET /crm/leads returns it (production lead L-C2AHQS: status scored, no opportunity).
  const found = normLead({ id: "l1", ref: "L-C2AHQS", name: "Hovinikkarit Oy", status: "scored", score: 61, opportunity_id: null });
  const a = render(<LeadState lead={found} />);
  expect(a.div.textContent).toBe("Pisteytetty");
  a.unmount();
  const opp = normLead({ id: "l2", name: "Pohjolan Kaluste Oy", status: "converted", opportunity: { stage: "qualified" } });
  const b = render(<LeadState lead={opp} />);
  expect(b.div.textContent).toBe("Kvalifioitu");
  b.unmount();
  expect(dictionaries.fi["com.pipe.open"]).toBe("avointa myyntimahdollisuutta");
});

test("live tariff and tender shapes: every unit cell has its unit, the tender shows its source file", () => {
  const { tariff, tender } = require("./shapes");
  // GET /api/tariffs/{id} lane on the live app (CT-2026-000001): flat lane with basis, no volume_unit
  const lane = { id: "f6b0b810", origin: { country: "FI", locode: "FIHEL", name: "Vantaa" }, destination: { country: "SE", locode: "SESTO", name: "Stockholm" },
    mode: "road", service: "ROAD_LTL", basis: "PER_KG", price_cents: 16, surcharges_included: true };
  const bases = ["PER_KG", "PER_CONTAINER", "PER_SHIPMENT", "PER_LDM", "PER_PALLET", "PER_WM"];
  const tf = tariff({ id: "t1", number: "CT-2026-000001", lanes: bases.map((b, i) => ({ ...lane, id: `l${i}`, basis: b })) });
  expect(tf.lanes.map((l) => l.price_unit)).toEqual(["kg", "container", "shipment", "ldm", "pallet", "cbm"]);
  for (const u of tf.lanes.map((l) => l.price_unit)) expect(dictionaries.fi[`com.enum.unit.${u}`]).toBeTruthy();
  expect(tf.lanes[0].price_per_unit_cents).toBe(16);
  // GET /api/tenders/{id} on the live app: the uploaded workbook is original_filename
  expect(tender({ id: "d6f3", number: "T-2026-000001", original_filename: "s7_tender_fi_kuljetukset_2027.xlsx", lanes: [] }).file_name)
    .toBe("s7_tender_fi_kuljetukset_2027.xlsx");
  expect(tender({ id: "x", lanes: [] }).file_name).toBeNull();
});

test("a stored quote line keeps the backend's amount and cost until a person edits it", () => {
  const { lineCost, lineAmount, optionTotals, cleanLine } = require("@/features/quotes/ops/quoteMath");
  // Live Q-2026-000075 contract line: 1 480 kg, cost 290.08 EUR -> unit cost rounds to 20 c (1 480 * 20 c = 296.00)
  const contract = { code: "CONTRACT_FREIGHT", qty: 1480, unit_price_cents: 16, amount_cents: 23680, unit_cost_cents: 20, cost_cents: 29008, is_pass_through: false };
  const fee = { code: "DOC_FEE", qty: 1, unit_price_cents: 3500, amount_cents: 3500, unit_cost_cents: 0, cost_cents: 0, is_pass_through: false };
  expect(lineCost(contract)).toBe(29008);
  const tot = optionTotals([contract, fee]);
  expect(tot.total).toBe(27180);
  expect(tot.cost).toBe(29008);
  expect(tot.margin).toBe(-1828); // the API's margin_cents, not 271.80 - 296.00
  const edited = { ...contract, unit_cost_cents: 21, _edited: true };
  expect(lineCost(edited)).toBe(31080);
  expect(lineAmount({ qty: 2, unit_price_cents: 150 })).toBe(300); // a new line has no stored totals
  const clean = cleanLine({ ...edited, _k: "k1" });
  expect(clean._edited).toBeUndefined();
  expect(clean.cost_cents).toBe(31080);
});

test("contract warnings and the contract tag read in Finnish (Q-2026-000075 showed English and a raw key)", () => {
  const { warningText } = require("@/features/quotes/quoteFormat");
  const t = (k) => dictionaries.fi[k] ?? k;
  const below = { code: "contract.below_floor", level: "warning", params: { margin_cents: 680, floor_cents: 2500 },
    message: "The contract price is below the margin floor for this shipment (margin 6.80 EUR, floor 25.00 EUR): it is not recommended and Autopilot does not send it.",
    message_fi: "Sopimushinta alittaa tämän lähetyksen katelattian (kate 6,80 EUR, lattia 25,00 EUR): sitä ei suositella eikä Autopilot lähetä sitä." };
  expect(warningText(t, below, "fi")).toBe(below.message_fi);
  expect(warningText((k) => dictionaries.en[k] ?? k, below, "en")).toBe(below.message);
  expect(dictionaries.fi["oq.tag.contract"]).toBe("Sopimushinta");
  expect(dictionaries.en["oq.tag.contract"]).toBe("Contract price");
});

test("every quote source the backend sets has an FI and EN label (Q-2026-000113 showed oq.q.src.email)", () => {
  const dictionaries = require("@/i18n").default;
  ["portal", "public", "ai", "ops", "email", "api", "tender"].forEach((s) => {
    expect(dictionaries.fi[`oq.q.src.${s}`]).toBeTruthy();
    expect(dictionaries.en[`oq.q.src.${s}`]).toBeTruthy();
  });
  expect(dictionaries.fi["oq.q.src.email"]).toBe("Sähköposti");
});
