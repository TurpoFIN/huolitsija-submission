// Fixture: lane 1's recorded hands-off run (shared/event-logs/run-d8a7b3f0…), the real demo_run_events of one quote phase.
import fs from "fs";
import path from "path";
import { RUN_STATUS_BADGE, W, agreedChip, applyEvent, basisChip, buildModel, docsSub, edgePath, emptyModel, intakeSub, layout, nodeDetail, nodeSub, partnerName, simChip, skeleton, summary } from "./model";

const events = fs.readFileSync(path.join(__dirname, "__fixtures__/quote-run.events.jsonl"), "utf8").trim().split("\n").map((l) => JSON.parse(l));

test("the recorded quote run ends with every quote node done and the offer out", () => {
  const m = buildModel(events);
  expect(m.lastSeq).toBe(events.length);
  ["requester", "intake", "research", "outreach", "replies", "pricing", "offer"].forEach((id) => expect(m.nodes[id].state).toBe("done"));
  expect(m.partners).toHaveLength(6);
  expect(m.partners.map((id) => partnerName(m.nodes[id]))).toContain("Kuljetusliike V. Mönkkönen Oy");
  expect(m.phase).toBe("offer_sent");
  expect(summary(m)).toMatchObject({ partners: 6, rfqs: 6, rates: 6, offer: "EUR 795.00" });
});

test("a partner with only the RFQ out shows waiting (awaiting reply)", () => {
  const upTo = events.filter((e) => e.seq <= 36);
  const m = buildModel(upTo);
  expect(m.partners.every((id) => m.nodes[id].state === "waiting")).toBe(true);
  expect(m.nodes.replies.state).toBe("waiting");
});

test("replaying an event twice changes nothing; out-of-order history still folds by seq", () => {
  const m1 = buildModel(events);
  expect(applyEvent(m1, events[10])).toBe(m1);
  const m2 = buildModel([...events].reverse());
  expect(m2.nodes.offer.state).toBe("done");
  expect(Object.keys(m2.edges).sort()).toEqual(Object.keys(m1.edges).sort());
});

test("edges carry direction of the latest exchange and count follow-ups", () => {
  const m = buildModel(events);
  const p = "p:2c52b1b6-8584-48d2-b369-7099f9bd0457";
  const k = ["replies", p].sort().join("|");
  expect(m.edges[k].count).toBeGreaterThanOrEqual(6);
  expect(m.edges[k].lastFrom).toBe(p);
  expect(Object.keys(m.edges[k].dirs).sort()).toEqual([`${p}>replies`, `replies>${p}`].sort());
});

test("layout places every node inside the canvas and skeleton edges have paths", () => {
  const m = buildModel(events);
  const lay = layout(m);
  Object.values(lay.pos).forEach((b) => {
    expect(b.x - b.w / 2).toBeGreaterThanOrEqual(0);
    expect(b.x + b.w / 2).toBeLessThanOrEqual(W);
  });
  // no two nodes overlap
  const boxes = Object.entries(lay.pos);
  boxes.forEach(([a, p], i) => boxes.slice(i + 1).forEach(([b, q]) => {
    const apart = Math.abs(p.x - q.x) >= (p.w + q.w) / 2 || Math.abs(p.y - q.y) >= (p.h + q.h) / 2;
    if (!apart) throw new Error(`${a} overlaps ${b}`);
  }));
  skeleton(m, lay).forEach(([a, b]) => expect(edgePath(lay.pos[a], lay.pos[b])).toMatch(/^M [\d.-]+ [\d.-]+ [CL]/));
  // world actors and lifecycle stages are laid out before any lifecycle event arrives; the shipment row reads left to right
  expect(lay.pos.booking && lay.pos.payment && lay.pos["w:customer"]).toBeTruthy();
  expect(lay.pos.booking.x).toBeLessThan(lay.pos.payment.x);
  // the hand-off edge goes down into the gutter between the bands, not across the quote row
  expect(edgePath(lay.pos.offer, lay.pos.booking)).toMatch(/L [\d.]+ 501 Q/);
});

