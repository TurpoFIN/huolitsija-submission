/**
 * Which tab the shipments list opens on when the URL names none. Needs attention first when anything needs it.
 * Otherwise ops see active work; customers see everything, so a customer whose only shipment is delivered still
 * sees it (s4 D9: an empty "no shipments match" list).
 */
export function defaultTab(counts, { ops, loaded }) {
  if (!loaded) return "needs_action";
  if ((counts?.needs_action || 0) > 0) return "needs_action";
  return ops ? "active" : "all";
}
