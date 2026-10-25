// Finance (s13) formatting + small pure helpers. Money is integer cents; EUR is the functional currency.
// Nothing here invents a value: missing input returns null and the UI shows "—".
import { money } from "@/lib/format";

/** EUR cents -> "1 234,56 €" (always two decimals; finance figures are compared to the cent). */
export function eur(cents, locale = "fi") {
  return cents == null ? (locale === "fi" ? "Tieto puuttuu" : "Missing data") : money(cents, locale, { decimals: 2 });
}

/** Cents in any ISO currency -> "1 234,56 $". Unknown currency code falls back to "1 234,56 XYZ". */
export function amount(cents, currency = "EUR", locale = "fi") {
  if (cents === null || cents === undefined || Number.isNaN(Number(cents))) return locale === "fi" ? "Tieto puuttuu" : "Missing data";
  try {
    return money(Number(cents), locale, { currency: currency || "EUR", decimals: 2 });
  } catch (e) {
    return `${money(Number(cents), locale, { decimals: 2 }).replace(/[^\d\s,.\-−]/g, "").trim()} ${currency}`;
  }
}

/** Signed EUR delta: "+120,00 €" / "−35,10 €". */
export function signedEur(cents, locale = "fi") {
  if (cents === null || cents === undefined || Number.isNaN(Number(cents))) return locale === "fi" ? "Tieto puuttuu" : "Missing data";
  const s = eur(Math.abs(cents), locale);
  return cents > 0 ? `+${s}` : cents < 0 ? `−${s}` : s;
}

/** Percent with one decimal: 12.345 -> "12,3 %". */
export function pctText(v, locale = "fi", digits = 1) {
  if (v === null || v === undefined || v === "" || Number.isNaN(Number(v))) return locale === "fi" ? "Tieto puuttuu" : "Missing data";
  return `${new Intl.NumberFormat(locale === "fi" ? "fi-FI" : "en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(v))} %`;
}

/** First value that is not null/undefined/"" (field names that the API may still be settling). */
export function first(...vals) {
  for (const v of vals) if (v !== null && v !== undefined && v !== "") return v;
  return null;
}

/** Safe nested read: at(obj, "a.b.c"). */
export function at(obj, path) {
  return String(path).split(".").reduce((o, k) => (o === null || o === undefined ? undefined : o[k]), obj);
}

/** Number of items in a list payload, or null when the payload is not loaded. */
export const countOf = (d) => (d ? (typeof d.total === "number" ? d.total : (d.items || []).length) : null);

/** Today in Helsinki, YYYY-MM-DD. */
export function todayHelsinki() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Helsinki", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}

/** Current period YYYY-MM in Helsinki. */
export const currentPeriod = () => todayHelsinki().slice(0, 7);

/** Shift a period by n months: shiftPeriod("2026-01", -1) -> "2025-12". */
export function shiftPeriod(period, n) {
  const [y, m] = period.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + n, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** First and last day of a period: periodRange("2026-02") -> ["2026-02-01", "2026-02-28"]. */
export function periodRange(period) {
  const [y, m] = period.split("-").map(Number);
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return [`${period}-01`, `${period}-${String(last).padStart(2, "0")}`];
}

/** "2026-10" -> "lokakuu 2026" / "October 2026". */
export function periodLabel(period, locale = "fi") {
  if (!period || !/^\d{4}-\d{2}$/.test(period)) return period || "—";
  const [y, m] = period.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 15)).toLocaleDateString(locale === "fi" ? "fi-FI" : "en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
}

/** Named ranges for report filters, computed from today's Helsinki date. -> {from, to} */
export function presetRange(preset, today = todayHelsinki()) {
  const period = today.slice(0, 7);
  const y = Number(today.slice(0, 4));
  if (preset === "mtd") return { from: `${period}-01`, to: today };
  if (preset === "last_month") {
    const [from, to] = periodRange(shiftPeriod(period, -1));
    return { from, to };
  }
  if (preset === "qtd") {
    const q = Math.floor((Number(today.slice(5, 7)) - 1) / 3);
    return { from: `${y}-${String(q * 3 + 1).padStart(2, "0")}-01`, to: today };
  }
  if (preset === "ytd") return { from: `${y}-01-01`, to: today };
  return null;
}

/** Localised {fi, en} pair or *_fi / *_en sibling fields. */
export function pairText(obj, base, locale) {
  if (!obj) return null;
  const other = locale === "fi" ? "en" : "fi";
  return first(obj[`${base}_${locale}`], obj[`${base}_${other}`], obj[base]);
}

/** Tone for a variance: over the accrual = cost overrun (danger), under = neutral. */
export function varianceTone(cents) {
  if (cents === null || cents === undefined) return null;
  return cents > 0 ? "text-danger-fg" : null;
}

/** Build a query string, dropping empty values. */
export function qs(params) {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== "") p.set(k, v);
  });
  const s = p.toString();
  return s ? `?${s}` : "";
}
