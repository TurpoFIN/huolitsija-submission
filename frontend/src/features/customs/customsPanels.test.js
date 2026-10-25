/** @jest-environment node */
import React from "react";
import fs from "fs";
import path from "path";
import { render, text } from "./testRender";
import { XsdBadge, DecisionCard, isMissingApi } from "./extCommon";
import { licenceSummary, LicenceRequirements, daysTo } from "./TaricLicences";
import TaricDutyBasis, { dutySource } from "./TaricDutyBasis";
import OriginPreference from "./OriginPreference";
import { ControlsKpis } from "./ControlsSettings";
import { BtiRulings } from "./TaricBti";
import { filingMessages } from "./ExportFlow";
import { groupMeasures } from "./taricModel";
import ext from "@/i18n/customsExt";
import Totals from "./Totals";

beforeAll(() => { global.localStorage = { getItem: () => null }; });
afterAll(() => { delete global.localStorage; });

test("missing duty cannot display a partial numeric duty or VAT total", () => {
  const html = text(render(<Totals decl={{ items: [{ duty_cents: null, vat_cents: 2550 }],
    totals: { duty_cents: 81557, vat_base_cents: 2674571, vat_cents: 682016, duty_complete: false,
      payable_to_customs_cents: null } }} />));
  expect(html.match(/Ei laskettu/g)).toHaveLength(4);
  expect(html).not.toContain("815,57");
  expect(html).not.toContain("6820,16");
});

test("a complete zero duty remains numeric", () => {
  const html = text(render(<Totals decl={{ items: [{ duty_cents: 0, vat_cents: 2550 }],
    totals: { duty_cents: 0, vat_base_cents: 10000, vat_cents: 2550, duty_complete: true,
      payable_to_customs_cents: 0 } }} />));
  expect(html).not.toContain("Ei laskettu");
  expect(html).toContain("0,00");
});

