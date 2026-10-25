// Live flow map model: folds the run's real events (demo_run_events, schema in shared/interfaces.md §2-3) into node and
// edge state, and lays the nodes out. Pure functions, no React: the map draws whatever this returns.

export const QUOTE_NODES = ["requester", "intake", "clarify", "research", "outreach", "replies", "pricing", "offer"];
export const LIFECYCLE_NODES = ["booking", "docs", "customs", "exceptions", "delivery", "invoicing", "payment"];
export const ACTOR_KINDS = ["customer", "carrier", "trucker", "terminal", "customs", "consignee", "bank", "insurer", "warehouse"];
// Events on these nodes drive the header (clock) or the trace only; they never get a node.
const NO_NODE = new Set(["clock", "w:unrouted", "run"]);

export const W = 1608;
export const H = 864;
export const NODE = { w: 188, h: 62 };
export const PARTNER = { w: 236, h: 66 };
export const ACTOR = { w: 212, h: 54 };

export function nodeGroup(id) {
  if (id.startsWith("p:")) return "partner";
  if (id.startsWith("w:")) return "actor";
  if (id.startsWith("leg:")) return "leg";
  if (QUOTE_NODES.includes(id)) return "quote";
  if (LIFECYCLE_NODES.includes(id)) return "lifecycle";
  return "other";
}

export const actorKind = (id) => id.split(":")[1] || "";

/** Run-level events (an ops stop, a fatal error) belong to the header status line, never to a stage node. The backend
 * emits them as a phase event on intake, or (lifecycle stop) as a note with data.stopped_note; node "run" is accepted too. */
export function isRunEvent(ev) {
  return ev?.node === "run" || Boolean(ev?.data?.stopped_note)
    || (ev?.kind === "phase" && ev.node === "intake" && (ev.state === "done" || ev.state === "failed"));
}

export function emptyModel() {
  return { events: [], seen: new Set(), nodes: {}, edges: {}, partners: [], legs: [], actors: [], others: [], lastSeq: 0, phase: "quote", ended: null,
    alias: {}, passedRank: -1, actorField: false };
}

const edgeKey = (a, b) => (a < b ? `${a}|${b}` : `${b}|${a}`);

// Shipment stages in order. A stage the shipment has gone past cannot still be waiting on a send of its own.
const STAGE_RANK = { booking: 0, docs: 1, customs: 2, delivery: 4, invoicing: 5, payment: 6 };
export const stageRank = (id) => (id?.startsWith("leg:") ? 3 : STAGE_RANK[id] ?? null);

/** Every run status the backend sets on demo_runs (services/demo_autoquote, demo_world), plus the map's own done_open,
 * with its badge tone. Each needs a demoflow.status.<status> label in EN and FI (guarded in model.test.js). */
export const RUN_STATUS_BADGE = { deferred: "in-progress", running: "in-progress", awaiting_requester: "in-review", awaiting_approval: "in-review", offer_sent: "submitted",
  lifecycle: "in-progress", done: "success", done_open: "expired", failed: "failed", stopped: "expired", interrupted: "expired" };

/** One outside party, one node: the same company can arrive under two role ids (w:carrier for the booking, w:trucker
 * for the leg). Fold `from` into `into`: its edges, its events, its provenance. */
function mergeActor(m, from, into) {
  m.alias = { ...m.alias, [from]: into };
  Object.keys(m.alias).forEach((k) => { if (m.alias[k] === from) m.alias[k] = into; });
  Object.values(m.edges).forEach((e) => {
    if (e.a !== from && e.b !== from) return;
    const a = e.a === from ? into : e.a;
    const b = e.b === from ? into : e.b;
    const k = edgeKey(a, b);
    const cur = m.edges[k];
    const dirs = Object.fromEntries(Object.keys(e.dirs || {}).map((d) => [d.split(">").map((x) => (x === from ? into : x)).join(">"), true]));
    const lastFrom = e.lastFrom === from ? into : e.lastFrom;
    const lastTo = e.lastTo === from ? into : e.lastTo;
    delete m.edges[e.key];
    m.edges[k] = cur
      ? { ...cur, count: cur.count + e.count, world: cur.world || e.world, dirs: { ...cur.dirs, ...dirs },
        ...(e.lastSeq > cur.lastSeq ? { lastSeq: e.lastSeq, lastFrom, lastTo } : {}) }
      : { ...e, key: k, a, b, dirs, lastFrom, lastTo };
  });
  const f = m.nodes[from];
  const t = m.nodes[into];
  if (f && t) {
    m.nodes[into] = { ...t, seqs: [...t.seqs, ...f.seqs].sort((x, y) => x - y), lastSeq: Math.max(t.lastSeq, f.lastSeq),
      meta: { ...f.meta, ...t.meta, actorSource: t.meta.actorSource || f.meta.actorSource } };
  }
  delete m.nodes[from];
  m.actors = m.actors.filter((id) => id !== from);
}

