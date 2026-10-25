// Every text the flow map takes from an event, in the page's language.
// The stable way is a key: the backend adds data.title_key + data.title_vars (keys under demoflow.ev.* in
// i18n/demoflow.js; contract in shared/interfaces.md), and the vars are formatted here for the locale (money, days).
// Without a key, a known fixed title comes from the displayText catalogue (storedText.fi.json); anything else is shown
// as sent (partner names, e-mail subjects).
import { useCallback } from "react";
import { useT } from "@/lib/i18n";
import { displayText } from "@/lib/localizedData";
import { money } from "@/lib/format";

const loc = (locale) => (locale === "fi" ? "fi-FI" : "en-GB");

export const daysText = (n, t) => (Number(n) === 1 ? t("demoflow.day") : t("demoflow.days", { n }));

/** A structured value from event data, formatted for the locale: "EUR 1,010.00" → "1 010,00 €", "2026-11-08" → "8.11.2026". */
export function valueText(v, locale) {
  if (typeof v !== "string") return v == null ? "" : String(v);
  const m = v.match(/^([A-Z]{3}) (-?[\d,]+(?:\.\d+)?)$/);
  if (m) return money(Math.round(Number(m[2].replace(/,/g, "")) * 100), locale, { currency: m[1] });
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return new Date(`${v}T12:00:00Z`).toLocaleDateString(loc(locale), { timeZone: "Europe/Helsinki" });
  return displayText(v, locale);
}

export function formatVars(vars, t, locale) {
  const out = { ...(vars || {}) };
  if (vars?.amount != null) out.amount = money(Math.round(Number(vars.amount) * 100), locale, { currency: vars.currency || "EUR" });
  if (vars?.days != null) out.days = daysText(vars.days, t);
  if (typeof vars?.ready === "string") out.ready = valueText(vars.ready, locale);
  return out;
}

// Runs recorded before the backend sent title keys (A4 DQ-G6P53M / DQ-P7A5WT) carry only the English title. These are
// the backend's own templates (services/demo_autoquote, demo_world), read back into the same key and vars so a replay
// formats exactly like a keyed run. A title_key on the event always wins.
const AMT = "([A-Z]{3}) (-?[\\d,]+(?:\\.\\d+)?)";
// Long titles arrive cut at 59 characters: the trailing word may be any prefix of itself (" received" → " recei").
const cut = (w) => [...w].reverse().reduce((acc, ch) => `(?:${ch.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}${acc})?`, "");
const RECORDED = [
  [/^Request understood · (\d+) details? missing$/, "intake.understood", ["n"]],
  [/^Asked (\d+) questions? by e-mail$/, "clarify.asked", ["n"]],
  [/^Researching partners for (.+) → (.+)$/, "research.researching", ["from", "to"]],
  [/^Web search: (\d+) queries, (\d+) pages read$/, "research.web_search", ["queries", "pages"]],
  [/^Partners from (\S+)$/, "research.reused", ["source"]],
  [/^(\d+) partners? selected · no new search$/, "research.selected_reused", ["n"]],
  [/^(\d+) partners? selected$/, "research.selected", ["n"]],
  [/^Sending (\d+) rate requests?$/, "outreach.sending", ["n"]],
  [/^(\d+) rate requests? sent$/, "outreach.sent", ["n"]],
  [/^Reusing (\d+) rate requests?$/, "outreach.reusing", ["n"]],
  [/^(\d+) rate requests? reused · (\d+) new$/, "outreach.reused", ["n", "new"]],
  [/^Rate request from (\S+) · awaiting reply$/, "partner.rfq_reused", ["source"]],
  [new RegExp(`^Rate received: ${AMT} · (\\d+) days?$`), "partner.rate", ["currency", "amount", "days"]],
  [new RegExp(`^Accepted our counteroffer: ${AMT} · (\\d+) days?$`), "partner.counter_accepted", ["currency", "amount", "days"]],
  [new RegExp(`^Rate stands at ${AMT}$`), "partner.rate_stands", ["currency", "amount"]],
  [/^(\d+) rates? in$/, "replies.count", ["n"]],
  [/^Comparing (\d+) rates · countering the best$/, "replies.comparing", ["n"]],
  [/^Best rate: (.+)$/, "replies.best", ["partner"]],
  [new RegExp(`^${AMT} · (\\d+) days?$`), "pricing.priced", ["currency", "amount", "days"]],
  [/^Offer (\S+) drafted$/, "offer.drafted", ["quote"]],
  [/^Offer (\S+) awaiting approval$/, "offer.awaiting_approval", ["quote"]],
  [new RegExp(`^Offer sent · ${AMT}$`), "offer.sent", ["currency", "amount"]],
  [new RegExp(`^Customer reply · (.+?)${cut(" received")}$`), "world.customer_reply", ["name"]],
  [new RegExp(`^Carrier reply · (.+?)${cut(" received")}$`), "world.carrier_reply", ["name"]],
  [/^Booking confirmed · ref (\S+)$/, "booking.confirmed", ["ref"]],
  [/^Booking request sent to (.+?) *$/, "booking.request_sent", ["name"]],
  // An outgoing e-mail: the subject stays as written, the status is the page's language.
  [new RegExp(`^(.+?) ·${cut(" sent")}$`), "email.sent", ["subject"]],
];