test("XSD unknown and invalid never render success", () => {
  expect(text(render(<XsdBadge />))).toBe("Ei validoitu");
  expect(text(render(<XsdBadge valid={false} errors={["invalid"]} />))).toContain("1 virhe");
  expect(text(render(<XsdBadge valid />))).toBe("XSD kunnossa");
});
test("both actual XML storage contracts preserve validation and message type", () => {
  const [exp, transit, absent] = filingMessages([
    { id: "e", type: "FI515A", direction: "out", validation: { valid: false, errors: ["xsd error"], schema_version: "1.0" } },
    { id: "t", type: "CC015C", direction: "out", xsd_valid: true },
    { id: "u", type: "CC028C", direction: "in" },
  ], "/customs-export", "d");
  expect(exp.valid).toBe(false); expect(exp.errors).toEqual(["xsd error"]); expect(exp.type).toBe("FI515A");
  expect(transit.valid).toBe(true); expect(absent.valid).toBeUndefined();
  expect(exp.xmlHref).toBe("/api/customs-export/d/messages/e/xml");
});
test("Autopilot permission is not presented as a submitted filing", () => {
  const html = render(<DecisionCard decision={{ outcome: "auto" }} />);
  expect(text(html)).toContain("Autopilotti sallittu"); expect(text(html)).not.toContain("ilmoitti");
  const owner = render(<DecisionCard decision={{ outcome: "owner_queue", approval_id: "abc" }} />);
  expect(owner).toContain('/ops/approvals?focus=abc');
});
test("missing duty and broker overrides cannot be counted as TARIC confirmed", () => {
  expect(dutySource({ duty_rate_pct: null, taric: { status: "needs_code" } })).toBe("taric_open");
  expect(dutySource({ duty_rate_pct: 0, duty_source: "taric" })).toBe("taric");
  const html = text(render(<TaricDutyBasis decl={{ items: [{ seq: 1, duty_source: "broker", duty_rate_pct: 0, duty_cents: null }] }} />));
  expect(html).toContain("puuttuu vahvistettu TARIC-tulli"); expect(html).toContain("Tieto puuttuu");
  expect(html).not.toContain("Kaikkien rivien tulli on TARICista");
});
test("an unchecked or incomplete licence check is not clear", () => {
  expect(text(render(<LicenceRequirements decl={{ items: [{}] }} />))).toContain("ei ole tarkistettu");
  const decl = { items: [{ seq: 1, licence_check: { status: "needs_code" } }] };
  expect(licenceSummary(decl).needsCode).toBe(1);
  expect(text(render(<LicenceRequirements decl={decl} />))).toContain("vaatii tavaranimikkeen");
  expect(text(render(<LicenceRequirements decl={decl} />))).not.toContain("ei ole lupavaatimuksia");
});
test("missing API and missing record remain distinct", () => {
  expect(isMissingApi({ status: 404, data: { detail: "Not Found" } })).toBe(true);
  expect(isMissingApi({ status: 404, data: { detail: "Declaration not found" } })).toBe(false);
  expect(isMissingApi({ status: 503 })).toBe(false);
});
test("every static customs extension label resolves in both languages", () => {
  const files = fs.readdirSync(__dirname).filter(f => f.endsWith(".jsx"));
  for (const file of files) {
    const src = fs.readFileSync(path.join(__dirname, file), "utf8");
    for (const [, key] of src.matchAll(/\bt\("(cx\.[^"]+)"/g)) {
      expect(ext.fi[key]).toBeTruthy(); expect(ext.en[key]).toBeTruthy();
    }
  }
});
const fixtures = path.join(__dirname, "__fixtures__");
for (const name of ["taric_bicycles_cn", "taric_footwear_vn", "taric_steel_cn", "taric_computers_ru_export"]) {
  test(`live HTTP capture ${name} maps every official measure once`, () => {
    const d = JSON.parse(fs.readFileSync(path.join(fixtures, `${name}.json`), "utf8"));
    expect(d.source_url).toMatch(/^https:\/\/(ec\.europa\.eu|webgate\.ec\.europa\.eu)/);
    expect(d.measures.length).toBeGreaterThan(0);
    expect(groupMeasures(d.measures).flatMap(g => g.measures || g.items).length).toBe(d.measures.length);
  });
}
// Captured records, created through the actual HTTP API by scripts/s11_ui_seed.py.
for (const [name, Component, props] of [
  ["import_declaration", OriginPreference, d => ({ decl: d })],
  ["controls_kpis", ControlsKpis, d => ({ data: d })],
  ["bti", BtiRulings, d => ({ data: d })],
]) {
  if (fs.existsSync(path.join(fixtures, `${name}.json`))) test(`render actual ${name} response in Finnish`, () => {
    const d = JSON.parse(fs.readFileSync(path.join(fixtures, `${name}.json`), "utf8"));
    const output = text(render(<Component {...props(d)} />));
    expect(output).not.toMatch(/cx\.[a-z]/); expect(output.length).toBeGreaterThan(10);
  });
}

test("licence expiry is negative immediately after the last valid day", () => {
  const now = jest.spyOn(Date, "now").mockReturnValue(new Date("2026-10-25T00:00:00").getTime());
  expect(daysTo("2026-10-24")).toBe(-1);
  expect(daysTo("2026-10-25")).toBe(1);
  now.mockRestore();
});

test("actual origin proof reference renders without granting unverified preference", () => {
  const d = JSON.parse(fs.readFileSync(path.join(fixtures, "origin_declaration.json"), "utf8"));
  const a = d.items[0].origin_assessment;
  expect(a.preference_code).toBe("100"); expect(a.available_preference_code).toBe("300");
  expect(a.proof.valid).toBe(false);
  const output = text(render(<OriginPreference decl={d} />));
  expect(output).toContain("UI-TEST-CERTIFICATE-NOT-VALID");
  expect(output).toContain("N954"); expect(output).toContain("Todistetta ei vahvistettu");
});
