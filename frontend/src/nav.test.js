import dictionaries from "@/i18n";
import { PARTNER_NAV, settingsPath } from "@/nav";

test("every area's user menu leads to its own settings page", () => {
  expect(["internal", "partner", "customer", undefined].map(settingsPath))
    .toEqual(["/ops/settings", "/partner/settings", "/portal/settings", "/portal/settings"]);
});

test("partners have a settings link in the nav, labelled in both languages", () => {
  const item = PARTNER_NAV.flatMap((g) => g.items).find((i) => i.to === "/partner/settings");
  expect(item).toBeTruthy();
  expect([dictionaries.en[item.label], dictionaries.fi[item.label]]).toEqual(["Settings", "Asetukset"]);
});

test("ops nav has the Sourcing Autopilot page next to the rate table, in both languages", () => {
  const { OPS_NAV } = require("@/nav");
  const items = OPS_NAV.flatMap((g) => g.items);
  const i = items.findIndex((x) => x.to === "/ops/sourcing");
  expect(i).toBeGreaterThan(-1);
  expect(items[i - 1].to).toBe("/ops/rates");
  expect([dictionaries.en[items[i].label], dictionaries.fi[items[i].label]]).toEqual(["Sourcing", "Hankinta"]);
});

test("ops nav has the e-mail inbox right after Messages, in both languages", () => {
  const { OPS_NAV } = require("@/nav");
  const items = OPS_NAV.flatMap((g) => g.items);
  const i = items.findIndex((x) => x.to === "/ops/email");
  expect(i).toBeGreaterThan(-1);
  expect(items[i - 1].to).toBe("/ops/messages");
  expect([dictionaries.en[items[i].label], dictionaries.fi[items[i].label]]).toEqual(["E-mail", "Sähköpostit"]);
});
