import fs from "fs";
import path from "path";
import dictionaries from "@/i18n";
import { OVERRIDE_MIN_NOTE, fourEyesState, overrideReady } from "./fourEyes";

const admin = fs.readFileSync(path.resolve(__dirname, "../../../backend/routes/admin.py"), "utf8");

test("every four-eyes code the decide endpoint returns maps to a state", () => {
  const codes = [...admin.matchAll(/_four_eyes_refusal\(\d+, "([a-z_]+)"/g)].map((m) => m[1]);
  expect(codes.sort()).toEqual(["four_eyes_override_note", "four_eyes_required", "four_eyes_single_user"]);
  expect(fourEyesState({ data: { code: "four_eyes_required" } })).toBe("colleague");
  expect(fourEyesState({ data: { code: "four_eyes_single_user" } })).toBe("single_user");
  expect(fourEyesState({ data: { code: "four_eyes_override_note" } })).toBe("single_user");
  expect(fourEyesState({ status: 409, data: { detail: "Already approved" } })).toBeNull();
  expect(fourEyesState(undefined)).toBeNull();
});

test("the override reason length matches the backend minimum", () => {
  expect(Number(/FOUR_EYES_MIN_NOTE = (\d+)/.exec(admin)[1])).toBe(OVERRIDE_MIN_NOTE);
  expect(overrideReady("  short  ")).toBe(false);
  expect(overrideReady("Only ops user today")).toBe(true);
});

test("four-eyes texts exist in both languages", () => {
  for (const k of ["title", "colleague", "single_user", "override", "audited", "reason_needed"]) {
    const key = `approvals.four_eyes.${k}`;
    expect(dictionaries.en[key]).toBeTruthy();
    expect(dictionaries.fi[key]).toBeTruthy();
    expect(dictionaries.fi[key]).not.toBe(dictionaries.en[key]);
  }
});