/** Ids a node answers to: its own and any party folded into it. */
export const aliasesOf = (m, id) => [id, ...Object.keys(m.alias || {}).filter((k) => m.alias[k] === id)];

function touchNode(m, id) {
  if (!id || NO_NODE.has(id)) return null;
  let n = m.nodes[id];
  if (!n) {
    n = m.nodes[id] = { id, state: "idle", title: "", seqs: [], lastSeq: 0, lastAt: null, meta: {} };
    const g = nodeGroup(id);
    if (g === "partner") m.partners.push(id);
    else if (g === "leg") m.legs.push(id);
    else if (g === "actor") m.actors.push(id);
    else if (g === "other") m.others.push(id);
  }
  return n;
}

/** Apply one event. Returns the model (mutated copy-on-write at the top level so React sees a new object). */
export function applyEvent(prev, ev) {
  if (!ev || prev.seen.has(ev.seq)) return prev;
  const m = { ...prev, nodes: { ...prev.nodes }, edges: { ...prev.edges }, partners: [...prev.partners], legs: [...prev.legs],
    actors: [...prev.actors], others: [...prev.others], events: [...prev.events, ev], seen: new Set(prev.seen) };
  m.seen.add(ev.seq);
  // The run's start: the first event's time less its offset. A reply received before it comes from an earlier run on a
  // reused thread (data.reply.received_at, the original receive time; data.reply.source_run_ref names that run). It is
  // labelled as such in the panel only: a reuse run takes those replies over on purpose, so they still set the card and
  // count as rates; what this run decided (partner.rate_agreed, the ranking) supersedes them where it differs.
  if (!m.startAt && ev.at) m.startAt = new Date(Date.parse(ev.at) - (Number(ev.t) || 0) * 1000).toISOString();
  const rx = ev.data?.reply?.received_at;
  const earlier = ev.kind === "email_in" && (ev.data?.reply?.earlier_run === true || Boolean(rx && m.startAt && Date.parse(rx) < Date.parse(m.startAt)));
  if (earlier) m.earlier = new Set([...(m.earlier || []), ev.seq]);
  m.lastSeq = Math.max(m.lastSeq, ev.seq);
  m.alias = prev.alias || {};
  // Events keep their own ids in the trace; the map reads them through the party merges.
  const as = (id) => (id && m.alias[id]) || id;
  const original = ev;
  if (m.alias[ev.node] || (ev.edge && (m.alias[ev.edge.from] || m.alias[ev.edge.to]))) {
    ev = { ...ev, node: as(ev.node), edge: ev.edge ? { from: as(ev.edge.from), to: as(ev.edge.to) } : ev.edge };
  }
  if (isRunEvent(ev)) {
    // Where it ended: the stage of the run's last step event; why: the event's error key or message.
    m.ended = { state: ev.state === "failed" ? "failed" : "done", title: ev.title, at: ev.at, sim_at: ev.sim_at, node: m.lastStage || null,
      errorKey: ev.data?.error_key || null, reason: ev.data?.error || ev.data?.reasoning || null };
    return m;
  }
  if (ev.kind === "phase" && ev.node === "offer") m.phase = "offer_sent";
  const g = ev.node ? nodeGroup(ev.node) : "";
  if ((g === "lifecycle" || g === "leg") && m.phase !== "done") m.phase = "lifecycle";
  if (ev.kind === "phase" && ev.node === "payment") {
    m.phase = "done";
    // Paid, but a person still holds an item (claim_review_handoff: a damage claim with a person for review).
    m.endReason = ev.data?.end_reason || null;
  }
  // The quote phase is over once the offer is out: a quote step still marked working has finished.
  if (ev.kind === "phase" && ev.node === "offer") {
    QUOTE_NODES.forEach((id) => { if (m.nodes[id]?.state === "active") m.nodes[id] = { ...m.nodes[id], state: "done" }; });
  }

  if (ev.node && ev.kind !== "tool" && ev.kind !== "clock" && !ev.node.startsWith("w:") && !NO_NODE.has(ev.node)) m.lastStage = ev.node;
  const n0 = touchNode(m, ev.node);
  const rank = stageRank(ev.node);
  if (n0) {
    const n = (m.nodes[ev.node] = { ...n0, seqs: [...n0.seqs, ev.seq], lastSeq: ev.seq, lastAt: ev.at, meta: { ...n0.meta } });
    // A stage the shipment has already passed stays done: a status update sent after delivery is not waiting, and a
    // late reply on it (the carrier answering after the documents went out) does not reopen it.
    const passed = rank !== null && rank < (m.passedRank ?? -1);
    const state = passed && ((ev.state === "waiting" && ev.kind === "email_out") || ev.state === "active") ? "done" : ev.state;
    if (state) { n.state = state; n.stateKind = ev.kind; }
    if (ev.title && ev.kind !== "tool") { n.title = ev.title; n.titleEv = original; }
    const d = ev.data || {};
    if (d.partner && !n.meta.partner) n.meta.partner = d.partner;
    if (n.id.startsWith("p:") && !n.meta.partner && ev.title && ev.kind === "step") n.meta.name = ev.title;
    if (d.facts && !(ev.kind === "tool" && ev.simulated)) n.meta.facts = { ...(n.meta.facts || {}), ...d.facts };
    if (ev.simulated) n.meta.simulated = true;
    // What the price rests on: a partner offer or Huolitsija's market reference (price_basis).
    if (d.price_basis) n.meta.priceBasis = d.price_basis;
    // Who wrote a partner's reply: data.reply.source ("world" | "real"); before that field, the event's simulated flag.
    if (n.id.startsWith("p:") && ev.kind === "email_in") n.meta.replySource = ev.data?.reply?.source || (ev.simulated ? "world" : "real");
    // A rate card production already accepted (partner.rate_agreed): no new reply in this run, but the card's own reply
    // source; a card that does not fit this request (partner.agreed_rate_skipped) is left out like an expired rate.
    const key = ev.data?.title_key;
    if (n.id.startsWith("p:") && key === "partner.rate_agreed") {
      n.meta.agreed = ev.data?.agreed || {};
      if (ev.data?.reply?.source) n.meta.replySource = ev.data.reply.source;
    }
    if (n.id.startsWith("p:") && key === "partner.agreed_rate_skipped") n.meta.agreedSkipped = ev.data?.agreed || {};
  }
  // Rates that were in but not valid on the ready date (replies.expired, and next to the best-rate ranking): the
  // partner's node shows the rate as excluded.
  (Array.isArray(ev.data?.expired) ? ev.data.expired : []).forEach((x) => {
    const id = m.partners.find((pid) => x?.partner && partnerName(m.nodes[pid]) === x.partner);
    if (id) m.nodes[id] = { ...m.nodes[id], meta: { ...m.nodes[id].meta, expired: { valid_to: x.valid_to || null, cost_cents: x.cost_cents ?? null } } };
  });
  if (rank !== null && ev.state === "done" && rank > (m.passedRank ?? -1)) {
    m.passedRank = rank;
    Object.keys(m.nodes).forEach((id) => {
      const r = stageRank(id);
      const nd = m.nodes[id];
      if (r !== null && r < rank && ((nd.state === "waiting" && nd.stateKind === "email_out") || nd.state === "active")) m.nodes[id] = { ...nd, state: "done" };
    });
  }
  if (ev.edge && ev.edge.from && ev.edge.to) {
    [ev.edge.from, ev.edge.to].forEach((id) => touchNode(m, id));
    const k = edgeKey(ev.edge.from, ev.edge.to);
    const e = m.edges[k] || { key: k, a: ev.edge.from, b: ev.edge.to, count: 0, kinds: {} };
    m.edges[k] = { ...e, count: e.count + 1, lastSeq: ev.seq, lastFrom: ev.edge.from, lastTo: ev.edge.to, world: e.world || ev.kind === "world",
      dirs: { ...(e.dirs || {}), [`${ev.edge.from}>${ev.edge.to}`]: true },
      kinds: { ...e.kinds, [ev.kind]: (e.kinds[ev.kind] || 0) + 1 } };
    // A world event names its sender (lane 2 puts e.g. the carrier's name in facts.name): label that actor with it.
    const from = ev.edge.from;
    const name = ev.data?.facts?.name;
    if (from.startsWith("w:") && name && m.nodes[from] && !m.nodes[from].meta.name) {
      m.nodes[from] = { ...m.nodes[from], meta: { ...m.nodes[from].meta, name } };
      const twin = m.actors.find((id) => id !== from && m.nodes[id]?.meta?.name === name);
      if (twin) mergeActor(m, from, twin);
    }
  }
  // Who a party is comes from data.actor ({id, source: "world" | "real"}), also on events without an edge (a carrier
  // confirmation on a leg). No actor on an event means no claim either way. Runs recorded before the field existed
  // fall back to a simulated world event's endpoints; the first event that carries data.actor drops those guesses.
  const actor = ev.data?.actor;
  if (actor && !m.actorField) {
    m.actorField = true;
    Object.keys(m.nodes).forEach((id) => {
      if (m.nodes[id].meta.actorGuess) m.nodes[id] = { ...m.nodes[id], meta: { ...m.nodes[id].meta, actorSource: null, actorGuess: false } };
    });
  }
  const guess = !actor && !m.actorField && ev.kind === "world" && ev.simulated;
  const actorSource = actor?.source || (guess ? "world" : null);
  const actorIds = (actor?.id ? [actor.id] : guess && ev.edge ? [ev.edge.from, ev.edge.to] : []).map(as);
  actorIds.forEach((id) => {
    if (!id?.startsWith("w:") || !actorSource || !touchNode(m, id)) return;
    const meta = m.nodes[id].meta;
    if (meta.actorSource !== actorSource || meta.actorGuess !== guess) m.nodes[id] = { ...m.nodes[id], meta: { ...meta, actorSource, actorGuess: guess } };
  });
  return m;
}

