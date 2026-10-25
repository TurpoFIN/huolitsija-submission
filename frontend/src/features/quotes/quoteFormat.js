/**
 * Quote formatting helpers shared by portal, public site and ops (V2).
 *
 * cargoSummary(cargo, t, locale)        -> "2 × EUR pallet · 600 kg · 1,4 m³ · DG UN1263" ("—" when empty)
 * locName(loc)                          -> "Helsinki" (name | label | query | "—")
 * locCode(loc)                          -> "FIHEL" | ""
 * locShort(loc)                         -> loc.city || name ("Tallinn" for "Tallinn Old City Harbour")
 * routeLabel(origin, destination, {short}) -> "Shanghai → Helsinki" (short: city names)
 * routeCodes(origin, destination)       -> "CNSHA → FIHEL" (falls back to names)
 * legsLabel(legs)                       -> "CNSHA → DEHAM → FIHEL"
 * modeIcon(mode)                        -> lucide component (Ship, Truck, Plane, TrainFront; Route for any/multimodal)
 * warningText(t, warning)               -> translated `quotes.warn.<code>` with {params}, else warning.message
 * vatLabel(t, code, ratePct?, locale?)  -> "0 % intl." | "25,5 %" | "Outside VAT" …
 * liabilityLabel(t, liabilityOrRegime, locale?) -> backend label_fi / label when present, else "CMR · enintään 8,33 SDR/kg"
 * chargeableText(t, chargeable, locale) -> "4 440 kg" | "1 × 20GP" | "3,2 W/M" (else backend label)
 * otherPartyInfo(option)                -> {partial, otherCents}: quote covers only the customer's share under the Incoterm
 * lineText(line, locale)                 -> description_fi in Finnish UI, else description
 * reasonText(t, notIncluded, incoterm, locale) -> reason_fi (FI) | quotes.ni.<reason_code> | reason
 * eur(cents, locale)                    -> money with 2 decimals always (prices are never rounded in a quote)
 * validityDays(iso) / validityLabel(t, iso) -> days left (date-only strings count to the end of that day) / "valid 5 d"
 * countryName(code, locale)             -> "Finland" / "Suomi"
 * Constants: MODES, PACKAGE_TYPES, CONTAINER_TYPES, INCOTERMS, SEA_ONLY_INCOTERMS, DG_CLASSES, PACKING_GROUPS, STAGES, INSURANCE_COVERS
 */
import { Plane, Route, Ship, TrainFront, Truck } from "lucide-react";
import { daysUntil, money, num } from "@/lib/format";
import { currentLocale } from "@/lib/localizedData";

export const MODES = ["sea", "road", "air", "rail"];

/** An option priced from an indicative market rate card, not a carrier contract (backend services.pricing.label_indicative). */
export function isIndicative(o) {
  return o?.price_status === "indicative" || (o?.rate_source === "indicative" && o?.price_basis !== "contract");
}
export const PACKAGE_TYPES = ["pallet_eur", "pallet_fin", "carton", "crate", "other"];
export const CONTAINER_TYPES = ["20GP", "40GP", "40HC", "45HC", "20RF", "40RF"];
// Incoterms 2020, rules for any mode first, then sea and inland waterway rules.
export const INCOTERMS = ["EXW", "FCA", "CPT", "CIP", "DAP", "DPU", "DDP", "FAS", "FOB", "CFR", "CIF"];
export const SEA_ONLY_INCOTERMS = ["FAS", "FOB", "CFR", "CIF"];
export const DG_CLASSES = ["1", "2.1", "2.2", "2.3", "3", "4.1", "4.2", "4.3", "5.1", "5.2", "6.1", "6.2", "7", "8", "9"];
export const PACKING_GROUPS = ["I", "II", "III"];
export const STAGES = ["origin", "export_customs", "main", "destination", "import_customs", "customs", "insurance", "fees", "pass_through"];
export const INSURANCE_COVERS = ["NONE", "ICC_A", "ICC_B", "ICC_C"];

const MODE_ICONS = { sea: Ship, road: Truck, air: Plane, rail: TrainFront };
export function modeIcon(mode) {
  return MODE_ICONS[mode] || Route;
}

export function eur(cents, locale = "fi") {
  return money(cents, locale, { decimals: 2 });
}

export function locName(loc) {
  if (!loc) return "—";
  return loc.name || loc.label || loc.query || "—";
}

export function locCode(loc) {
  return (loc && loc.unlocode) || "";
}

/** City when the location carries one (lists, summaries), else the name. */
export function locShort(loc) {
  return (loc && loc.city) || locName(loc);
}