test("lifecycle and world events (lane 2 schema) add legs, actors and incoming edges", () => {
  let m = buildModel(events);
  const base = { run_id: "r", at: "2026-10-25T08:00:00Z", sim_at: "2026-11-03T08:00:00Z", data: {}, simulated: true };
  m = applyEvent(m, { ...base, seq: 900, kind: "world", node: "booking", state: "active", edge: { from: "w:customer", to: "booking" }, title: "BOOK 1" });
  m = applyEvent(m, { ...base, seq: 901, kind: "world", node: "leg:2", state: "active", edge: { from: "w:carrier", to: "leg:2" }, title: "Departed reported",
    data: { facts: { mode: "sea", name: "Example Line" } } });
  m = applyEvent(m, { ...base, seq: 902, kind: "world", node: "customs", state: "waiting", edge: { from: "w:customs", to: "customs" }, title: "Documents requested" });
  expect(m.phase).toBe("lifecycle");
  expect(m.legs).toEqual(["leg:2"]);
  expect(m.nodes["leg:2"].meta.facts.mode).toBe("sea");
  expect(m.nodes["w:carrier"].meta.name).toBe("Example Line");
  expect(m.actors).toEqual(expect.arrayContaining(["w:customer", "w:customs"]));
  expect(m.edges[["booking", "w:customer"].sort().join("|")].world).toBe(true);
  const lay = layout(m);
  expect(lay.pos["w:customs"].y).toBeGreaterThan(lay.pos.customs.y);
  const det = nodeDetail(m, "w:customs");
  expect(det.events.map((e) => e.seq)).toContain(902);
  m = applyEvent(m, { ...base, seq: 903, kind: "phase", node: "payment", state: "done", edge: null, title: "Payment received" });
  expect(m.phase).toBe("done");
});

test("node detail gathers emails (simulated flag kept in panel data), tools, ranking and documents", () => {
  const m = buildModel(events);
  const replies = nodeDetail(m, "replies");
  expect(replies.extra.ranking[0].partner).toBe("Kuljetusliike V. Mönkkönen Oy");
  const p = nodeDetail(m, "p:a2b2ecf5-af1f-4141-bad4-4e5ebbe125b5");
  expect(p.emails.some((e) => e.direction === "in" && e.simulated)).toBe(true);
  expect(p.emails.some((e) => e.direction === "out" && !e.simulated)).toBe(true);
  expect(p.tools.length).toBeGreaterThan(0);
  expect(nodeDetail(m, "offer").documents[0].file_id).toBe("6addaa68648468a153545899");
  expect(emptyModel().events).toEqual([]);
});

test("a run-level stop goes to the header state, never onto the intake node", () => {
  const before = buildModel(events);
  const last = events[events.length - 1];
  const stop = { ...last, id: "stop", seq: last.seq + 1, kind: "phase", node: "intake", state: "done", edge: null, title: "Run stopped by ops", data: {}, sim_at: "2026-11-16T08:32:11Z" };
  const m = applyEvent(before, stop);
  expect(m.nodes.intake).toBe(before.nodes.intake);
  expect(m.ended).toEqual(expect.objectContaining({ state: "done", sim_at: "2026-11-16T08:32:11Z" }));
  expect(nodeDetail(m, "intake").events.map((e) => e.seq)).not.toContain(stop.seq);
  expect(before.ended).toBeNull();
});

test("a waiting state remembers the kind of event that set it", () => {
  const base = { run_id: "r", at: "2026-11-10T10:00:00Z", t: 1, sim_at: "2026-11-10T10:00:00Z", edge: null, data: {}, simulated: false };
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "step", node: "exceptions", state: "waiting", title: "Claim awaiting settlement review",
    data: { facts: { claim: "CL-1", approval_id: "a1" } } });
  expect(m.nodes.exceptions.stateKind).toBe("step");
  expect(nodeSub("exceptions", m.nodes.exceptions)).toBe("Claim awaiting settlement review");
  m = applyEvent(m, { ...base, seq: 2, kind: "email_out", node: "docs", state: "waiting", title: "Re: CMR · sent" });
  expect(m.nodes.docs.stateKind).toBe("email_out");
});

