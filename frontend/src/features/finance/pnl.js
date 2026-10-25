// P&L row readers for GET /finance/reports/pnl/{shipments,lanes} (services/finance_reports.py).
// Revenue = issued invoices net of VAT and disbursements; cost = matched carrier invoices (actual) + open accruals (accrued).
// All amounts are EUR cents. Missing values stay null so the table shows "—".
const v = (x) => (x === undefined ? null : x);

/** Shipment row: {shipment{id, reference, customer, lane{key}, mode}, revenue_cents, cost_*_cents, gross_profit_cents,
 *  margin_pct, basis, quoted{margin_cents, margin_pct}, variance{margin_cents, margin_pct_points}, warnings[]} */
export function readShipmentRow(r) {
  const s = r.shipment || {};
  return {
    id: v(s.id),
    reference: v(s.reference),
    customer: v(s.customer),
    lane: v(s.lane?.key),
    mode: v(s.mode),
    revenue: v(r.revenue_cents),
    cost: v(r.cost_cents),
    actual: v(r.cost_actual_cents),
    accrued: v(r.cost_accrued_cents),
    gp: v(r.gross_profit_cents),
    margin: v(r.margin_pct),
    basis: v(r.basis),
    quotedMargin: v(r.quoted?.margin_cents),
    quotedPct: v(r.quoted?.margin_pct),
    variance: v(r.variance?.margin_cents),
    variancePts: v(r.variance?.margin_pct_points),
    fx: v(r.fx_result_cents),
    warnings: r.warnings || [],
  };
}

/** Lane row / totals: {lane, mode, shipments, revenue_cents, cost_cents, cost_actual_cents, cost_accrued_cents,
 *  gross_profit_cents, margin_pct, gp_per_shipment_cents, quoted_margin_cents, margin_variance_cents,
 *  margin_variance_pct_points, cost_actual_share_pct} */
export function readSumRow(r) {
  if (!r) return null;
  return {
    lane: v(r.lane),
    mode: v(r.mode),
    shipments: v(r.shipments),
    revenue: v(r.revenue_cents),
    cost: v(r.cost_cents),
    actual: v(r.cost_actual_cents),
    accrued: v(r.cost_accrued_cents),
    gp: v(r.gross_profit_cents),
    margin: v(r.margin_pct),
    perShipment: v(r.gp_per_shipment_cents),
    quotedMargin: v(r.quoted_margin_cents),
    quotedPct: v(r.quoted_margin_pct),
    variance: v(r.margin_variance_cents),
    variancePts: v(r.margin_variance_pct_points),
    actualShare: v(r.cost_actual_share_pct),
    fx: v(r.fx_result_cents),
  };
}