export function routeLabel(origin, destination, { short = false } = {}) {
  return short ? `${locShort(origin)} → ${locShort(destination)}` : `${locName(origin)} → ${locName(destination)}`;
}

export function routeCodes(origin, destination) {
  return `${locCode(origin) || locName(origin)} → ${locCode(destination) || locName(destination)}`;
}

export function legsLabel(legs) {
  if (!Array.isArray(legs) || legs.length === 0) return "";
  const sorted = [...legs].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  const pts = [];
  const push = (loc) => {
    const c = locCode(loc) || (loc ? locName(loc) : "");
    if (c && pts[pts.length - 1] !== c) pts.push(c);
  };
  sorted.forEach((l, i) => {
    if (i === 0) push(l.from);
    push(l.to);
  });
  return pts.join(" → ");
}

export function countryName(code, locale = "fi") {
  if (!code) return "";
  try {
    return new Intl.DisplayNames([locale === "fi" ? "fi" : "en"], { type: "region" }).of(String(code).toUpperCase()) || code;
  } catch (e) {
    return code;
  }
}

const isBlank = (v) => v === null || v === undefined || v === "";

/** One-line cargo summary. Unknown values are left out, never shown as 0. */
export function cargoSummary(cargo, t, locale = "fi") {
  if (!cargo) return "—";
  const parts = [];
  if (cargo.load_type === "FCL" && Array.isArray(cargo.containers) && cargo.containers.length) {
    parts.push(cargo.containers.filter((c) => c && c.type).map((c) => `${c.count ?? 1} × ${c.type}`).join(", "));
  } else if (!isBlank(cargo.packages)) {
    const pt = cargo.package_type ? t(`quotes.pkg.short.${cargo.package_type}`) : t("quotes.pkg.short.other");
    parts.push(`${num(cargo.packages, locale)} × ${pt}`);
  }
  if (!isBlank(cargo.weight_kg)) parts.push(`${num(cargo.weight_kg, locale, cargo.weight_kg < 10 ? 1 : 0)} kg`);
  if (!isBlank(cargo.volume_m3)) parts.push(`${num(cargo.volume_m3, locale, 2)} m³`);
  if (cargo.dangerous_goods) parts.push(cargo.dg?.un_number ? `${t("quotes.dg.short")} ${cargo.dg.un_number}` : t("quotes.dg.short"));
  return parts.length ? parts.join(" · ") : "—";
}

function fmtParam(v) {
  if (Array.isArray(v)) return v.map((x) => (typeof x === "object" && x ? x.type ? `${x.count ?? 1} × ${x.type}` : JSON.stringify(x) : x)).join(", ");
  if (v && typeof v === "object") return JSON.stringify(v);
  return v ?? "";
}

/** A warning's text: the UI key quotes.warn.<code> when there is one, else the backend's Finnish variant (message_fi)
 *  in Finnish, else its message. */
export function warningText(t, w, locale) {
  if (!w) return "";
  const key = `quotes.warn.${w.code}`;
  const vars = {};
  Object.entries(w.params || {}).forEach(([k, v]) => (vars[k] = fmtParam(v)));
  const s = t(key, vars);
  if (s === key || /\{[a-z_]+\}/i.test(s)) return ((locale || currentLocale()) === "fi" && w.message_fi) || w.message || w.code;
  return s;
}

const VAT_KEYS = ["FI_25_5", "FI_13_5", "FI_10", "FI_0", "ZERO_EXPORT", "ZERO_INTL_TRANSPORT", "EU_ICS_RC", "REVERSE_CHARGE",
  "OUT_OF_SCOPE_DISBURSEMENT", "OUT_OF_SCOPE_FOREIGN", "EXEMPT"];
export function vatLabel(t, code, ratePct, locale = "fi") {
  if (!code) return ratePct !== null && ratePct !== undefined ? `${num(ratePct, locale, 1)} %` : "—";
  if (VAT_KEYS.includes(code)) return t(`quotes.vat.${code}`);
  const m = /^FI_(\d+)(?:_(\d+))?$/.exec(code);
  if (m) return `${num(Number(`${m[1]}.${m[2] || 0}`), locale, 1)} %`;
  if (ratePct !== null && ratePct !== undefined) return `${num(ratePct, locale, 1)} %`;
  return code;
}

