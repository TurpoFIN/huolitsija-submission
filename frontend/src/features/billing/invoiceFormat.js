// Billing formatting + small pure helpers shared by the portal and ops invoice screens.
import { money } from "@/lib/format";

export const OPEN_STATES = ["open", "overdue", "processing"];
export const PAYABLE_STATES = ["open", "overdue"];

/** Invoice money: always two decimals ("4 871,65 €"); credit notes stay negative. */
export function eur(cents, locale = "fi") {
  return money(cents, locale, { decimals: 2 });
}

/** Date-only ISO ("2026-11-07") -> "7.11.2026" (fi) / "7 Nov 2026" (en), without timezone drift. */
export function isoDate(iso, locale = "fi") {
  if (!iso) return "—";
  const s = String(iso);
  const d = /^\d{4}-\d{2}-\d{2}$/.test(s) ? new Date(`${s}T00:00:00Z`) : new Date(s);
  if (Number.isNaN(d.getTime())) return "—";
  const tz = /^\d{4}-\d{2}-\d{2}$/.test(s) ? "UTC" : "Europe/Helsinki";
  return locale === "fi"
    ? d.toLocaleDateString("fi-FI", { day: "numeric", month: "numeric", year: "numeric", timeZone: tz })
    : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: tz });
}

/** Today in Helsinki as YYYY-MM-DD (the backend's business day). */
export function todayIso() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Helsinki", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}

/** Due-date status from the API's own day counts. -> {text, tone} or null when nothing is due. */
export function dueInfo(t, inv) {
  if (!inv || inv.kind === "credit_note" || !OPEN_STATES.includes(inv.status) || !inv.due_date) return null;
  if (inv.days_overdue > 0) return { text: t("billing.due.overdue_n", { n: inv.days_overdue }), tone: "warning" };
  if (inv.days_until_due === 0) return { text: t("billing.due.today"), tone: "warning" };
  if (inv.days_until_due > 0) return { text: t("billing.due.in_n", { n: inv.days_until_due }), tone: null };
  return null;
}

/** {fi, en} text in the user's locale. */
export function localeText(obj, locale) {
  if (!obj) return null;
  return obj[locale] || obj.fi || obj.en || null;
}

export function vatLabel(row, locale) {
  if (!row) return "—";
  return (locale === "fi" ? row.label_fi : row.label_en) || row.label_fi || row.label_en || row.vat_code || row.code || "—";
}

export const PENDING_REVIEW = "pending_tax_adviser_review";

export function hasPendingLegalBasis(inv) {
  if (!inv) return false;
  if (inv.legal_basis_pending) return true;
  return (inv.vat_breakdown || []).some((b) => b.legal_basis?.status === PENDING_REVIEW)
    || (inv.lines || []).some((l) => l.legal_basis?.status === PENDING_REVIEW);
}

/** "1 234,50" / "1234.50" / "-12" -> cents (int) or null when not a valid amount. */
export function parseEuro(input) {
  if (input === null || input === undefined) return null;
  const s = String(input).replace(/[\s  €]/g, "").replace(",", ".");
  if (!/^-?\d+(\.\d{0,2})?$/.test(s)) return null;
  const v = Number(s);
  if (!Number.isFinite(v)) return null;
  return Math.round(v * 100);
}

/** Quantity input with comma or dot decimals -> number or null. */
export function parseQty(input) {
  if (input === null || input === undefined || input === "") return null;
  const s = String(input).replace(/[\s ]/g, "").replace(",", ".");
  if (!/^\d+(\.\d+)?$/.test(s)) return null;
  const v = Number(s);
  return Number.isFinite(v) ? v : null;
}

/** cents -> editable amount string ("1234,50" in Finnish). */
export function centsToInput(cents, locale = "fi") {
  if (cents === null || cents === undefined) return "";
  const neg = cents < 0;
  const abs = Math.abs(cents);
  const s = `${Math.floor(abs / 100)}${locale === "fi" ? "," : "."}${String(abs % 100).padStart(2, "0")}`;
  return neg ? `-${s}` : s;
}

export function qtyToInput(q, locale = "fi") {
  if (q === null || q === undefined) return "";
  const s = String(q);
  return locale === "fi" ? s.replace(".", ",") : s;
}

function roundHalfUp(x) {
  return Math.sign(x) * Math.round(Math.abs(x));
}

/**
 * Client-side preview of totals, mirroring the backend (VAT per code on the summed base, half-up).
 * lines: [{qty, unit_price_cents, vat_code ("" = decided on save), is_pass_through}]
 * vatCodes: GET /invoices/vat-codes items (today's rates).
 */
export function previewTotals(lines, vatCodes) {
  const byCode = {};
  (vatCodes || []).forEach((v) => { byCode[v.code] = v; });
  const groups = {};
  let subtotal = 0;
  let passThrough = 0;
  let autoBase = 0;
  let autoCount = 0;
  lines.forEach((l) => {
    if (l.amount_cents === null || l.amount_cents === undefined) return;
    subtotal += l.amount_cents;
    if (l.is_pass_through) passThrough += l.amount_cents;
    const code = l.is_pass_through ? "OUT_OF_SCOPE_DISBURSEMENT" : l.vat_code;
    if (!code || !byCode[code]) {
      autoBase += l.amount_cents;
      autoCount += 1;
      return;
    }
    groups[code] = (groups[code] || 0) + l.amount_cents;
  });
  const breakdown = Object.entries(groups).map(([code, base]) => {
    const spec = byCode[code];
    return { vat_code: code, rate_pct: spec.rate_pct, base_cents: base, vat_cents: roundHalfUp((base * spec.rate_pct) / 100),
      kind: spec.kind, label_fi: spec.label_fi, label_en: spec.label_en, legal_basis: spec.legal_basis };
  });
  const vat = breakdown.reduce((a, b) => a + b.vat_cents, 0);
  return { subtotal_cents: subtotal, services_net_cents: subtotal - passThrough, pass_through_cents: passThrough,
    vat_cents: vat, total_cents: subtotal + vat, vat_breakdown: breakdown, auto_base_cents: autoBase, auto_count: autoCount };
}

/** Amount the "mark paid" dialog proposes: amount due, or the refund owed on a credit note. */
export function settleAmount(inv) {
  if (!inv) return 0;
  if (inv.kind === "credit_note") return Math.max(0, Number(inv.refund_due_cents || 0) - Number(inv.paid_cents || 0));
  return inv.amount_due_cents || 0;
}

export function invoiceTitle(t, inv) {
  if (!inv) return "";
  if (!inv.number) return inv.kind === "credit_note" ? t("billing.credit_note_draft") : t("billing.draft_title");
  return inv.kind === "credit_note" ? t("billing.credit_note_n", { n: inv.number }) : t("billing.invoice_n", { n: inv.number });
}
