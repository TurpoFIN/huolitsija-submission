import fs from "fs";
import path from "path";
import dictionaries from "@/i18n";
import cases from "./eori.cases.json";
import { eoriErrorParty, eoriShapeOk } from "./eori";

// The same cases run against the backend (backend/tests/test_core_eori_shape.py), so the form and the API agree.
test.each(cases)("eoriShapeOk(%p) is %p", (value, ok) => {
  expect(eoriShapeOk(value)).toBe(ok);
});

test("eoriErrorParty reads the booking API's message for every party it checks", () => {
  const src = fs.readFileSync(path.resolve(__dirname, "../../../backend/routes/quotes.py"), "utf8");
  const template = /raise HTTPException\(400, f"(The \{label\}'s EORI number looks wrong[^"]*)"\)/.exec(src)[1];
  const labels = [...src.matchAll(/_check_party\(body\.(\w+), "([^"]+)"/g)].map((m) => [m[1], m[2]]);
  expect(labels.map(([k]) => k)).toEqual(["shipper", "consignee", "notify"]);
  for (const [key, label] of labels) expect(eoriErrorParty(template.replace("{label}", label))).toBe(key);
  expect(eoriErrorParty("Give the shipper's name.")).toBeNull();
  expect(eoriErrorParty(undefined)).toBeNull();
});

test("EORI messages exist in both languages", () => {
  for (const k of ["pq.b.err.eori", "pq.b.err.eori_party"]) {
    expect(dictionaries.en[k]).toBeTruthy();
    expect(dictionaries.fi[k]).toBeTruthy();
    expect(dictionaries.fi[k]).not.toBe(dictionaries.en[k]);
  }
});