/** A partner whose rate production already accepted: its card says so next to the price (partner.rate_agreed). */
export function agreedChip(id, node) {
  return id.startsWith("p:") && node?.meta?.agreed ? "demoflow.rank.agreed" : null;
}

/** The price-basis pill on pricing and the offer: priced from market rates, no partner rate behind it. */
export function basisChip(id, node) {
  return (id === "pricing" || id === "offer") && node?.meta?.priceBasis === "market_reference" ? "demoflow.chip.market" : null;
}

/** The chip a node carries: a partner reply or an outside party written by the world simulator. */
export function simChip(id, node) {
  if (id.startsWith("p:") && node?.meta?.replySource === "world") return "demoflow.chip.simReply";
  if (id.startsWith("w:") && node?.meta?.actorSource === "world") return "demoflow.chip.simParty";
  return null;
}

export function buildModel(events) {
  return [...events].sort((a, b) => a.seq - b.seq).reduce(applyEvent, emptyModel());
}

/** Human name of a partner node. */
export function partnerName(node) {
  return node?.meta?.partner?.name || node?.meta?.name || "";
}

/** Headline numbers for the header: partners reached, rates in, offer total. Derived from events only. */
export function summary(m) {
  const partners = m.partners.length;
  let rfqs = 0;
  let rates = 0;
  let offer = null;
  m.events.forEach((ev) => {
    if (ev.node?.startsWith("p:") && ev.kind === "email_out" && ev.edge?.from === "outreach") rfqs += 1;
    if (ev.node === "offer" && ev.kind === "email_out") offer = ev.data?.facts?.total || offer;
  });
  // A rate is a reply that carries a price; a decline is a reply too, but not a rate.
  const quoted = new Set(m.events.filter((ev) => ev.node?.startsWith("p:") && ((ev.kind === "email_in" && ev.data?.facts?.["all-in"])
    || ev.data?.title_key === "partner.rate_agreed")).map((ev) => m.alias?.[ev.node] || ev.node));
  // A rate excluded as not valid on the ready date is counted apart from the valid ones.
  const excluded = [...quoted].filter((id) => m.nodes[id]?.meta?.expired || m.nodes[id]?.meta?.agreedSkipped).length;
  rates = quoted.size - excluded;
  const docs = m.events.reduce((s, ev) => s + ((ev.data?.documents || []).length), 0);
  const world = m.events.filter((ev) => ev.kind === "world").length;
  return { partners, rfqs, rates, excluded, offer, docs, world };
}