test("a lifecycle stop note (data.stopped_note) is run-level too", () => {
  const before = buildModel(events);
  const last = events[events.length - 1];
  const note = { ...last, id: "note", seq: last.seq + 1, kind: "tool", node: "intake", state: null, edge: null,
    title: "Lifecycle stopped at carrier booking", data: { stopped_note: "lifecycle stopped at carrier booking" } };
  const m = applyEvent(before, note);
  expect(m.nodes.intake).toBe(before.nodes.intake);
  expect(m.ended).toEqual(expect.objectContaining({ state: "done", title: "Lifecycle stopped at carrier booking" }));
});

test("the payment phase keeps its end reason (claim with a person for review)", () => {
  const base = { run_id: "r", at: "2026-11-20T10:00:00Z", t: 1, sim_at: "2026-11-20T10:00:00Z", edge: null, simulated: false };
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "step", node: "exceptions", state: "waiting", title: "Claim awaiting settlement review", data: {} });
  m = applyEvent(m, { ...base, seq: 2, kind: "phase", node: "payment", state: "done", title: "Delivered and paid · damage claim with a person for review",
    data: { end_reason: "claim_review_handoff" } });
  expect(m.phase).toBe("done");
  expect(m.endReason).toBe("claim_review_handoff");
  expect(m.nodes.exceptions.state).toBe("waiting");
});

test("rates in counts priced replies only, not declines", () => {
  const base = { run_id: "r", at: "2026-10-25T10:00:00Z", t: 1, sim_at: "2026-10-25T10:00:00Z", edge: null, simulated: true };
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "email_in", node: "p:a", state: "done", title: "Rate received: EUR 870.00 · 3 days",
    data: { facts: { "all-in": "EUR 870.00", transit: "3 days" } } });
  m = applyEvent(m, { ...base, seq: 2, kind: "email_in", node: "p:b", state: "done", title: "Declined", data: {} });
  expect(summary(m).rates).toBe(1);
});

test("intake reads 'details asked' once clarification is done, 'missing' before", () => {
  const base = { run_id: "r", at: "2026-10-25T10:00:00Z", t: 1, sim_at: "2026-10-25T10:00:00Z", edge: null, data: {}, simulated: false };
  const missing = { facts: { missing: "cargo.packages, weight_kg, volume_m3, ready_date, incoterm, pickup_address, delivery_address, dangerous_goods" } };
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "step", node: "intake", state: "done", title: "Request understood · 8 details missing", data: missing });
  expect(intakeSub(m)).toEqual({ key: "demoflow.sub.intakeMissing", n: 8 });
  m = applyEvent(m, { ...base, seq: 2, kind: "step", node: "clarify", state: "done", title: "Details complete" });
  expect(intakeSub(m)).toEqual({ key: "demoflow.sub.intakeAsked", n: 8 });
  const one = applyEvent(emptyModel(), { ...base, seq: 1, kind: "step", node: "intake", state: "done", title: "Request understood · 1 detail missing",
    data: { title_key: "intake.understood", title_vars: { n: 1 } } });
  expect(intakeSub(one).key).toBe("demoflow.sub.intakeMissingOne");
  const none = applyEvent(emptyModel(), { ...base, seq: 1, kind: "step", node: "intake", state: "done", title: "Request understood", data: { facts: { missing: "" } } });
  expect(intakeSub(none).key).toBe("demoflow.sub.intakeUnderstood");
});

test("the priced offer reads from its facts, formatted by the caller", () => {
  const base = { run_id: "r", at: "2026-10-25T10:00:00Z", t: 1, sim_at: "2026-10-25T10:00:00Z", edge: null, simulated: false };
  const m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "step", node: "pricing", state: "done", title: "EUR 931.80 · 3 days",
    data: { facts: { sell: "EUR 931.80", transit: "3 days" } } });
  expect(nodeSub("pricing", m.nodes.pricing, (n) => `${n} päivää`, (v) => `<${v}>`)).toBe("<EUR 931.80> · 3 päivää");
});

