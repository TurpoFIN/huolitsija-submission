// Shared helpers for the Autopilot control room (/ops/autopilot) and the owner queue (/ops/approvals).
import { legalBasisFi } from "./legalBasisFi";

/** The locale's variant of a bilingual pair from the API ({fi, en} or *_fi / plain fields). */
export function pick(locale, fi, en) {
  if (locale === "fi") return fi || en || "";
  return en || fi || "";
}

/** Backend messages (English, from {detail}) the control room can produce, mapped to dictionary keys. */
const ERRORS = {
  "Type enable to enable autonomous actions": "ap.err.type_enable",
  "Give the name and title of the person who grants the delegation": "ap.err.signer",
  "Scope must be direct or direct_and_indirect": "ap.err.scope",
  "Limit must be a whole number of cents from 0 to 1 000 000 EUR": "ap.err.cents",
  "Confidence floor must be between 0.5 and 1": "ap.err.confidence",
  "Hours and days must be whole numbers from 0 to 365": "ap.err.days",
  "Response time and reminder interval must be whole hours from 1 to 365": "ap.err.owner_hours",
  "Dunning steps must come in order: reminder, second reminder, final notice, collection": "ap.err.dunning_order",
  "Enter a valid e-mail address": "ap.err.email",
  "Margin must be at least 0 and below 100 percent": "ap.err.margin",
  "Price limit must be a positive whole number of cents": "ap.err.price",
  "Write a short reason": "ap.err.reason",
  "This dead letter was already retried or dismissed": "ap.err.dl_done",
  "Only an open dead letter can be dismissed": "ap.err.dl_done",
  "Schedule not found": "ap.err.schedule",
  "Alert not found": "ap.err.alert",
  "Missing permission: settings.write": "ap.err.permission",
};

/** One readable sentence for a failed request, in the UI language. */
export function apError(e, t) {
  const detail = typeof e?.data?.detail === "string" ? e.data.detail : null;
  if (detail && ERRORS[detail]) return t(ERRORS[detail]);
  if (e?.status === 403) return t("ap.err.permission");
  return e?.message || t("state.error");
}

/** Euros as typed -> cents. Finnish "1 234,50" (comma or dot decimal), English "1,234.50". Throws "ap.err.eur" (a
 *  dictionary key) when the text is not an amount. */
export function eurToCents(text, locale = "fi") {
  let raw = String(text ?? "").replace(/[\s  €]/g, "");
  raw = locale === "en" ? raw.replace(/,/g, "") : raw.replace(",", ".");
  if (!/^\d+(\.\d{1,2})?$/.test(raw)) throw new Error("ap.err.eur");
  return Math.round(Number(raw) * 100);
}

/** Cents -> editable EUR text: "10 000" / "1 500,50" in Finnish, "10,000" / "1,500.50" in English when grouped;
 *  eurToCents reads either back. */
export function centsToEur(cents, locale, grouped = false) {
  if (cents === null || cents === undefined) return "";
  const v = cents / 100;
  const [int, dec] = (Number.isInteger(v) ? String(v) : v.toFixed(2)).split(".");
  const g = grouped ? int.replace(/\B(?=(\d{3})+(?!\d))/g, locale === "en" ? "," : " ") : int;
  return dec ? `${g}${locale === "en" ? "." : ","}${dec}` : g;
}

export function decimalText(v, locale) {
  if (v === null || v === undefined) return "";
  const s = String(v);
  return locale === "fi" ? s.replace(".", ",") : s;
}

export function parseDecimal(text) {
  const raw = String(text ?? "").trim().replace(",", ".");
  if (!/^\d+(\.\d+)?$/.test(raw)) throw new Error("ap.err.number");
  return Number(raw);
}

export function parseWhole(text) {
  const raw = String(text ?? "").trim();
  if (!/^\d+$/.test(raw)) throw new Error("ap.err.whole");
  return Number(raw);
}

// Visual state per domain value. Only the nine documented workflow states are used.
export const OUTCOME_STATE = { auto: "success", owner_queue: "in-review", blocked: "failed" };
export const JOB_STATE = { queued: "scheduled", running: "in-progress", retry: "pending", done: "success", dead: "failed" };
export const HEALTH_STATE = { ok: "success", warn: "pending", fail: "failed" };
export const SEVERITY_STATE = { info: "in-progress", warning: "pending", critical: "failed" };
export const DEAD_LETTER_STATE = { open: "failed", retried: "in-progress", dismissed: "expired" };

/** Agent display name: the API's own label pair, else the dictionary, else the code. */
export function agentName(t, locale, code, labelFi, labelEn) {
  if (labelFi || labelEn) return pick(locale, labelFi, labelEn);
  const key = `ap.agent.${code}`;
  const s = t(key);
  return s === key ? code || "—" : s;
}

/** Legal basis in the UI language. The Finnish text applies only to the exact citation it was written for. */
export function legalBasis(locale, basis) {
  if (!basis) return "";
  return locale === "fi" ? legalBasisFi[basis] || basis : basis;
}

/** Whole hours/minutes until (positive) or since (negative) an ISO time. */
export function hoursFrom(iso, nowMs = Date.now()) {
  if (!iso) return null;
  const ms = new Date(iso).getTime();
  if (Number.isNaN(ms)) return null;
  return (ms - nowMs) / 3600000;
}

/** "45 min" / "3 h" / "2 pv" for a duration in hours (absolute value). */
export function durationText(t, hours) {
  const h = Math.abs(hours);
  if (h < 1) return t("ap.dur.min", { n: Math.max(1, Math.round(h * 60)) });
  if (h < 48) return t("ap.dur.h", { n: Math.round(h) });
  return t("ap.dur.d", { n: Math.round(h / 24) });
}

/** SLA chip for an owner-queue card: {state|null, label}. null state = neutral (plenty of time). */
export function slaChip(t, a, nowMs = Date.now()) {
  const left = hoursFrom(a?.sla_due_at, nowMs);
  if (left === null) return null;
  if (left < 0) return { state: "failed", label: t("ap.sla.late", { time: durationText(t, left) }) };
  const total = Number(a.sla_hours) || 24;
  const tight = left < Math.max(1, total * 0.25);
  return { state: tight ? "pending" : null, label: t("ap.sla.left", { time: durationText(t, left) }) };
}

/** Schedule cadence as text: "every 60 s" / "daily at 07:00". */
export function cadence(t, s) {
  if (s.daily_at) return t("ap.sched.daily", { time: s.daily_at });
  const sec = Number(s.every_seconds) || 0;
  if (sec && sec % 3600 === 0) return t("ap.sched.every_h", { n: sec / 3600 });
  if (sec && sec % 60 === 0) return t("ap.sched.every_min", { n: sec / 60 });
  return t("ap.sched.every_s", { n: sec });
}
