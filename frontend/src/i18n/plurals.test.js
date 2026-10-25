import fs from "fs";
import path from "path";
import dictionaries from "@/i18n";
import { translate } from "@/lib/i18n";
import plurals from "./plurals";

const t = (locale, key, vars) => translate(dictionaries, locale, key, vars);

// r3 N3: the AI runs view said "3 virheet"; Finnish counts take the partitive above one and the nominative at one.
test.each([
  ["ai.error_count", 3, "3 virhettä"], ["ai.error_count", 1, "1 virhe"],
  ["customs.items_n", 4, "4 nimikettä"], ["customs.items_n", 1, "1 nimike"],
  ["customers.shipments_n", 2, "2 lähetystä"], ["customers.shipments_n", 1, "1 lähetys"],
  ["customs.item.packages_count", 58, "58 kollia"], ["customs.item.packages_count", "1", "1 kolli"],
  ["oq.q.options_n", 1, "1 vaihtoehto"], ["quotes.opt.warnings", 1, "1 huomio"],
])("fi %s n=%s", (key, n, want) => {
  expect(t("fi", key, { n })).toBe(want);
});

test("English singulars", () => {
  expect(t("en", "ai.error_count", { n: 1 })).toBe("1 error");
  expect(t("en", "ai.error_count", { n: 2 })).toBe("2 errors");
  expect(t("en", "customers.shipments_n", { n: 1 })).toBe("1 shipment");
});

test("a grouped number is never taken for one", () => {
  expect(t("fi", "customs.item.packages_count", { n: "1 200" })).toBe("1 200 kollia");
});

test("every singular form belongs to an existing count text with the same placeholders", () => {
  for (const locale of ["en", "fi"]) {
    for (const [key, one] of Object.entries(plurals[locale])) {
      if (!key.endsWith("_one")) continue;
      const base = dictionaries[locale][key.slice(0, -4)];
      expect([key, typeof base]).toEqual([key, "string"]);
      const vars = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).filter((v) => v !== "n").sort();
      expect([key, vars(one)]).toEqual([key, vars(base)]);
    }
  }
});

test("no count is followed by a lower-cased noun label", () => {
  const offenders = [];
  (function walk(dir) {
    for (const f of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, f.name);
      if (f.isDirectory()) walk(p);
      else if (/\.jsx$/.test(f.name) && /\}\s\{t\("[^"]+"\)\.toLowerCase\(\)\}/.test(fs.readFileSync(p, "utf8"))) offenders.push(p);
    }
  })(path.resolve(__dirname, ".."));
  expect(offenders).toEqual([]);
});