test("the simulated chip follows the reply's source, then the event flag", () => {
  const base = { run_id: "r", at: "2026-10-25T10:00:00Z", t: 1, sim_at: "2026-10-25T10:00:00Z", edge: null };
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "email_in", node: "p:a", state: "done", title: "Rate received", simulated: true, data: {} });
  m = applyEvent(m, { ...base, seq: 2, kind: "email_in", node: "p:b", state: "done", title: "Rate received", simulated: false, data: { reply: { source: "real" } } });
  m = applyEvent(m, { ...base, seq: 3, kind: "email_in", node: "p:c", state: "done", title: "Declined", simulated: false, data: { reply: { source: "world" } } });
  expect(simChip("p:a", m.nodes["p:a"])).toBe("demoflow.chip.simReply");
  expect(simChip("p:b", m.nodes["p:b"])).toBeNull();
  expect(simChip("p:c", m.nodes["p:c"])).toBe("demoflow.chip.simReply");
  m = applyEvent(m, { ...base, seq: 4, kind: "world", node: "booking", state: "active", title: "Customer reply", simulated: true, edge: { from: "w:customer", to: "booking" }, data: {} });
  expect(simChip("w:customer", m.nodes["w:customer"])).toBe("demoflow.chip.simParty");
  // A carrier confirmation on a leg names its world actor without an edge.
  m = applyEvent(m, { ...base, seq: 5, kind: "step", node: "leg:1", state: "active", title: "Carrier booking confirmed", simulated: false,
    data: { actor: { id: "w:carrier", source: "world" } } });
  expect(simChip("w:carrier", m.nodes["w:carrier"])).toBe("demoflow.chip.simParty");
});

test("the phone layout is one column that fits its width, with nothing overlapping", () => {
  const m = buildModel(events);
  const lay = layout(m, { narrow: true });
  const boxes = Object.entries(lay.pos);
  boxes.forEach(([id, b]) => {
    expect(b.x - b.w / 2).toBeGreaterThanOrEqual(0);
    expect(b.x + b.w / 2).toBeLessThanOrEqual(lay.width);
  });
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const [, a] = boxes[i]; const [, b] = boxes[j];
    const overlap = Math.abs(a.x - b.x) < (a.w + b.w) / 2 && Math.abs(a.y - b.y) < (a.h + b.h) / 2;
    expect(overlap ? `${boxes[i][0]} × ${boxes[j][0]}` : null).toBeNull();
  }
  expect(lay.pos.outreach.y).toBeLessThan(lay.pos[m.partners[0]].y);
  expect(lay.pos.offer.y).toBeLessThan(lay.pos.booking.y);
  expect(edgePath(lay.pos.offer, lay.pos.requester, lay)).toMatch(/^M [\d.]+ [\d.]+ L/);
});

test("one outside party, one node: the same company under two role ids folds into one", () => {
  const base = { run_id: "r", at: "2026-11-04T10:00:00Z", t: 1, sim_at: "2026-11-04T10:00:00Z", simulated: true };
  const name = "Kuljetus ja maanrakennus Katupojat Oy";
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "email_out", node: "booking", state: "waiting", title: "Booking request", edge: { from: "booking", to: "w:carrier" }, data: {} });
  m = applyEvent(m, { ...base, seq: 2, kind: "world", node: "booking", state: "active", title: "Carrier reply", edge: { from: "w:carrier", to: "booking" }, data: { facts: { name } } });
  m = applyEvent(m, { ...base, seq: 3, kind: "world", node: "leg:1", state: "active", title: "Picked up reported", edge: { from: "w:trucker", to: "leg:1" }, data: { facts: { name } } });
  m = applyEvent(m, { ...base, seq: 4, kind: "world", node: "delivery", state: "done", title: "Delivered", edge: { from: "w:trucker", to: "delivery" }, data: {} });
  expect(m.actors).toEqual(["w:carrier"]);
  expect(m.nodes["w:trucker"]).toBeUndefined();
  expect(Object.keys(m.edges).sort()).toEqual(["booking|w:carrier", "delivery|w:carrier", "leg:1|w:carrier"]);
  expect(nodeDetail(m, "w:carrier").events.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
  expect(layout(m).actors.filter((id) => id === "w:carrier" || id === "w:trucker")).toEqual(["w:carrier"]);
});

