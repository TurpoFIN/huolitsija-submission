// Display helpers for exceptions. The detection rules store English titles/descriptions
// ("ETA slipped 50 h"); these helpers render them in the UI language from the same facts and fall
// back to the stored text for anything they do not recognise (manual titles written by people).
import { date, dateTime, money, num } from "@/lib/format";
import { tEnum } from "@/lib/i18n";

export const TYPES = ["delay", "customs_hold", "missing_docs", "damage", "address_issue", "capacity", "weather", "payment", "other"];
export const SEVERITIES = ["critical", "high", "medium", "low"];
export const SLA_HOURS = { critical: 2, high: 8, medium: 24, low: 72 };

export const typeLabel = (t, type) => {
  const k = `exceptions.type.${type}`;
  const s = t(k);
  return s === k ? tEnum(t, "x", type) : s;
};

/** "bill_of_lading or sea_waybill" -> "Konossementti tai merirahtikirja" */
export function docLabel(t, name) {
  return String(name)
    .split(/\s+or\s+/)
    .map((p) => {
      const k = `exceptions.doc.${p.trim()}`;
      return t(k) !== k ? t(k) : tEnum(t, "doc", p.trim());
    })
    .join(` ${t("exceptions.or")} `);
}

export const docList = (t, list) => (list || []).map((d) => docLabel(t, d)).join(", ");

/** Compact duration from milliseconds: 45 min · 5 h · 3 pv. */
export function compactDuration(ms, t) {
  const abs = Math.abs(ms);
  const min = abs / 60000;
  if (min < 60) return t("exceptions.dur.min", { n: Math.max(1, Math.round(min)) });
  const h = min / 60;
  if (h < 48) return t("exceptions.dur.h", { n: Math.round(h) });
  return t("exceptions.dur.d", { n: Math.round(h / 24) });
}

export function localTitle(ex, t, locale) {
  const own = ex?.title_i18n && (ex.title_i18n[locale] || null);
  if (own) return own;
  const title = ex?.title || "";
  let m = title.match(/^ETA slipped (\d+) h$/);
  if (m) return t("exceptions.rt.eta", { h: m[1] });
  m = title.match(/^Customs hold (\d+) h$/);
  if (m) return t("exceptions.rt.customs", { h: m[1] });
  m = title.match(/^Missing documents: (.+)$/);
  if (m) return t("exceptions.rt.docs", { list: docList(t, m[1].split(/,\s*/)) });
  m = title.match(/^No position update for (\d+) h$/);
  if (m) return t("exceptions.rt.pos", { h: m[1] });
  m = title.match(/^Invoice (\S+) overdue (\d+) d$/);
  if (m) return t("exceptions.rt.pay", { number: m[1], d: m[2] });
  return title;
}

export function localDescription(ex, t, locale) {
  const own = ex?.description_i18n && (ex.description_i18n[locale] || null);
  if (own) return own;
  const text = (ex?.description || "").trim();
  let m = text.match(/^(.+?): ETA now (\S+) vs planned (\S+) \((.+)\)\.$/);
  if (m) return t("exceptions.rd.eta", { eta: dateTime(m[2], locale), planned: dateTime(m[3], locale) });
  m = text.match(/^(.+?) has been held by customs since (\S+)\.(?: Reason: ([\s\S]+))?$/);
  if (m) return t("exceptions.rd.customs", { since: dateTime(m[2], locale) }) + (m[3] ? ` ${t("exceptions.rd.customs_reason", { reason: m[3] })}` : "");
  m = text.match(/^(.+?) arrives (?:in ([\d.]+) days|now); not on file: (.+)\.$/);
  if (m) {
    const list = docList(t, m[3].split(/,\s*/));
    return m[2] ? t("exceptions.rd.docs_in", { n: num(m[2], locale, 1), list }) : t("exceptions.rd.docs_now", { list });
  }
  m = text.match(/^(.+?): last position (.+?) \(source .+\)\.$/);
  if (m) return m[2] === "never received" ? t("exceptions.rd.pos_never") : t("exceptions.rd.pos", { at: dateTime(m[2], locale) });
  m = text.match(/^(.+?): ([\d.]+) EUR due (\S+)\.$/);
  if (m) return t("exceptions.rd.pay", { customer: m[1], amount: money(Math.round(parseFloat(m[2]) * 100), locale), due: date(m[3], locale) });
  return text;
}

function basisLabel(t, basis) {
  if (!basis) return null;
  if (/planned|original/i.test(basis) && !/milestone/i.test(basis)) return t("exceptions.basis.planned");
  if (/quoted/i.test(basis)) return t("exceptions.basis.quoted");
  if (/first ETA/i.test(basis)) return t("exceptions.basis.first_seen");
  if (/milestone/i.test(basis)) return t("exceptions.basis.milestone");
  return basis;
}

