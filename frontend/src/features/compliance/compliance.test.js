// s12 compliance UI: nav entry, FI/EN parity of the catalogue, and error detail read from real FastAPI responses
// (fixtures captured from this repo's backend; see errors.fixture.json "source").
import dictionaries from "@/i18n";
import compliance from "@/i18n/compliance";
import { OPS_NAV } from "@/nav";
import { policyRows, rawDetail } from "./logic";
import fixture from "./errors.fixture.json";

test("ops nav has one compliance item, labelled in both languages", () => {
  const items = OPS_NAV.flatMap((g) => g.items).filter((i) => i.to === "/ops/compliance");
  expect(items).toHaveLength(1);
  expect([dictionaries.en[items[0].label], dictionaries.fi[items[0].label]]).toEqual(["Compliance", "Vaatimustenmukaisuus"]);
});

test("every compliance string exists in Finnish and English", () => {
  const en = Object.keys(compliance.en).sort();
  const fi = Object.keys(compliance.fi).sort();
  expect(fi).toEqual(en);
  Object.entries(compliance.fi).forEach(([k, v]) => expect([k, typeof v === "string" && v.length > 0]).toEqual([k, true]));
});

test("the server's own error detail is shown, from real FastAPI bodies", () => {
  expect(rawDetail({ data: fixture.route_missing.body })).toBe("Not Found");
  const v = rawDetail({ data: fixture.validation.body });
  expect(typeof v).toBe("string");
  expect(v).toContain("name");
  expect(rawDetail({ data: fixture.http_exception.body })).toBe(fixture.http_exception.body.detail);
  expect(rawDetail({ data: null })).toBeNull();
});

test("retention policy rows come from a list or a keyed map", () => {
  expect(policyRows({ items: [{ key: "invoices", years: 6 }] })).toEqual([{ key: "invoices", years: 6 }]);
  expect(policyRows({ policy: { invoices: { years: 6, basis: "KPL 2:10" } } })).toEqual([{ key: "invoices", years: 6, basis: "KPL 2:10" }]);
  expect(policyRows({})).toEqual([]);
});
