import dictionaries from "@/i18n";
import { approvalText, docsLine, expiredText, failText, titleText, valueText } from "./titles";

const tr = (locale) => (key, vars) => {
  const s = dictionaries[locale][key];
  if (s === undefined) return key;
  return s.replace(/\{(\w+)\}/g, (_, k) => (vars && vars[k] !== undefined ? vars[k] : `{${k}}`));
};
const ev = (title, data) => ({ seq: 1, kind: "step", node: "outreach", title, data: data || {} });

test("a keyed title reads in the page's language, with fi-FI money and days", () => {
  const rate = ev("Rate received: EUR 1,010.00 · 4 days", { title_key: "partner.rate", title_vars: { amount: 1010, currency: "EUR", days: 4 } });
  expect(titleText(rate, tr("fi"), "fi")).toBe("Hinta saapui: 1 010,00 € · 4 päivää");
  expect(titleText(rate, tr("en"), "en")).toBe("Rate received: €1,010.00 · 4 days");
  const sent = ev("6 rate requests sent", { title_key: "outreach.sent", title_vars: { n: 6 } });
  expect(titleText(sent, tr("fi"), "fi")).toBe("6 hintapyyntöä lähetetty");
  const one = ev("1 rate in", { title_key: "replies.count", title_vars: { n: 1 } });
  expect(titleText(one, tr("fi"), "fi")).toBe("1 hinta saapunut");
});

test("without a key, a fixed title comes from the catalogue; anything else is shown as sent", () => {
  expect(titleText(ev("Details complete"), tr("fi"), "fi")).toBe("Tiedot täydennetty");
  expect(titleText(ev("Run stopped by ops"), tr("fi"), "fi")).toBe("Ajo pysäytetty");
  expect(titleText(ev("Details complete"), tr("en"), "en")).toBe("Details complete");
  expect(titleText(ev("Kuljetus Oy Laakso"), tr("fi"), "fi")).toBe("Kuljetus Oy Laakso");
});

test("structured values format for the locale", () => {
  expect(valueText("EUR 1,010.00", "fi")).toBe("1 010,00 €");
  expect(valueText("2026-11-08", "fi")).toBe("8.11.2026");
});

test("a recorded title without a key reads like the keyed one, cut-off titles included", () => {
  const fi = (title) => titleText(ev(title), tr("fi"), "fi").replace(/[\u00a0\u202f]/g, " ");
  expect(fi("Rate received: EUR 1,010.00 · 4 days")).toBe("Hinta saapui: 1 010,00 € · 4 päivää");
  expect(titleText(ev("Rate received: EUR 1,010.00 · 4 days"), tr("en"), "en")).toBe("Rate received: €1,010.00 · 4 days");
  expect(fi("6 partners selected · no new search")).toBe("6 kumppania valittu · ei uutta hakua");
  expect(fi("6 rate requests sent")).toBe("6 hintapyyntöä lähetetty");
  expect(fi("EUR 931.80 · 3 days")).toBe("931,80 € · 3 päivää");
  expect(fi("Carrier reply · Kuljetus ja maanrakennus Katupojat Oy recei")).toBe("Rahdinkuljettajan vastaus saapui · Kuljetus ja maanrakennus Katupojat Oy");
  expect(fi("Re: Pakkausluettelo PL-2026-000002 · [HUO-2026-000002] · se")).toBe("Re: Pakkausluettelo PL-2026-000002 · [HUO-2026-000002] · lähetetty");
  expect(fi("Payment matched")).toBe("Maksu kohdistettu");
  expect(fi("Kuljetus J.J. Oksanen Oy")).toBe("Kuljetus J.J. Oksanen Oy");
});

test("the documents subtitle names document kinds from their codes, not the e-mail subject", () => {
  const docs = { kinds: ["cmr", "packing_list", "certificate_of_origin"], n: 4, sent: true };
  expect(docsLine(docs, tr("fi"), "fi")).toBe("4 asiakirjaa lähetetty · CMR-rahtikirja, pakkausluettelo, alkuperätodistus");
  expect(docsLine(docs, tr("en"), "en")).toBe("4 documents sent · CMR consignment note, packing list, certificate of origin");
  expect(docsLine({ kinds: ["packing_list"], n: 1, sent: false }, tr("fi"), "fi")).toBe("Pakkausluettelo valmiina");
});