test("a send on a stage the shipment has passed is done, not waiting", () => {
  const base = { run_id: "r", at: "2026-11-04T10:00:00Z", t: 1, sim_at: "2026-11-04T10:00:00Z", edge: null, data: {}, simulated: false };
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "email_out", node: "docs", state: "waiting", title: "CMR sent" });
  expect(m.nodes.docs.state).toBe("waiting");
  m = applyEvent(m, { ...base, seq: 2, kind: "step", node: "delivery", state: "done", title: "Delivery recorded" });
  expect(m.nodes.docs.state).toBe("done");
  m = applyEvent(m, { ...base, seq: 3, kind: "email_out", node: "docs", state: "waiting", title: "Re: status update · sent" });
  expect(m.nodes.docs.state).toBe("done");
  m = applyEvent(m, { ...base, seq: 4, kind: "email_out", node: "invoicing", state: "waiting", title: "Invoice sent to customer" });
  expect(m.nodes.invoicing.state).toBe("waiting");
  // A late reply on a passed stage does not reopen it; a stage still working when a later one completes is done.
  m = applyEvent(m, { ...base, seq: 5, kind: "world", node: "booking", state: "active", title: "Carrier reply · X received", edge: { from: "w:carrier", to: "booking" } });
  expect(m.nodes.booking.state).toBe("done");
  m = applyEvent(m, { ...base, seq: 6, kind: "world", node: "invoicing", state: "active", title: "Customer reply · Y received" });
  m = applyEvent(m, { ...base, seq: 7, kind: "decision", node: "payment", state: "done", title: "Payment matched" });
  expect(m.nodes.invoicing.state).toBe("done");
});

test("the documents subtitle counts distinct documents by code; uploads and attachments are left out", () => {
  const base = { run_id: "r", at: "2026-11-04T10:00:00Z", t: 1, sim_at: "2026-11-04T10:00:00Z", edge: null, simulated: false };
  const doc = (seq, kind, name) => ({ ...base, seq, kind: "document", node: "docs", state: "active", title: name, data: { documents: [{ kind, name }] } });
  let m = [doc(1, "booking_confirmation", "BC-1"), doc(2, "other", "packing-list.pdf"), doc(3, "packing_list", "PL-1"), doc(4, "cmr", "C-1"), doc(5, "cmr", "C-1")]
    .reduce(applyEvent, emptyModel());
  expect(docsSub(m)).toEqual({ kinds: ["cmr", "packing_list", "booking_confirmation"], n: 3, sent: false });
  m = applyEvent(m, { ...base, seq: 6, kind: "email_out", node: "docs", state: "waiting", title: "Re: Quote request · sent", data: { documents: [{ kind: "attachment", name: "C-1.pdf" }] } });
  expect(docsSub(m).sent).toBe(true);
});

test("a party's chip follows data.actor once the run sends it; an event without an actor claims nothing", () => {
  const base = { run_id: "r", at: "2026-10-25T10:00:00Z", t: 1, sim_at: "2026-10-25T10:00:00Z", simulated: true };
  // Before the field: a simulated world event marks its sender.
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "world", node: "booking", state: "active", title: "Carrier reply", edge: { from: "w:carrier", to: "booking" }, data: {} });
  expect(simChip("w:carrier", m.nodes["w:carrier"])).toBe("demoflow.chip.simParty");
  // The run sends data.actor: the guess goes, a real confirmation carries no chip, and an event without an actor adds none.
  m = applyEvent(m, { ...base, seq: 2, kind: "step", node: "leg:1", state: "active", title: "Carrier booking confirmed", simulated: false, edge: null,
    data: { actor: { id: "w:customer", source: "world" } } });
  expect(simChip("w:carrier", m.nodes["w:carrier"])).toBeNull();
  expect(simChip("w:customer", m.nodes["w:customer"])).toBe("demoflow.chip.simParty");
  m = applyEvent(m, { ...base, seq: 3, kind: "world", node: "booking", state: "active", title: "Carrier reply", edge: { from: "w:carrier", to: "booking" }, data: {} });
  expect(simChip("w:carrier", m.nodes["w:carrier"])).toBeNull();
  m = applyEvent(m, { ...base, seq: 4, kind: "world", node: "leg:1", state: "active", title: "Carrier reply", edge: { from: "w:carrier", to: "leg:1" },
    data: { actor: { id: "w:carrier", source: "real" } } });
  expect(simChip("w:carrier", m.nodes["w:carrier"])).toBeNull();
});