/** The key and vars a recorded title without a key was made from, or null. */
export function recordedKey(title) {
  for (const [re, key, names] of RECORDED) {
    const m = typeof title === "string" && title.match(re);
    if (!m) continue;
    const vars = {};
    names.forEach((n, i) => { vars[n] = n === "amount" ? m[i + 1].replace(/,/g, "") : m[i + 1]; });
    return { key, vars };
  }
  return null;
}

export function titleText(ev, t, locale) {
  if (!ev) return "";
  const rec = ev.data?.title_key ? null : recordedKey(ev.title);
  const key = ev.data?.title_key || rec?.key;
  const vars = ev.data?.title_key ? ev.data?.title_vars || {} : rec?.vars || {};
  if (key) {
    const keys = [Number(vars.n) === 1 ? `demoflow.ev.${key}.one` : null, `demoflow.ev.${key}`].filter(Boolean);
    for (const k of keys) {
      const s = t(k, formatVars(vars, t, locale));
      if (s !== k) return s;
    }
  }
  return displayText(ev.title || "", locale);
}

/** A document code in the page's language: "packing_list" → "Pakkausluettelo"; an unknown code reads as itself. */
export function docKind(kind, t) {
  if (!kind) return "";
  const key = `demoflow.doc.${kind}`;
  const s = t(key);
  return s === key ? kind.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : s;
}

/** The documents step's subtitle (model.docsSub), count and status first so a short card cuts only the list:
 * "5 asiakirjaa lähetetty · CMR-rahtikirja, pakkausluettelo, …"; one document: "CMR-rahtikirja lähetetty". */
export function docsLine(docs, t, locale) {
  if (!docs) return "";
  const one = docs.n === 1;
  // Inside the list a name is lower-case unless it is an acronym ("CMR-rahtikirja", "EUR.1-hakemus").
  const names = docs.kinds.map((k) => {
    const s = docKind(k, t);
    return !one && /^.[a-zäöå]/.test(s) ? s[0].toLowerCase() + s.slice(1) : s;
  });
  const key = `demoflow.sub.docs${docs.sent ? "Sent" : "Ready"}${one ? "One" : ""}`;
  return t(key, { n: docs.n, list: names.join(", ") });
}

// Known run errors by their backend message, until the failure event carries data.error_key. The exception type
// ("ValueError: ") is not part of the message. An unknown error shows only "Failed"; the panel keeps the full text.
const ERROR_KEYS = {
  "Native quoting produced no option from the winning partner rate": "no_option_from_winning_rate",
  "Native procurement already accepted a different offer; review the winning RFQ": "other_offer_accepted",
};

/** The failed stage's subtitle: "Epäonnistui · ei tarjousta voittaneesta hinnasta". */
export function failText(ended, t) {
  const msg = String(ended?.reason || "").replace(/^[A-Za-z]+(?:Error|Exception): /, "").trim();
  const key = ended?.errorKey || ERROR_KEYS[msg];
  const reason = key ? t(`demoflow.err.${key}`) : "";
  return [t("demoflow.state.failed"), reason && reason !== `demoflow.err.${key}` ? reason : ""].filter(Boolean).join(" · ");
}

// Approval reasons by their backend sentence, until the card carries reason codes (send_decision.reason_codes).
const REASON_KEYS = [
  [/^Customs formalities required\b/, "customs_required"],
];

/** Why the offer waits on a person, in the page's language: ["tulliselvitys vaaditaan"]. */
export function approvalReasons(ev, t) {
  const sd = ev?.data?.send_decision || {};
  const codes = Array.isArray(sd.reason_codes) ? sd.reason_codes : [];
  const fromText = (Array.isArray(sd.decision_reasons) ? sd.decision_reasons : [])
    .map((r) => REASON_KEYS.find(([re]) => re.test(String(r)))?.[1]).filter(Boolean);
  return [...new Set([...codes, ...fromText])].map((k) => t(`demoflow.approval.${k}`)).filter((s) => !s.startsWith("demoflow.approval."));
}

/** The offer waiting on a person: "Odottaa hyväksyntää · tulliselvitys vaaditaan". */
export function approvalText(ev, t) {
  return [t("demoflow.sub.awaitingApproval"), ...approvalReasons(ev, t)].join(" · ");
}

/** A partner rate excluded as not valid on the ready date: "Voimassa 1.11. asti · ei kelpaa". */
export function expiredText(x, t, locale) {
  const d = x?.valid_to && /^\d{4}-\d{2}-\d{2}$/.test(x.valid_to)
    ? new Date(`${x.valid_to}T12:00:00Z`).toLocaleDateString(loc(locale), locale === "fi" ? { day: "numeric", month: "numeric", timeZone: "Europe/Helsinki" } : { day: "numeric", month: "short", timeZone: "Europe/Helsinki" })
    : null;
  return d ? t("demoflow.sub.expired", { d }) : t("demoflow.sub.expiredNoDate");
}

export function useTexts() {
  const { t, locale } = useT();
  const title = useCallback((ev) => titleText(ev, t, locale), [t, locale]);
  // The status line under the header: an offer on hold also says why.
  const line = useCallback((ev) => [titleText(ev, t, locale), ...(ev?.data?.title_key === "offer.awaiting_approval" ? approvalReasons(ev, t) : [])].join(" · "), [t, locale]);
  const value = useCallback((v) => valueText(v, locale), [locale]);
  return { title, line, value, t, locale };
}