// ── Layout ──────────────────────────────────────────────────────────────────────────────────────────────────────────

// Quote phase columns (centre x): requester over intake, clarify over research, outreach, partners, replies, pricing, offer.
export const COLX = [114, 336, 558, 804, 1050, 1272, 1494];
const X0 = COLX[0];
const X1 = COLX[6];
const PITCH = 222;
export const Y = { arc: -40, quote: 262, top: 128, bottom: 396, gutter: 517, life: 608, exceptions: 698, actors: 818 };

const ACTOR_ANCHOR = { customer: "booking", carrier: "booking", trucker: "leg:1", terminal: "leg:2", customs: "customs", consignee: "delivery",
  bank: "payment", insurer: "docs", warehouse: "delivery", broker: "customs" };

/** Positions (centre x, y, w, h) for every node the model knows plus the planned journey, and the bounding box to fit. */
// Phone layout: one column read top to bottom (quote, shipment, outside parties), partners indented under outreach.
// Long jumps run on side rails instead of crossing the column: forward ones on the left, a partner's reply and any
// way back on the right.
export const NW = 380;
const N_NODE = { w: 300, h: 54 };
const N_PARTNER = { w: 276, h: 62 };
const N_ACTOR = { w: 300, h: 50 };
const N_ROW = 72;
const N_LABEL = 34;

