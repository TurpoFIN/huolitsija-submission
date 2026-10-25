// Display helpers for claims. The backend states the cap basis and notes as English sentences
// (e.g. "CMR art. 23: 8.33 SDR × 640 kg gross"); these helpers render known patterns in the UI language
// and keep anything unrecognised as stored.
import { num } from "@/lib/format";

export const CLAIM_TYPES = ["damage", "loss", "delay"];

export const typeLabel = (t, type) => {
  const k = `claims.type.${type}`;
  return t(k) === k ? type || "—" : t(k);
};

function src(t, s) {
  return /^Montreal Convention art\. 22\(3\), ICAO-revised limit$/.test(s) ? t("claims.src.montreal") : s;
}

export function localBasis(basis, t, locale) {
  const b = (basis || "").trim();
  if (!b) return "";
  let m;
  if (/^PSYM 2015 § 23 \(intermediary\)/.test(b)) return t("claims.basis.intermediary");
  if (/^CIM art\. 33: delay limited to four times the carriage charge$/.test(b)) return t("claims.basis.cim_delay");
  if ((m = b.match(/^(.+): delay limited to the freight charged$/))) return t("claims.basis.delay_freight", { src: src(t, m[1]) });
  if ((m = b.match(/^(.+): ([\d.]+) SDR\/kg also covers delay$/))) return t("claims.basis.delay_per_kg", { src: src(t, m[1]), rate: num(m[2], locale, 2) });
  if ((m = b.match(/^(.+) has no delay liability limit for goods; assessed under the contract$/))) return t("claims.basis.delay_none", { label: m[1] });
  if ((m = b.match(/^(.+): max\(([\d.]+) SDR × (\d+) packages, ([\d.]+) SDR × ([\d.]+) kg\)$/)))
    return t("claims.basis.hv", { src: src(t, m[1]), pp: num(m[2], locale, 2), n: m[3], rate: num(m[4], locale, 2), kg: num(m[5], locale, 1) });
  if ((m = b.match(/^(.+): ([\d.]+) SDR × ([\d.]+) kg gross$/)))
    return t("claims.basis.per_kg", { src: src(t, m[1]), rate: num(m[2], locale, 2), kg: num(m[3], locale, 1) });
  return b;
}

export function localNote(note, t) {
  const n = (note || "").trim();
  let m;
  if ((m = n.match(/^As intermediary .* under (.+) is claimed from the carrier\.$/))) return t("claims.note.intermediary", { label: m[1] });
  if (n === "No convention limit for delay under this regime.") return t("claims.note.no_delay_limit");
  if (/^Freight amount unknown/.test(n)) return t("claims.note.freight_unknown");
  if (/^Package count missing/.test(n)) return t("claims.note.packages_missing");
  if (/^Gross weight of the affected goods is needed/.test(n)) return t("claims.note.kg_needed");
  if (/^NSAB 2015 § 2 network clause/.test(n)) return t("claims.note.network");
  return n;
}

export const sdrSource = (s, t) => (/^IMF/i.test(s || "") ? t("claims.cap.imf") : s || "—");

/** Claim record -> the estimate shape CapCalculation renders. */
export function capFromClaim(c) {
  if (!c) return null;
  return {
    regime: c.liability_regime, regime_label: c.regime_label, basis: c.cap_basis, cap_sdr: c.cap_sdr, cap_eur_cents: c.cap_eur_cents,
    notes: c.cap_notes || [], sdr_rate: c.sdr_rate, delivery_date: c.delivery_date, time_bar_date: c.time_bar_date,
    time_bar_alert_date: c.time_bar_alert_date, regime_time_bar_date: c.regime_time_bar_date, notice_deadline: c.notice_deadline,
    // A draft is not notified yet: show "late" when sending it now would already be late.
    notice_late: c.notice_late || (c.status === "draft" && !!c.notice_deadline && daysLeft(c.notice_deadline) < 0), notice_source: c.notice_source, forwarder_role: c.forwarder_role, regime_source: c.regime_source,
  };
}

const DECIDED = ["approved", "rejected", "paid"];
export const isDecided = (status) => DECIDED.includes(status);

/** Days to the time bar; null when unknown. */
export function daysLeft(iso) {
  if (!iso) return null;
  const target = new Date(`${iso.slice(0, 10)}T00:00:00`);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((target.getTime() - today.getTime()) / 86400000);
}

export function historyEvents(history, t) {
  return (history || []).map((h, i) => {
    const key = `claims.hist.${String(h.action || "").replace(/\s+/g, "_")}`;
    const title = t(key) === key ? h.action : t(key);
    return { id: `${h.at}-${i}`, at: h.at, title, source: h.by || null, tone: h.action === "proposal declined" ? "warning" : undefined };
  });
}