export function liabilityLabel(t, liability, locale = "fi") {
  if (!liability) return "—";
  // the backend's own sentence is the most exact (e.g. "667 SDR per package or 2 SDR/kg"); FI only when label_fi exists
  if (typeof liability === "object") {
    if (locale === "fi" && liability.label_fi) return liability.label_fi;
    if (locale !== "fi" && liability.label) return liability.label;
  }
  const regime = typeof liability === "string" ? liability : liability.regime;
  const key = `quotes.liability.${regime}`;
  let s = regime ? t(key) : "";
  if (!regime || s === key) s = (typeof liability === "object" && liability.label) || regime || "—";
  const limit = typeof liability === "object" ? liability.limit_sdr_per_kg : null;
  if (limit !== null && limit !== undefined) s += ` · ${t("quotes.liability.limit", { limit: num(limit, locale, 2) })}`;
  return s;
}

function validityEnd(iso) {
  if (!iso) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d, 23, 59, 59);
  }
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Calendar days left (0 = last day today, -1 = expired). A date-only string is valid through the end of that day. */
export function validityDays(iso) {
  const end = validityEnd(iso);
  if (!end) return null;
  const now = new Date();
  if (end.getTime() < now.getTime()) return -1;
  const a = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const b = new Date(end.getFullYear(), end.getMonth(), end.getDate()).getTime();
  return Math.round((b - a) / 86400000);
}

export function isExpired(iso) {
  const d = validityDays(iso);
  return d !== null && d < 0;
}

export function validityLabel(t, iso) {
  if (!iso) return "—";
  if (isExpired(iso)) return t("quotes.validity.expired");
  const d = validityDays(iso);
  if (d === null) return "—";
  if (d <= 0) return t("quotes.validity.today");
  return t("quotes.validity.days", { n: d });
}

/** Chargeable quantity in the UI language from option.chargeable fields; falls back to the backend label. */
export function chargeableText(t, ch, locale = "fi") {
  if (!ch) return "";
  const n = (v, d = 0) => num(v, locale, d);
  if (ch.basis === "PER_KG" && ch.chargeable_kg) return `${n(ch.chargeable_kg)} kg`;
  if (ch.basis === "PER_WM" && ch.wm) return `${n(ch.wm, 2)} W/M`;
  if (ch.basis === "PER_LDM" && ch.ldm) return `${n(ch.ldm, 1)} ${t("quotes.unit.PER_LDM")}`;
  if (ch.basis === "PER_CONTAINER" && Array.isArray(ch.containers) && ch.containers.length) {
    return ch.containers.map((c) => `${c.count ?? 1} × ${c.type}`).join(", ");
  }
  return ch.label || "";
}

/**
 * Under C/D terms (or E/F for a seller) the quote covers only the customer's share; the other party's costs sit in
 * not_included with reason_code incoterm.seller|buyer. Returns {partial, otherCents} so the UI never reads it as a cheap price.
 */
export function otherPartyInfo(option) {
  if (!option) return { partial: false, otherCents: null };
  const partial = (option.warnings || []).some((w) => w.code === "incoterm.other_party_main");
  const other = (option.not_included || []).filter((n) => /^incoterm\./.test(n.reason_code || "") && n.amount_cents !== null && n.amount_cents !== undefined);
  return { partial, otherCents: other.length ? other.reduce((a, n) => a + n.amount_cents, 0) : null };
}

/** Line / not-included description in the UI language (backend sends description + description_fi). */
export function lineText(line, locale = "fi") {
  if (!line) return "";
  return (locale === "fi" && line.description_fi) || line.description || line.code || "";
}

/** Reason for a not-included item: translated by reason_code (quotes.ni.<code>), else the backend's English reason. */
export function reasonText(t, item, incoterm, locale = "fi") {
  if (!item) return "";
  if (locale === "fi" && item.reason_fi) return item.reason_fi;
  if (item.reason_code) {
    const k = `quotes.ni.${item.reason_code}`;
    const s = t(k, { incoterm: incoterm || "" });
    if (s !== k) return s;
  }
  return item.reason || "";
}

/** Normalise a location record from /api/locations (V1) or /api/public/locations. */
export function normalizeLocation(l) {
  if (!l) return null;
  const name = l.name || l.label || l.query || "";
  const unlocode = l.unlocode || l.locode || null;
  return {
    ...l,
    unlocode,
    name,
    country: l.country || (unlocode ? unlocode.slice(0, 2) : ""),
    kinds: Array.isArray(l.kinds) ? l.kinds : [],
    label: l.label || [name + (unlocode ? ` (${unlocode})` : ""), l.country].filter(Boolean).join(", "),
  };
}

/** Empty Cargo object (contract shape). Numbers start as null: empty is not zero; stackable starts false (the customer ticks it). */
export function emptyCargo() {
  return {
    load_type: "loose", description: "", packages: null, package_type: "pallet_eur", weight_kg: null, volume_m3: null,
    stackable: false, // conservative: non-stackable is never cheaper than reality containers: [], dangerous_goods: false, dg: null, value_cents: null,
  };
}
