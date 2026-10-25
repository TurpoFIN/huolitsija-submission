// Documents helpers: field grouping, value formatting, status sets. Field specs come from GET /api/documents/specs.
import { date, num } from "@/lib/format";

export const LOW_CONFIDENCE = 0.7;
export const MAX_BYTES = 20 * 1024 * 1024;
export const ACCEPT = ".pdf,.png,.jpg,.jpeg,.webp,.csv,.xlsx";
export const READING = ["pending", "processing"];
export const isReading = (status) => READING.includes(status);

export const GROUPS = ["references", "parties", "terms", "transport", "totals", "other"];

const PARTY = /^(seller|buyer|consignee|shipper|sender|exporter)_|^notify_party$/;
const REFERENCES = new Set(["invoice_number", "invoice_date", "packing_list_number", "date", "bl_number", "bl_kind", "awb_number",
  "house_awb_number", "cmr_number", "certificate_number", "document_title", "reference_number", "purchase_order_number",
  "number_of_originals", "place_of_issue", "date_of_issue", "issue_date", "issuing_body", "issuer_name"]);
const TERMS = new Set(["incoterm", "incoterm_place", "currency", "payment_terms", "freight_terms", "country_of_origin", "origin_declaration_text"]);
const TRANSPORT = new Set(["carrier_name", "vessel_name", "voyage_number", "port_of_loading", "port_of_discharge", "place_of_receipt",
  "place_of_delivery", "container_numbers", "seal_numbers", "shipped_on_board_date", "airport_of_departure", "airport_of_destination",
  "flight_numbers", "place_of_taking_over", "date_of_taking_over", "vehicle_plate", "trailer_plate", "handling_information",
  "sender_instructions", "marks_and_numbers"]);
const TOTALS = /(_amount|_kg|_m3)$|^total_packages$|^package_type$/;

export function groupOf(key) {
  if (REFERENCES.has(key)) return "references";
  if (PARTY.test(key)) return "parties";
  if (TERMS.has(key)) return "terms";
  if (TRANSPORT.has(key)) return "transport";
  if (TOTALS.test(key)) return "totals";
  return "other";
}

/** [{group, fields:[spec]}] in spec order, empty groups dropped. */
export function groupFields(specFields = []) {
  const by = Object.fromEntries(GROUPS.map((g) => [g, []]));
  specFields.forEach((f) => by[groupOf(f.key)].push(f));
  return GROUPS.filter((g) => by[g].length).map((g) => ({ group: g, fields: by[g] }));
}

export const isNumeric = (kind) => kind === "number";
const AMOUNT = /(_amount|unit_price|^amount)$/;

/** Display string for a stored value. Missing -> "—". Amounts keep two decimals. */
export function formatValue(kind, value, locale, key = "") {
  if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) return "—";
  if (Array.isArray(value)) return value.join(", ");
  if (kind === "number") {
    if (AMOUNT.test(key)) {
      return new Intl.NumberFormat(locale === "fi" ? "fi-FI" : "en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(value));
    }
    return num(value, locale, 3);
  }
  if (kind === "date" && /^\d{4}-\d{2}-\d{2}$/.test(String(value))) return date(value, locale);
  if (kind === "hs") return String(value).replace(/^(\d{4})(\d{2})?(\d{2})?(\d{2})?$/, (m, a, b, c, d) => [a, b, c, d].filter(Boolean).join(" "));
  return String(value);
}

/** Editable string for an input. */
export function toInput(value) {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

export function fileSize(bytes, locale) {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${num(bytes / 1024, locale, 0)} kB`;
  return `${num(bytes / 1024 / 1024, locale, 1)} MB`;
}

export const pctText = (c, locale) => `${num(c * 100, locale, 0)} %`;

export const isSheet = (doc) => /csv|spreadsheet|excel/.test(doc?.content_type || "") || /\.(csv|xlsx)$/i.test(doc?.filename || "");
export const isImage = (doc) => /^image\//.test(doc?.content_type || "");

/** Values that differ between the edit draft and the stored fields (only these are sent). */
export function changedFields(draft, fields, specFields) {
  const out = {};
  specFields.forEach((f) => {
    if (!(f.key in draft)) return;
    const before = toInput(fields?.[f.key]?.value);
    if (draft[f.key].trim() !== before.trim()) out[f.key] = draft[f.key].trim();
  });
  return out;
}

/** Plural-aware t(): uses "<key>_one" when n is 1 and that key exists. */
export function tn(t, key, n, vars = {}) {
  if (n === 1) {
    const one = t(`${key}_one`, { n, ...vars });
    if (one !== `${key}_one`) return one;
  }
  return t(key, { n, ...vars });
}

/** Server text in the UI language: "<key>_fi" when the UI is Finnish and the server sent it. */
export const localized = (obj, key, locale) => (locale === "fi" && obj?.[`${key}_fi`]) || obj?.[key] || null;

/** "Why this needs a person" from the blocking reader warnings: each message once, as sentences, and the fields that
 *  were not found named in one sentence. The stored reason joins up to three raw messages with "; ", so it repeats
 *  "not found" without the field and doubles punctuation. Falls back to it when there is nothing to build from. */
export function reviewReason(ex, t) {
  const errors = (ex?.warnings || []).filter((w) => w.severity === "error");
  if (ex?.status === "failed" || !errors.length) return ex?.reason || "";
  const isMissing = (w) => w.code === "missing" && w.field;
  const sentences = [...new Set(errors.filter((w) => !isMissing(w)).map((w) => String(w.message || "").trim()).filter(Boolean))]
    .map((s) => s.replace(/[;,:\s]+$/, ""))
    .map((s) => (/[.!?]$/.test(s) ? s : `${s}.`));
  const missing = [...new Set(errors.filter(isMissing).map((w) => t(`doc.field.${w.field}`)))];
  if (missing.length) sentences.push(t("doc.review.reason_missing", { fields: missing.join(", ") }));
  return sentences.join(" ");
}