test("a failed run remembers the stage it was on and why", () => {
  const base = { run_id: "r", at: "2026-10-25T11:33:15Z", t: 1, sim_at: "2026-10-25T11:33:15Z", edge: null, simulated: false };
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "decision", node: "replies", state: "done", title: "Best rate: X", data: {} });
  m = applyEvent(m, { ...base, seq: 2, kind: "step", node: "pricing", state: "active", title: "Pricing the offer", data: {} });
  m = applyEvent(m, { ...base, seq: 3, kind: "tool", node: "pricing", state: null, title: "quote", data: {} });
  m = applyEvent(m, { ...base, seq: 4, kind: "phase", node: "intake", state: "failed", title: "Run stopped by an error",
    data: { reasoning: "ValueError: Native quoting produced no option from the winning partner rate" } });
  expect(m.ended).toMatchObject({ state: "failed", node: "pricing", reason: "ValueError: Native quoting produced no option from the winning partner rate" });
});

test("every run status the backend writes has a badge and an EN and FI label", () => {
  // Read from the backend source: any demo_runs write that sets "status": "<value>".
  const dicts = require("@/i18n").default;
  const root = path.resolve(__dirname, "../../../../backend/services");
  const statuses = new Set();
  const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true }).forEach((d) => {
    const f = path.join(dir, d.name);
    if (d.isDirectory()) walk(f);
    else if (f.endsWith(".py")) fs.readFileSync(f, "utf8").split("\n").filter((l) => l.includes("demo_runs"))
      .forEach((l) => [...l.matchAll(/"status": "([a-z_]+)"/g)].forEach((m) => statuses.add(m[1])));
  });
  walk(root);
  expect(statuses.size).toBeGreaterThan(3);
  [...statuses, "done", "done_open", "lifecycle"].forEach((s) => {
    expect([s, RUN_STATUS_BADGE[s]]).toEqual([s, expect.any(String)]);
    ["en", "fi"].forEach((l) => expect([s, l, dicts[l][`demoflow.status.${s}`]]).toEqual([s, l, expect.any(String)]));
  });
});

test("an excluded rate marks its partner; the offer waiting on approval keeps its card's reasons", () => {
  const base = { run_id: "r", at: "2026-10-25T11:56:00Z", t: 1, sim_at: "2026-10-25T11:56:00Z", edge: null, simulated: true };
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "step", node: "p:s", state: "active", title: "Seikkala Transport Oy", data: {} });
  m = applyEvent(m, { ...base, seq: 2, kind: "email_in", node: "p:s", state: "done", title: "Rate received", data: { facts: { "all-in": "EUR 400.00" } } });
  m = applyEvent(m, { ...base, seq: 3, kind: "decision", node: "replies", state: "active", title: "1 rate not valid on 2026-11-04",
    data: { expired: [{ partner: "Seikkala Transport Oy", cost_cents: 40000, valid_to: "2026-11-01" }] } });
  expect(m.nodes["p:s"].meta.expired).toEqual({ valid_to: "2026-11-01", cost_cents: 40000 });
  expect(summary(m)).toMatchObject({ rates: 0, excluded: 1 });
});