function layoutNarrow(m) {
  const pos = {};
  const cx = NW / 2 + 6;
  const put = (id, x, y, size) => { pos[id] = { x, y, w: size.w, h: size.h, g: nodeGroup(id) }; };
  const bands = {};
  let y = 4 + N_LABEL + N_NODE.h / 2;
  const row = (id, size = N_NODE, dx = 0, gap = N_ROW) => { put(id, cx + dx, y, size); y += gap; };
  ["requester", "intake", "clarify", "research", "outreach"].forEach((id) => row(id));
  const partners = m.partners.length ? m.partners : ["p:next"];
  partners.forEach((id) => row(id, N_PARTNER, 12, N_PARTNER.h + 16));
  y += N_ROW - (N_PARTNER.h + 16);
  ["replies", "pricing", "offer"].forEach((id) => row(id));
  bands.quote = { top: 4, h: y - N_ROW + N_NODE.h / 2 + 14 - 4 };

  const lifeTop = bands.quote.top + bands.quote.h + 14;
  y = lifeTop + N_LABEL + N_NODE.h / 2;
  const legs = m.legs.length ? [...m.legs].sort((p, q) => Number(p.split(":")[1]) - Number(q.split(":")[1])) : ["leg:1"];
  const life = ["booking", "docs", "customs", ...legs, "delivery", "invoicing", "payment"];
  life.forEach((id) => {
    row(id);
    if (id === legs[legs.length - 1]) row("exceptions", { w: N_PARTNER.w, h: 46 }, 12);
  });
  m.others.forEach((id) => row(id));
  bands.life = { top: lifeTop, h: y - N_ROW + N_NODE.h / 2 + 14 - lifeTop };

  const actors = [...m.actors, ...["w:customer", "w:carrier", "w:customs", "w:bank"].filter((id) => !m.actors.includes(id) && !(m.alias || {})[id])];
  const worldTop = bands.life.top + bands.life.h + 12;
  y = worldTop + N_LABEL + N_ACTOR.h / 2;
  // One column as wide as the stage cards, so the side rails pass beside the outside parties, not across them.
  actors.forEach((id) => row(id, N_ACTOR, 0, N_ACTOR.h + 12));
  bands.world = { top: worldTop, h: y - (N_ACTOR.h + 12) + N_ACTOR.h / 2 + 14 - worldTop };

  const left = cx - N_NODE.w / 2;
  const right = cx + N_NODE.w / 2;
  const height = bands.world.top + bands.world.h + 8;
  return { pos, legs, actors, life, narrow: true, width: NW + 12, height, bands,
    rails: { left: left - 14, right: right + 12, back: right + 24 }, bbox: { x0: 0, x1: NW + 12, y0: 0, y1: height } };
}

