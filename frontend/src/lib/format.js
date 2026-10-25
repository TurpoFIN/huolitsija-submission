// Formatting helpers. Money is integer cents (API fields *_cents). Dates are ISO strings in UTC.
const loc = (l) => (l === "fi" ? "fi-FI" : "en-GB");

export function money(cents, locale = "fi", { currency = "EUR", decimals } = {}) {
  if (cents === null || cents === undefined || Number.isNaN(cents)) return "—";
  const v = cents / 100;
  const d = decimals ?? (Math.abs(v) >= 10000 ? 0 : 2);
  return new Intl.NumberFormat(loc(locale), { style: "currency", currency, minimumFractionDigits: d, maximumFractionDigits: d }).format(v);
}

export function num(v, locale = "fi", digits = 0) {
  if (v === null || v === undefined || v === "" || Number.isNaN(Number(v))) return "—";
  return new Intl.NumberFormat(loc(locale), { maximumFractionDigits: digits, minimumFractionDigits: 0 }).format(Number(v));
}

export const kg = (v, l) => (v === null || v === undefined ? "—" : `${num(v, l, v < 10 ? 1 : 0)} kg`);
export const cbm = (v, l) => (v === null || v === undefined ? "—" : `${num(v, l, 2)} m³`);
export const pct = (v, l, d = 1) => (v === null || v === undefined ? "—" : `${num(v, l, d)} %`);

export function date(iso, locale = "fi", opts) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(loc(locale), opts || { day: "numeric", month: "short", year: "numeric" });
}

export function dateShort(iso, locale = "fi") {
  return date(iso, locale, { day: "numeric", month: "short" });
}

export function dateTime(iso, locale = "fi") {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(loc(locale), { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function relTime(iso, locale = "fi") {
  if (!iso) return "—";
  const diff = (new Date(iso).getTime() - Date.now()) / 1000;
  const rtf = new Intl.RelativeTimeFormat(loc(locale), { numeric: "auto" });
  const abs = Math.abs(diff);
  if (abs < 60) return rtf.format(Math.round(diff), "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 86400 * 45) return rtf.format(Math.round(diff / 86400), "day");
  return date(iso, locale);
}

export function daysUntil(iso) {
  if (!iso) return null;
  return Math.ceil((new Date(iso).getTime() - Date.now()) / 86400000);
}