test("an expired-rates decision reads with the ready date in the page's format", () => {
  const e = ev("2 rates not valid on 2026-10-27", { title_key: "replies.expired", title_vars: { n: 2, ready: "2026-10-27" } });
  expect(titleText(e, tr("fi"), "fi")).toBe("2 hintaa ei voimassa 27.10.2026");
  expect(titleText(e, tr("en"), "en")).toBe("2 rates not valid on 27/10/2026");
});

test("a failed run names its reason from the error key or a known message; an unknown error says only failed", () => {
  expect(failText({ reason: "ValueError: Native quoting produced no option from the winning partner rate" }, tr("fi"))).toBe("Epäonnistui · ei tarjousta voittaneesta hinnasta");
  expect(failText({ errorKey: "no_option_from_winning_rate" }, tr("en"))).toBe("Failed · no quote option from the winning rate");
  expect(failText({ reason: "RuntimeError: something new" }, tr("fi"))).toBe("Epäonnistui");
  expect(failText({ reason: "ValueError: Native procurement already accepted a different offer; review the winning RFQ" }, tr("fi")))
    .toBe("Epäonnistui · toinen tarjous oli jo hyväksytty");
});

test("the offer on hold names its reason; an excluded rate says until when it was valid", () => {
  const ev2 = { data: { title_key: "offer.awaiting_approval", send_decision: { decision_reasons: ["Customs formalities required. Tulli filing is postponed: …"] } } };
  expect(approvalText(ev2, tr("fi"))).toBe("Odottaa hyväksyntää · tulliselvitys vaaditaan");
  expect(approvalText(ev2, tr("en"))).toBe("Awaiting approval · customs formalities required");
  expect(approvalText({ data: { send_decision: { decision_reasons: ["Something else"] } } }, tr("fi"))).toBe("Odottaa hyväksyntää");
  expect(expiredText({ valid_to: "2026-11-01" }, tr("fi"), "fi")).toBe("Voimassa 1.11. asti · ei kelpaa");
  expect(expiredText({ valid_to: "2026-11-01" }, tr("en"), "en")).toBe("Valid to 1 Nov · excluded");
});

test("agreed-rate events read in both languages", () => {
  const fi = (title, data) => titleText(ev(title, data), tr("fi"), "fi").replace(/[\u00a0\u202f]/g, " ");
  expect(fi("Rate already agreed: EUR 760.00 · 3 days", { title_key: "partner.rate_agreed", title_vars: { amount: 760, days: 3 } })).toBe("Hinta jo sovittu: 760,00 € · 3 päivää");
  expect(fi("Agreed rate does not fit this request", { title_key: "partner.agreed_rate_skipped" })).toBe("Sovittu hinta ei sovi tähän pyyntöön");
  expect(fi("A real reply changes an accepted rate · a person decides", { title_key: "replies.real_reply_after_agreed", title_vars: { partner: "K" } }))
    .toBe("Todellinen vastaus muuttaa sovittua hintaa · ihminen päättää");
  expect(fi("Priced from market rates · no partner rate", { title_key: "replies.market_rates" })).toBe("Hinnoiteltu markkinahinnoista · ei kumppanihintaa");
  expect(fi("No usable partner rate · a person decides", { title_key: "replies.no_rate", title_vars: { declined: 6, n: 6 } })).toBe("Ei käyttökelpoista kumppanihintaa · ihminen päättää");
  expect(fi("Real reply · the demo does not answer it", { title_key: "partner.real_reply_held" })).toBe("Todellinen vastaus · demo ei vastaa siihen");
  expect(fi("Comparing 5 rates · best rate already agreed", { title_key: "replies.best_agreed", title_vars: { n: 5, partner: "K" } })).toBe("5 hintaa vertailussa · paras hinta jo sovittu");
});