test("an agreed rate card counts as a rate and keeps its reply source; a card that does not fit is excluded", () => {
  const base = { run_id: "r", at: "2026-10-25T13:00:00Z", t: 1, sim_at: "2026-10-25T13:00:00Z", simulated: true };
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "decision", node: "p:k", state: "done", edge: { from: "p:k", to: "replies" },
    title: "Rate already agreed: EUR 760.00 · 3 days", data: { title_key: "partner.rate_agreed", title_vars: { amount: 760, days: 3 }, reply: { source: "world" },
      agreed: { rate_card_id: "rc1", cost_cents: 76000 }, facts: { "all-in": "EUR 760.00", transit: "3 days" } } });
  m = applyEvent(m, { ...base, seq: 2, kind: "decision", node: "p:x", state: "done", edge: { from: "p:x", to: "replies" },
    title: "Agreed rate does not fit this request", data: { title_key: "partner.agreed_rate_skipped", agreed: { rate_card_id: "rc0" } } });
  expect(simChip("p:k", m.nodes["p:k"])).toBe("demoflow.chip.simReply");
  expect(nodeSub("p:k", m.nodes["p:k"])).toBe("EUR 760.00 · 3 days");
  expect(agreedChip("p:k", m.nodes["p:k"])).toBe("demoflow.rank.agreed");
  expect(m.nodes["p:x"].meta.agreedSkipped).toEqual({ rate_card_id: "rc0" });
  expect(summary(m)).toMatchObject({ rates: 1, excluded: 0 });
});

test("a price from market rates carries its pill on pricing and the offer", () => {
  const base = { run_id: "r", at: "2026-10-25T13:00:00Z", t: 1, sim_at: "2026-10-25T13:00:00Z", edge: null, simulated: false };
  let m = applyEvent(emptyModel(), { ...base, seq: 1, kind: "step", node: "pricing", state: "done", title: "EUR 601.72 · 2 days",
    data: { title_key: "pricing.priced", title_vars: { amount: 601.72, days: 2 }, price_basis: "market_reference" } });
  m = applyEvent(m, { ...base, seq: 2, kind: "email_out", node: "offer", state: "done", title: "Offer sent · EUR 601.72", data: { price_basis: "market_reference" } });
  expect(basisChip("pricing", m.nodes.pricing)).toBe("demoflow.chip.market");
  expect(basisChip("offer", m.nodes.offer)).toBe("demoflow.chip.market");
  m = applyEvent(m, { ...base, seq: 3, kind: "step", node: "pricing", state: "done", title: "x", data: { price_basis: "partner_offer" } });
  expect(basisChip("pricing", m.nodes.pricing)).toBeNull();
});

test("a reply received before this run started carries its provenance label; it still counts as the rate the run took over", () => {
  const base = { run_id: "r", t: 0, simulated: true, edge: null };
  let m = applyEvent(emptyModel(), { ...base, seq: 1, at: "2026-10-25T13:56:56Z", sim_at: "2026-10-25T13:56:56Z", kind: "step", node: "requester", state: "done", title: "x", data: {} });
  m = applyEvent(m, { ...base, seq: 2, at: "2026-10-25T13:57:46Z", kind: "email_in", node: "p:k", state: "done", title: "Rate received: EUR 740.00 · 3 days",
    data: { reply: { source: "world", received_at: "2026-10-25T12:32:13Z", source_run_ref: "DQ-JU9SRY" }, facts: { "all-in": "EUR 740.00" }, emails: [{ direction: "in", subject: "Re: RFQ" }] } });
  m = applyEvent(m, { ...base, seq: 3, at: "2026-10-25T13:57:50Z", kind: "email_in", node: "p:n", state: "done", title: "Rate received: EUR 500.00 · 2 days",
    data: { reply: { source: "world", received_at: "2026-10-25T13:57:49Z" }, facts: { "all-in": "EUR 500.00" }, emails: [{ direction: "in", subject: "Re: RFQ" }] } });
  expect(nodeDetail(m, "p:k").emails[0]).toMatchObject({ earlier: true, earlierRef: "DQ-JU9SRY" });
  expect(nodeDetail(m, "p:n").emails[0].earlier).toBeUndefined();
  expect(summary(m).rates).toBe(2);
});
