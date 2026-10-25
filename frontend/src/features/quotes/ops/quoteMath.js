// Live quote arithmetic for the ops editor, mirroring services/pricing (V2 backend). Money is integer cents.
// - Markup % (PATCH option.margin_pct) is a markup on cost: unit_price = round(unit_cost * (1 + m/100)), applied
//   only to non-pass-through lines with unit_cost > 0; fixed fees with cost 0 keep their price; pass-through
//   lines are priced at cost (RULE Q-2: no margin on outlays). Lines are applied first, then the markup.
// - Margin % shown = gross margin share of sell excl. pass-through = margin_cents / subtotal_cents * 100.

export const MARGIN_MODE = "markup"; // "markup" (on cost) | "gross" (share of sell)

const n = (v) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)) ? 0 : Number(v));

// A line as the backend priced it keeps its stored amount / cost: unit prices are whole cents, so qty * unit can drift
// (a contract line of 290.08 EUR over 1 480 kg has unit cost 20 c, and 1 480 * 20 c = 296.00 EUR). Once a person
// edits the line (_edited, set by LinesEditor) or for a new line without stored totals, qty * unit is the truth.
const stored = (l, k) => !l._edited && Number.isInteger(l[k]);
export const lineAmount = (l) => (stored(l, "amount_cents") ? l.amount_cents : Math.round(n(l.qty) * n(l.unit_price_cents)));
export const lineCost = (l) => (stored(l, "cost_cents") ? l.cost_cents : Math.round(n(l.qty) * n(l.unit_cost_cents)));

export function optionTotals(lines) {
  let total = 0;
  let cost = 0;
  let pass = 0;
  let passCost = 0;
  (lines || []).forEach((l) => {
    const a = lineAmount(l);
    const c = lineCost(l);
    total += a;
    cost += c;
    if (l.is_pass_through) { pass += a; passCost += c; }
  });
  const services = total - pass;
  const servicesCost = cost - passCost;
  const margin = total - cost;
  const marginPct = services > 0 ? ((services - servicesCost) / services) * 100 : null;
  return { total, cost, pass, services, margin, marginPct };
}

export function priceForMargin(costCents, pct) {
  const m = n(pct) / 100;
  if (MARGIN_MODE === "markup") return Math.round(costCents * (1 + m));
  if (m >= 0.99) return Math.round(costCents * 100);
  return Math.round(costCents / (1 - m));
}

/** Reprice non-pass-through lines that have a cost; fee lines without cost keep their price. */
export function applyMargin(lines, pct) {
  return (lines || []).map((l) => {
    if (l.is_pass_through) return { ...l, unit_price_cents: l.unit_cost_cents ?? l.unit_price_cents };
    if (!n(l.unit_cost_cents)) return l;
    return { ...l, unit_price_cents: priceForMargin(n(l.unit_cost_cents), pct) };
  });
}

export const KNOWN_REASONS = ["threshold", "dangerous_goods", "out_of_network", "no_freight"];

/** Approval reason codes: reasons[] when present, else the single reason (code or plain text). */
export function approvalReasons(approval) {
  if (!approval) return [];
  if (Array.isArray(approval.reasons) && approval.reasons.length) return approval.reasons;
  if (approval.reason) return [approval.reason];
  return [];
}

export function emptyLine(vat = "FI_25_5") {
  return { code: "", description: "", basis: "PER_SHIPMENT", qty: 1, unit_cost_cents: 0, unit_price_cents: 0, vat_code: vat, is_pass_through: false };
}

/**
 * Line for the PATCH: keeps every field the backend sent (description_fi, stage, payer, note, leg_seq, …)
 * so nothing is lost on save, drops the editor key and recomputes amount / cost.
 */
export function cleanLine(l) {
  const { _k, _edited, ...rest } = l;
  return {
    ...rest,
    code: (l.code || "").trim().toUpperCase(), description: l.description || "", basis: l.basis || "PER_SHIPMENT",
    qty: n(l.qty), unit_cost_cents: Math.round(n(l.unit_cost_cents)), unit_price_cents: Math.round(n(l.unit_price_cents)),
    amount_cents: lineAmount(l), cost_cents: lineCost(l), vat_code: l.vat_code || null, is_pass_through: !!l.is_pass_through,
  };
}