const humanKey = (k) => String(k).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

/** Evidence facts -> KV items with localised labels and formatted values. */
export function evidenceItems(evidence, t, locale, { invoiceLink } = {}) {
  // Nested fact groups (e.g. {rule_eta: {eta, planned_eta, slip_hours}}) are flattened; first value wins.
  const flat = {};
  const walk = (obj) => Object.entries(obj || {}).forEach(([k, v]) => {
    if (v && typeof v === "object" && !Array.isArray(v)) walk(v);
    else if (!(k in flat)) flat[k] = v;
  });
  walk(evidence);
  const out = [];
  Object.entries(flat).forEach(([k, v]) => {
    const labelKey = `exceptions.ev.${k}`;
    const label = t(labelKey) === labelKey ? humanKey(k) : t(labelKey);
    let value = v;
    switch (k) {
      case "slip_hours":
      case "hours":
        value = v === null || v === undefined ? null : `${num(v, locale, 0)} h`;
        break;
      case "eta":
      case "planned_eta":
      case "since":
        value = v ? dateTime(v, locale) : null;
        break;
      case "position_updated_at":
        value = v ? dateTime(v, locale) : t("exceptions.ev.never");
        break;
      case "basis":
        value = basisLabel(t, v);
        break;
      case "missing":
      case "on_file":
        value = Array.isArray(v) ? (v.length ? docList(t, v) : t("exceptions.ev.none")) : v;
        break;
      case "days_overdue":
        value = v === null || v === undefined ? null : t("exceptions.dur.d", { n: v });
        break;
      case "amount_due_cents":
        value = money(v, locale);
        break;
      case "invoice_id":
        value = invoiceLink ? invoiceLink(v) : v;
        break;
      default:
        if (Array.isArray(v)) value = v.join(", ");
        else if (v && typeof v === "object") value = JSON.stringify(v);
        else if (typeof v === "boolean") value = v ? "✓" : "—";
    }
    out.push({ label, value, className: k === "missing" && Array.isArray(v) && v.length ? "text-danger-fg" : undefined });
  });
  return out;
}

/** History rows -> Timeline events (newest last, as stored). */
export function historyEvents(history, t) {
  return (history || []).map((h, i) => {
    const a = String(h.action || "");
    let title = a;
    let m;
    if (a === "opened") title = t("exceptions.hist.opened");
    else if (a === "resolved") title = t("exceptions.hist.resolved");
    else if (a === "escalated") title = t("exceptions.hist.escalated");
    else if (a === "unassigned") title = t("exceptions.hist.unassigned");
    else if (a === "note") title = t("exceptions.hist.note");
    else if ((m = a.match(/^severity -> (\w+)$/))) title = t("exceptions.hist.severity", { v: tEnum(t, "severity", m[1]) });
    else if ((m = a.match(/^status -> (\w+)$/))) title = t("exceptions.hist.status", { v: tEnum(t, "exception", m[1]) });
    else if ((m = a.match(/^owner -> (.+)$/))) title = t("exceptions.hist.owner", { v: m[1] });
    let note = h.note || "";
    const esc = note.match(/^(\w+) -> (\w+)$/);
    if (esc) note = `${tEnum(t, "severity", esc[1])} → ${tEnum(t, "severity", esc[2])}`;
    const by = ["rule", "carrier", "ops", "partner", "ai"].includes(h.by) ? t(`exceptions.source.${h.by}`) : h.by || t("exceptions.hist.by_system");
    return { id: `${h.at}-${i}`, at: h.at, title, detail: note || null, source: by, tone: a === "escalated" ? "warning" : undefined };
  });
}

const CLEARED = {
  "ETA back within tolerance": "exceptions.cleared.eta",
  "Customs hold released": "exceptions.cleared.customs",
  "Required documents on file": "exceptions.cleared.docs",
  "Position updates resumed": "exceptions.cleared.pos",
  "Condition cleared": "exceptions.cleared.other",
};

/** "Cleared automatically: ETA back within tolerance." -> localised; people's own text unchanged. */
export function localResolution(text, t) {
  const s = (text || "").trim();
  const m = s.match(/^Cleared automatically: (.+?)\.?$/);
  if (!m) return s;
  const why = m[1];
  let label = CLEARED[why] ? t(CLEARED[why]) : null;
  let mm;
  if (!label && (mm = why.match(/^Invoice is (\w+)$/))) label = t("exceptions.cleared.invoice", { v: tEnum(t, "invoice", mm[1]) });
  if (!label && (mm = why.match(/^Shipment (\w+)$/))) label = t("exceptions.cleared.shipment", { v: tEnum(t, "status", mm[1]) });
  return t("exceptions.cleared", { why: label || why });
}

export const resolverName = (rb, t) => (!rb ? null : rb.type === "rule" ? t("exceptions.hist.by_rule") : rb.name);
