// Compare within the same price unit. Tiered rates use the lowest published unit price;
// minimum charges and other quantity brackets remain visible in the cell/tooltip.
export function lowestUnitPrice(rate) {
  if(rate.container_costs?.['40GP'] != null) return rate.container_costs['40GP'];
  if(rate.unit_cost_cents != null) return rate.unit_cost_cents;
  const prices=(rate.tiers||[]).map(t=>t.unit_cost_cents).filter(v=>v!=null);
  return prices.length ? Math.min(...prices) : Infinity;
}
export function compareRateBuy(a,b) {
  const group=String(a.basis).localeCompare(String(b.basis));
  if(group)return group;
  const x=lowestUnitPrice(a),y=lowestUnitPrice(b);
  return x===y?0:x-y;
}
