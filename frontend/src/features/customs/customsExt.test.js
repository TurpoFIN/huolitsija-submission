/**
 * @jest-environment node
 */
// Node environment: react-router v7 needs TextEncoder (jsdom in Jest 27 lacks it); markup is rendered with react-dom/server.
import fs from "fs";
import path from "path";
import dictionaries from "@/i18n";
import ext from "@/i18n/customsExt";
import { HUB_FLOWS, HUB_REFERENCE, bi, goodsCode, hubKeyFor } from "./extCommon";
import { GROUP_ORDER, bestPreference, dutyText, groupMeasures, measureGroup, typeCode, validateLookup } from "./taricModel";

const SRC = path.resolve(__dirname, "../..");
const appJs = fs.readFileSync(path.join(SRC, "App.js"), "utf8");

// Texts that are the same word in both languages (codes, acronyms, proper names).
const SAME_OK = new Set(["cx.na", "cx.hub.ens", "cx.duty.src.taric", "customs.duty_source.taric", "cx.msg.xml"]);

test("every s11 customs text exists in Finnish and English, and Finnish is not left in English", () => {
  const enKeys = Object.keys(ext.en).sort();
  const fiKeys = Object.keys(ext.fi).sort();
  expect(fiKeys).toEqual(enKeys);
  const untranslated = enKeys.filter((k) => !SAME_OK.has(k) && ext.fi[k] === ext.en[k]);
  expect(untranslated).toEqual([]);
  // registered in the merged dictionary
  expect(dictionaries.fi["cx.hub.import"]).toBe("Tuonti");
  expect(dictionaries.fi["cx.hub.transit"]).toBe("Passitus");
  expect(dictionaries.en["cx.hub.export"]).toBe("Export");
});

test("placeholders match between languages", () => {
  const vars = (s) => [...String(s).matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
  const bad = Object.keys(ext.en).filter((k) => JSON.stringify(vars(ext.en[k])) !== JSON.stringify(vars(ext.fi[k])));
  expect(bad).toEqual([]);
});

test("every hub tab has a route in App.js and a label in both languages", () => {
  for (const h of [...HUB_FLOWS, ...HUB_REFERENCE]) {
    const rel = h.to.replace(/^\/ops\//, "");
    expect(appJs.includes(`path="${rel}"`)).toBe(true);
    expect(dictionaries.fi[`cx.hub.${h.key}`]).toBeTruthy();
    expect(dictionaries.en[`cx.hub.${h.key}`]).toBeTruthy();
  }
});

test("hub key follows the path, import owns the declaration detail", () => {
  expect(hubKeyFor("/ops/customs")).toBe("import");
  expect(hubKeyFor("/ops/customs/5f0c2c1e-aaaa-bbbb-cccc-000000000000")).toBe("import");
  expect(hubKeyFor("/ops/customs/export")).toBe("export");
  expect(hubKeyFor("/ops/customs/export/abc")).toBe("export");
  expect(hubKeyFor("/ops/customs/transit/")).toBe("transit");
  expect(hubKeyFor("/ops/customs/ens/x")).toBe("ens");
  expect(hubKeyFor("/ops/customs/taric")).toBe("taric");
  expect(hubKeyFor("/ops/customs/exporter")).toBe("import");
  expect(hubKeyFor("/ops/shipments")).toBeNull();
});

test("bilingual helper picks the locale, then the other language", () => {
  expect(bi("fi", { fi: "Suomeksi", en: "In English" })).toBe("Suomeksi");
  expect(bi("en", { fi: "Suomeksi" })).toBe("Suomeksi");
  expect(bi("fi", { title_fi: "Otsikko", title_en: "Title" }, "title")).toBe("Otsikko");
  expect(bi("en", { title: { fi: "Otsikko", en: "Title" } }, "title")).toBe("Title");
  expect(bi("en", null)).toBe("");
});

test("TARIC lookup form accepts 6/8/10 digits and an ISO country", () => {
  expect(validateLookup({ code: "8712 00 30 00", country: "cn" })).toEqual({ ok: true, code: "8712003000", country: "CN", errors: {} });
  expect(validateLookup({ code: "871200", country: "" }).ok).toBe(true);
  expect(validateLookup({ code: "87120", country: "" }).errors.code).toBe("cx.taric.err.code");
  expect(validateLookup({ code: "8712", country: "" }).ok).toBe(true);
  expect(validateLookup({ code: "8712003000", country: "CHN" }).errors.country).toBe("cx.taric.err.country");
  expect(goodsCode("8712003000")).toBe("8712 00 30 00");
});

test("measure groups follow TARIC measure type codes unless the backend names the group", () => {
  expect(measureGroup({ type_code: "103" })).toBe("duty");
  expect(measureGroup({ type_code: "142" })).toBe("preferences");
  expect(measureGroup({ type_code: "552" })).toBe("trade_defence");
  expect(measureGroup({ type_code: "277" })).toBe("restrictions");
  expect(measureGroup({ type_code: "750" })).toBe("restrictions");
  expect(measureGroup({ type_code: "305" })).toBe("other");
  expect(measureGroup({ type_code: "103", kind: "restriction" })).toBe("restrictions");
  const g = groupMeasures([{ type_code: "552" }, { type_code: "103" }, { type_code: "142" }]).map((x) => x.key);
  expect(g).toEqual(["duty", "preferences", "trade_defence"]);
});

test("duty text is TARIC's own expression; the best preference is the lowest ad-valorem one", () => {
  expect(dutyText({ duty_expression: " 14.00 % " })).toBe("14.00 %");
  expect(dutyText({ duty: { raw: "Entry price system (TARIC condition V)", kind: "entry_price" } })).toBe("Entry price system (TARIC condition V)");
  expect(dutyText({})).toBe("");
  const prefs = [{ geo_code: "JP", duty: { kind: "ad_valorem", pct: 1.2 } }, { geo_code: "JP", duty: { kind: "ad_valorem", pct: 0 } },
    { geo_code: "JP", duty: { kind: "specific", amount: 3 } }];
  expect(bestPreference(prefs)).toBe(prefs[1]);
  expect(bestPreference([])).toBeNull();
  expect(typeCode({ type_code: null, type_code_candidates: ["142", "145"] })).toBe("142/145");
});

test("the backend's measure kind decides the group", () => {
  const kinds = { third_country: "duty", quota: "preferences", preference: "preferences", trade_defence: "trade_defence",
    prohibition: "restrictions", restriction: "restrictions", suspension: "duty", other: "other" };
  for (const [k, g] of Object.entries(kinds)) expect(measureGroup({ kind: k })).toBe(g);
  // every kind services/taric.py can return has a group
  const taric = path.resolve(SRC, "../../backend/services/taric.py");
  if (fs.existsSync(taric)) {
    const src = fs.readFileSync(taric, "utf8");
    const body = src.slice(src.indexOf("def _kind("), src.indexOf("def summarise("));
    const returned = [...body.matchAll(/return "([a-z_]+)"/g)].map((m) => m[1]);
    expect(returned.length).toBeGreaterThan(5);
    for (const k of returned) expect(GROUP_ORDER).toContain(measureGroup({ kind: k }));
    expect(returned.filter((k) => measureGroup({ kind: k }) === "other" && k !== "other" && k !== "supplementary_unit")).toEqual([]);
  }
});
