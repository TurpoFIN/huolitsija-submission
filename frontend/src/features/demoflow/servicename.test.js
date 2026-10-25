import { displayText } from "@/lib/localizedData";

test("the demo offer's service name reads in Finnish (driver.py service_name)", () => {
  expect(displayText("Huolitsija door-to-door road freight", "fi")).toBe("Huolitsijan ovelta ovelle -maantiekuljetus");
  expect(displayText("Huolitsija door-to-door sea freight", "fi")).toBe("Huolitsijan ovelta ovelle -merikuljetus");
  expect(displayText("Huolitsija door-to-door road freight", "en")).toBe("Huolitsija door-to-door road freight");
});

test("demo world texts shown in the UI read in Finnish", () => {
  expect(displayText("Booking confirmed · ref KP-2026-1104-002", "fi")).toBe("Varaus vahvistettu · viite KP-2026-1104-002");
  expect(displayText("Terminal slot delayed", "fi")).toBe("Terminaalin aikaikkuna viivästyi");
  expect(displayText("PEP stated in registry actor facts", "fi")).toBe("PEP-asema ilmoitettu rekisterin toimijatiedoissa");
});
