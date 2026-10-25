// Backend tiles and report rows carry a UI `path`. Map the few that point at a different route shape in the app.
const FALLBACK = {
  carrier_invoices_attention: "/ops/finance/carrier-invoices?status=needs_review,disputed",
  payables_due_7d: "/ops/finance/payables",
  collections_open: "/ops/finance/collections",
  ar_overdue: "/ops/billing?tab=overdue",
  vat_current: "/ops/finance/vat",
  gross_profit_mtd: "/ops/finance/pnl",
  open_accruals: "/ops/finance/accruals",
  fx: "/ops/finance/fx",
  ledger_last_export: "/ops/finance/ledger",
  autopilot: "/ops/settings",
};

export function localLink(path, key) {
  if (!path || typeof path !== "string" || !path.startsWith("/")) return FALLBACK[key] || "/ops/finance";
  if (path.startsWith("/ops/finance/reports/pnl")) return path.replace("/ops/finance/reports/pnl", "/ops/finance/pnl");
  if (path.startsWith("/ops/billing?status=")) return path.replace("?status=", "?tab=");
  return path;
}
