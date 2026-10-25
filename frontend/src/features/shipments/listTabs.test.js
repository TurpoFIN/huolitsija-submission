import { defaultTab } from "./listTabs";

test("needs attention first when anything needs it", () => {
  expect(defaultTab({ needs_action: 2, active: 5, all: 9 }, { ops: false, loaded: true })).toBe("needs_action");
  expect(defaultTab({ needs_action: 1 }, { ops: true, loaded: true })).toBe("needs_action");
});

test("a customer with nothing needing attention sees all shipments (s4 D9)", () => {
  expect(defaultTab({ needs_action: 0, active: 0, delivered: 1, all: 1 }, { ops: false, loaded: true })).toBe("all");
  expect(defaultTab({}, { ops: false, loaded: true })).toBe("all");
});

test("ops keep the active work view", () => {
  expect(defaultTab({ needs_action: 0, active: 3 }, { ops: true, loaded: true })).toBe("active");
});
