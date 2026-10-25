// Pure helpers for the TARIC lookup UI (no React): grouping measures, summary line, request validation.
// The backend (services/taric.py, live EU TARIC consultation) owns the data; nothing here invents a rate.

export const GROUP_ORDER = ["duty", "preferences", "trade_defence", "restrictions", "other"];

// services/taric.py _kind() -> UI group.
const KIND_GROUP = {
  third_country: "duty", suspension: "duty", additional_duty: "duty", low_value_consignment: "duty",
  reference_price: "duty", supplementary_unit: "other",
  preference: "preferences", quota: "preferences",
  trade_defence: "trade_defence",
  restriction: "restrictions", prohibition: "restrictions",
  other: "other",
};

/** Measure group: the backend's kind first, then the TARIC measure type code. */
export function measureGroup(m) {
  if (m?.kind && KIND_GROUP[m.kind]) return KIND_GROUP[m.kind];
  const c = String(typeCode(m) || "").trim();
  if (!/^\d{3}$/.test(c) && !/^[A-Z0-9]{3}$/.test(c)) return "other";
  if (/^14\d$/.test(c)) return "preferences";
  if (/^5[5-7]\d$/.test(c) || /^69[56]$/.test(c)) return "trade_defence";
  if (/^1\d\d$/.test(c)) return "duty";
  if (/^[247]\d\d$/.test(c) || /^[A-Z]/.test(c)) return "restrictions";
  return "other";
}

/** The measure type code, or its candidates "142/145" when the description maps to several codes. */
export function typeCode(m) {
  if (!m) return "";
  if (m.type_code) return m.type_code;
  const c = m.type_code_candidates || [];
  return c.length ? c.join("/") : "";
}

export function groupMeasures(measures) {
  const out = Object.fromEntries(GROUP_ORDER.map((g) => [g, []]));
  (measures || []).forEach((m) => out[measureGroup(m)].push(m));
  return GROUP_ORDER.filter((g) => out[g].length).map((g) => ({ key: g, measures: out[g] }));
}

/** Duty text exactly as TARIC states it; falls back to the parsed duty's raw text. Never computes a rate on its own. */
export function dutyText(m) {
  if (!m) return "";
  if (m.duty_expression) return String(m.duty_expression).trim();
  const d = m.duty;
  if (!d) return "";
  if (typeof d === "string") return d;
  if (d.raw) return String(d.raw).trim();
  if (d.pct !== null && d.pct !== undefined && (d.amount === null || d.amount === undefined)) return `${d.pct} %`;
  return "";
}

const pctOf = (m) => (m?.duty && m.duty.kind === "ad_valorem" && m.duty.pct !== null && m.duty.pct !== undefined ? m.duty.pct : null);

/** Lowest ad-valorem preference; the backend's preferences[] is already limited to the country. */
export function bestPreference(prefs) {
  const list = (prefs || []).filter(Boolean);
  if (!list.length) return null;
  const withPct = list.filter((m) => pctOf(m) !== null);
  if (!withPct.length) return list[0];
  return withPct.reduce((a, b) => (pctOf(b) < pctOf(a) ? b : a));
}

export function thirdCountry(resp) {
  const t = resp?.third_country_duty;
  if (t && typeof t === "object") return t;
  if (typeof t === "string" && t) return { duty_expression: t };
  return null;
}

/** Validates the lookup form. Returns {ok, code, country, errors{code?, country?}}. */
export function validateLookup({ code, country }) {
  const c = String(code || "").replace(/[\s.]/g, "");
  const k = String(country || "").trim().toUpperCase();
  const errors = {};
  if (!/^(\d{4}|\d{6}|\d{8}|\d{10})$/.test(c)) errors.code = "cx.taric.err.code";
  if (k && !/^[A-Z]{2}$/.test(k)) errors.country = "cx.taric.err.country";
  return { ok: !Object.keys(errors).length, code: c, country: k, errors };
}

/** Link to the legal act, only when the backend gives one (no guessed CELEX numbers). */
export function legalUrl(m) {
  return m?.regulation_url || m?.legal_url || m?.legal_base_url || null;
}

// services/taric.py quota(): open | exhausted | critical | blocked | suspended | no_period -> workflow state.
export const QUOTA_STATE = { open: "success", critical: "pending", exhausted: "failed", blocked: "failed", suspended: "expired", no_period: "expired" };