export function layout(m, { narrow = false } = {}) {
  if (narrow) return layoutNarrow(m);
  const pos = {};
  const put = (id, x, y, size = NODE) => { pos[id] = { x, y, w: size.w, h: size.h }; };
  put("requester", COLX[0], Y.top);
  put("intake", COLX[0], Y.quote + 40);
  put("clarify", COLX[1], Y.top);
  put("research", COLX[1], Y.bottom);
  put("outreach", COLX[2], Y.quote);
  put("replies", COLX[4], Y.quote);
  put("pricing", COLX[5], Y.quote);
  // An offer on hold carries its reason: room for three lines.
  put("offer", COLX[6], Y.quote, m.nodes.offer?.state === "waiting" ? { w: NODE.w, h: 80 } : NODE);

  const partners = m.partners;
  // Room between partners for the "simulated reply" chip on each card's top edge.
  const gap = Math.min(74, 440 / Math.max(1, partners.length));
  partners.forEach((id, i) => put(id, COLX[3], Y.quote + (i - (partners.length - 1) / 2) * gap, { w: PARTNER.w, h: Math.min(PARTNER.h, gap - 6) }));
  if (!partners.length) put("p:next", COLX[3], Y.quote, PARTNER);

  // Shipment row reads left to right like the quote row: booking first, payment last.
  const legs = m.legs.length ? [...m.legs].sort((a, b) => Number(a.split(":")[1]) - Number(b.split(":")[1])) : ["leg:1"];
  const life = ["booking", "docs", "customs", ...legs, "delivery", "invoicing", "payment"];
  const step = (X1 - X0) / Math.max(1, life.length - 1);
  const lifeBox = { w: Math.min(NODE.w, step - 18), h: NODE.h };
  life.forEach((id, i) => put(id, X0 + step * i, Y.life, lifeBox));
  const legXs = legs.map((id) => pos[id].x);
  put("exceptions", legXs.reduce((s, x) => s + x, 0) / legXs.length, Y.exceptions, { w: NODE.w, h: 52 });

  // The usual outside parties stay on the map as upcoming until they join; any others appear when first used.
  const actors = [...m.actors, ...["w:customer", "w:carrier", "w:customs", "w:bank"].filter((id) => !m.actors.includes(id) && !(m.alias || {})[id])];
  // Outside parties: ordered by the stages they exchanged data with (weighted by exchanges; else their usual stage),
  // then spaced evenly across the row.
  const want = actors.map((id) => {
    let sx = 0;
    let n = 0;
    Object.values(m.edges).forEach((e) => {
      const other = e.a === id ? e.b : e.b === id ? e.a : null;
      if (other && pos[other] && nodeGroup(other) !== "actor") { sx += pos[other].x * e.count; n += e.count; }
    });
    const a = pos[ACTOR_ANCHOR[actorKind(id)]] || pos[legs[legs.length - 1]] || pos.booking;
    return { id, x: n ? sx / n : a.x, real: n > 0 };
  }).sort((p, q) => p.x - q.x || (q.real - p.real) || (p.id < q.id ? -1 : 1));
  const lo = 290;
  const hi = W - ACTOR.w / 2 - 20;
  const slot = want.length > 1 ? (hi - lo) / (want.length - 1) : 0;
  const minGap = ACTOR.w + 24;
  want.forEach((p, i) => { p.x = (Math.min(hi, Math.max(lo, p.x)) + (want.length > 1 ? lo + i * slot : p.x)) / 2; });
  for (let i = 1; i < want.length; i++) want[i].x = Math.max(want[i].x, want[i - 1].x + minGap);
  if (want.length) want[want.length - 1].x = Math.min(want[want.length - 1].x, hi);
  for (let i = want.length - 2; i >= 0; i--) want[i].x = Math.min(want[i].x, want[i + 1].x - minGap);
  want.forEach((p) => put(p.id, p.x, Y.actors, ACTOR));

  m.others.forEach((id, i) => put(id, X1 - i * (NODE.w + 16), Y.exceptions));

  const boxes = Object.values(pos);
  const bbox = {
    x0: Math.min(...boxes.map((b) => b.x - b.w / 2)) - 16,
    x1: Math.max(...boxes.map((b) => b.x + b.w / 2)) + 16,
    y0: Math.min(16, ...boxes.map((b) => b.y - b.h / 2)) - 16,
    y1: Math.max(...boxes.map((b) => b.y + b.h / 2)) + 16,
  };
  return { pos, legs, actors, life, bbox, width: W, height: H };
}

/** The planned flow, drawn as upcoming until data moves along it. Pairs of node ids. */
export function skeleton(m, lay) {
  const s = [["requester", "intake"], ["intake", "clarify"], ["intake", "research"], ["clarify", "requester"], ["clarify", "outreach"],
    ["research", "outreach"], ["replies", "pricing"], ["pricing", "offer"], ["offer", "requester"], ["offer", "booking"]];
  m.partners.forEach((p) => s.push(["outreach", p], [p, "replies"]));
  if (!m.partners.length) s.push(["outreach", "p:next"], ["p:next", "replies"]);
  for (let i = 0; i < lay.life.length - 1; i++) s.push([lay.life[i], lay.life[i + 1]]);
  return s;
}

/** SVG path between two laid-out nodes. Long jumps inside the quote band arc over the top instead of crossing it. */
function railPath(a, b, x, side) {
  // Out of a's side, along the rail at x, into b's side; rounded corners.
  const xa = a.x + (side * a.w) / 2;
  const xb = b.x + (side * b.w) / 2;
  const r = Math.min(10, Math.abs(b.y - a.y) / 2);
  const sy = Math.sign(b.y - a.y) || 1;
  const sx = Math.sign(x - xa) || side;
  return `M ${xa} ${a.y} L ${x - sx * r} ${a.y} Q ${x} ${a.y} ${x} ${a.y + sy * r} L ${x} ${b.y - sy * r} Q ${x} ${b.y} ${x - sx * r} ${b.y} L ${xb} ${b.y}`;
}

/** SVG path between two laid-out nodes. Long jumps inside the quote band arc over the top instead of crossing it.
 * On the phone layout (lay.narrow), neighbours join straight down and longer jumps take a side rail. */
