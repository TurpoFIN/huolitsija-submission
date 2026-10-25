import { titleText } from "./titles";

const ev = { seq: 2, kind: "step", node: "requester", state: "done", title: "Quote request typed", data: { facts: {} } };

test("a stored English title stays English on the EN map", () => {
  const t = (k) => k;
  expect(titleText(ev, t, "en")).toBe("Quote request typed");
});
