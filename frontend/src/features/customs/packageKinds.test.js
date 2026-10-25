import fs from "fs";
import path from "path";
import dictionaries from "@/i18n";
import { PACKAGE_KINDS, packageKind, packageOptions } from "./packageKinds";

test("stored package texts match a kind", () => {
  expect(["cartons", "CTNS", "Carton", "boxes", "pallets", "pcs", "pallet_eur", "barrels", ""].map(packageKind))
    .toEqual(["carton", "carton", "carton", "box", "pallet", "package", "pallet", null, null]);
});

test("the current value stays selectable and is not duplicated", () => {
  const opts = packageOptions("cartons");
  expect(opts.find((o) => o.kind === "carton").value).toBe("cartons");
  expect(opts.filter((o) => o.kind === "carton")).toHaveLength(1);
  expect(packageOptions("barrels").at(-1)).toEqual({ value: "barrels", kind: null });
  expect(packageOptions("").map((o) => o.value)).toEqual(PACKAGE_KINDS);
});

test("every kind has a label in both languages and a customs package code", () => {
  const tulli = fs.readFileSync(path.join(__dirname, "../../../../backend/services/tulli.py"), "utf8");
  const codes = Object.fromEntries([...tulli.matchAll(/"([a-z_]+)": "([A-Z]{2})"/g)].map((m) => [m[1], m[2]]));
  for (const k of PACKAGE_KINDS) {
    expect([k, typeof dictionaries.en[`customs.pkg.${k}`], typeof dictionaries.fi[`customs.pkg.${k}`]]).toEqual([k, "string", "string"]);
    expect([k, codes[k]]).toEqual([k, expect.stringMatching(/^[A-Z]{2}$/)]);
  }
});