export function edgePath(a, b, lay) {
  if (lay?.narrow) {
    const dy = b.y - a.y;
    const apart = Math.abs(dy) > N_ROW * 1.15 || Math.abs(b.x - a.x) > 60;
    if (apart) {
      if (dy < 0) return railPath(a, b, lay.rails.back, 1);
      if (a.g === "partner") return railPath(a, b, lay.rails.right, 1);
      return railPath(a, b, lay.rails.left, -1);
    }
    const sy = Math.sign(dy) || 1;
    const y0 = a.y + (sy * a.h) / 2;
    const y1 = b.y - (sy * b.h) / 2;
    const c = Math.max(10, Math.abs(y1 - y0) / 2);
    return `M ${a.x} ${y0} C ${a.x} ${y0 + sy * c}, ${b.x} ${y1 - sy * c}, ${b.x} ${y1}`;
  }
  // Between the bands (quote → shipment hand-off): down, across the gutter, down — rounded corners.
  if ((a.y < Y.gutter) !== (b.y < Y.gutter) && Math.abs(b.x - a.x) > PITCH) {
    const [u, d] = a.y < b.y ? [a, b] : [b, a];
    const y0 = u.y + u.h / 2;
    const y1 = d.y - d.h / 2;
    const g = Y.gutter;
    const r = 16;
    const sx = Math.sign(d.x - u.x);
    const path = `M ${u.x} ${y0} L ${u.x} ${g - r} Q ${u.x} ${g} ${u.x + sx * r} ${g} L ${d.x - sx * r} ${g} Q ${d.x} ${g} ${d.x} ${g + r} L ${d.x} ${y1}`;
    return a.y < b.y ? path : reversePath(u, d, g, r, sx, y0, y1);
  }
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  if (Math.abs(dx) > 3 * PITCH && a.y < Y.life && b.y < Y.life) {
    const y0 = a.y - a.h / 2;
    const y1 = b.y - b.h / 2;
    return `M ${a.x} ${y0} C ${a.x} ${Y.arc}, ${b.x} ${Y.arc}, ${b.x} ${y1}`;
  }
  // An outside party is reached from above, at its centre: a side entry would run along the party row through the
  // neighbouring cards (an exception → customer line crossing the customs authority it never involved).
  const toParty = (a.y >= Y.actors - 1) !== (b.y >= Y.actors - 1);
  if (!toParty && Math.abs(dx) >= Math.abs(dy) * 0.9) {
    const sx = Math.sign(dx) || 1;
    const x0 = a.x + (sx * a.w) / 2;
    const x1 = b.x - (sx * b.w) / 2;
    const c = Math.max(30, Math.abs(x1 - x0) / 2);
    return `M ${x0} ${a.y} C ${x0 + sx * c} ${a.y}, ${x1 - sx * c} ${b.y}, ${x1} ${b.y}`;
  }
  const sy = Math.sign(dy) || 1;
  const y0 = a.y + (sy * a.h) / 2;
  const y1 = b.y - (sy * b.h) / 2;
  const c = Math.max(24, Math.abs(y1 - y0) / 2);
  return `M ${a.x} ${y0} C ${a.x} ${y0 + sy * c}, ${b.x} ${y1 - sy * c}, ${b.x} ${y1}`;
}

function reversePath(u, d, g, r, sx, y0, y1) {
  return `M ${d.x} ${y1} L ${d.x} ${g + r} Q ${d.x} ${g} ${d.x - sx * r} ${g} L ${u.x + sx * r} ${g} Q ${u.x} ${g} ${u.x} ${g - r} L ${u.x} ${y0}`;
}

/** Short status line under a node's name: the price on a partner that quoted, else the latest step. */
export function nodeSub(id, node, days = (n) => `${n} days`, value = (v) => v, title = (n) => n.title || "") {
  if (!node) return "";
  const f = node.meta?.facts || {};
  // The priced offer (pricing step facts: sell, transit) reads like a partner's price.
  if (id === "pricing" && node.state === "done" && node.titleEv?.data?.title_key) return title(node);
  if (id === "pricing" && node.state === "done" && f.sell) {
    const m = String(f.transit || "").match(/^(\d+)\s*days?$/i);
    return [value(f.sell), m ? days(Number(m[1])) : f.transit].filter(Boolean).join(" · ");
  }
  if (id.startsWith("p:") && node.state === "done" && f["all-in"]) {
    const vars = node.titleEv?.data?.title_vars || {};
    const m = String(f.transit || "").match(/^(\d+)\s*days?$/i);
    const d = vars.days ?? (m ? Number(m[1]) : null);
    return [value(f["all-in"]), d != null ? days(d) : f.transit].filter(Boolean).join(" · ");
  }
  return title(node);
}

/** Intake's status line, read against clarification: once the questions are answered the missing details were asked,
 * not missing. The count comes from the event's data (title_vars.n, or the facts' list of missing fields), never from
 * the English title. -> {key, n} for the dictionary, or null. */
export function intakeSub(m) {
  const ev = m.nodes.intake?.titleEv;
  const d = ev?.data || {};
  let n = null;
  if (d.title_key === "intake.understood") n = Number(d.title_vars?.n || 0);
  else if (d.facts && "missing" in d.facts) n = String(d.facts.missing || "").split(",").map((s) => s.trim()).filter(Boolean).length;
  if (n === null) return null;
  if (!n) return { key: "demoflow.sub.intakeUnderstood", n };
  const asked = m.nodes.clarify?.state === "done";
  return { key: `demoflow.sub.intake${asked ? "Asked" : "Missing"}${n === 1 ? "One" : ""}`, n };
}

// Shipping documents first in the documents step's subtitle; uploads ("other") and e-mail attachments are not counted.
const DOC_ORDER = ["cmr", "packing_list", "commercial_invoice", "certificate_of_origin", "eur1_application", "customs_declaration", "booking_confirmation"];

/** The documents step's subtitle from its events' document codes, never from an e-mail subject: the distinct documents
 * (a new CMR version is the same CMR), their kinds in shipping order, and whether any went out by e-mail.
 * -> {kinds, n, sent} or null. */
export function docsSub(m) {
  const node = m.nodes.docs;
  if (!node) return null;
  const docs = new Map();
  let sent = false;
  m.events.forEach((ev) => {
    if (ev.node !== "docs") return;
    if (ev.kind === "email_out") sent = true;
    if (ev.kind !== "document") return;
    (ev.data?.documents || []).forEach((d) => {
      if (!d.kind || d.kind === "other" || d.kind === "attachment") return;
      docs.set(`${d.kind}|${d.name || d.title || ""}`, d.kind);
    });
  });
  if (!docs.size) return null;
  const rank = (k) => (DOC_ORDER.includes(k) ? DOC_ORDER.indexOf(k) : DOC_ORDER.length);
  const kinds = [...new Set(docs.values())].sort((a, b) => rank(a) - rank(b));
  return { kinds, n: docs.size, sent };
}

/** "+30 operates road; +15 based in FI (lane end)" → ["Operates road", "Based in FI (lane end)"]. */
export function reasonPoints(text) {
  if (!text) return [];
  const parts = text.split(/;\s*/).map((p) => p.replace(/^[+-]?\d+\s*/, "").trim()).filter(Boolean);
  if (parts.length < 2) return [];
  return parts.map((p) => p.charAt(0).toUpperCase() + p.slice(1));
}

// ── Node detail ─────────────────────────────────────────────────────────────────────────────────────────────────────

/** Everything the node panel shows, gathered from the node's events (newest last). */
export function nodeDetail(m, id) {
  const ids = aliasesOf(m, id);
  const evs = m.events.filter((ev) => (ids.includes(ev.node) && !isRunEvent(ev))
    || (ev.edge && (ids.includes(ev.edge.from) || ids.includes(ev.edge.to)) && !ids.includes(ev.node) && id.startsWith("w:")));
  const emails = [];
  const tools = [];
  const reasoning = [];
  const documents = [];
  const sources = [];
  const extra = {};
  let facts = {};
  evs.forEach((ev) => {
    const d = ev.data || {};
    const partnerReply = ev.node?.startsWith("p:") && ev.kind === "email_in";
    const replyWorld = partnerReply ? (ev.data?.reply?.source ? ev.data.reply.source === "world" : Boolean(ev.simulated)) : null;
    const earlier = Boolean(m.earlier?.has(ev.seq));
    (d.emails || []).forEach((e) => emails.push({ ...e, at: ev.at, sim_at: ev.sim_at, partnerReply,
      simulated: replyWorld ?? Boolean(e.simulated ?? ev.simulated), seq: ev.seq,
      ...(earlier ? { earlier: true, earlierRef: d.reply?.source_run_ref || d.reply?.run_ref || null, receivedAt: d.reply?.received_at } : {}) }));
    (d.tool_calls || []).forEach((tc) => tools.push({ ...tc, at: ev.at, seq: ev.seq, simulated: ev.simulated }));
    if (d.reasoning && !reasoning.some((r) => r.text === d.reasoning)) reasoning.push({ text: d.reasoning, at: ev.at, seq: ev.seq, key: d.title_key || null, agreed: d.agreed || null });
    (d.documents || []).forEach((doc) => { if (!documents.some((x) => x.file_id === doc.file_id)) documents.push({ ...doc, at: ev.at }); });
    (d.sources || []).forEach((s) => { if (!sources.some((x) => x.url === s.url)) sources.push(s); });
    // Facts a simulator wrote about itself stay with its tool call; Details shows what Huolitsija read and decided.
    if (d.facts && !(ev.kind === "tool" && ev.simulated)) facts = { ...facts, ...d.facts };
    ["partners", "ranking", "expired", "replies", "lines", "dropped"].forEach((k) => { if (Array.isArray(d[k])) extra[k] = d[k]; });
  });
  return { events: evs, emails, tools, reasoning, documents, sources, facts, extra };
}
