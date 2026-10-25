import React, { useContext, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDownLeft, ArrowUpRight, ChevronRight, ExternalLink, FileText, X } from "lucide-react";
import { StatusBadge } from "@/components/app/StatusBadge";
import { useT } from "@/lib/i18n";
import { displayText } from "@/lib/localizedData";
import { money } from "@/lib/format";
import { cn } from "@/lib/utils";
import { nodeGroup, nodeDetail, reasonPoints } from "./model";
import { daysText, docKind, useTexts, valueText } from "./titles";
import { nodeIcon, useNodeLabel } from "./FlowMap";
import { RunAccess, fileUrl } from "./useRunStream";

const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return ""; } };

// The partner scorer's fixed vocabulary (services/partner_discovery.score), read in the page's language when the
// partner carries no reason_i18n (a partner reused from the network).
const WHY = [
  [/^operates (road|sea|air|rail)$/i, (m, t) => t("demoflow.why.operates", { mode: t(`demoflow.why.mode.${m[1].toLowerCase()}`) })],
  [/^general logistics \(mode not stated\)$/i, (m, t) => t("demoflow.why.general")],
  [/^based in ([A-Z]{2}) \(lane end\)$/i, (m, t, locale) => t("demoflow.why.basedIn", { country: countryName(m[1], locale) })],
  [/^haulier$/i, (m, t) => t("demoflow.why.haulier")],
  [/^carrier$/i, (m, t) => t("demoflow.why.carrier")],
  [/^agent at destination$/i, (m, t) => t("demoflow.why.agentDest")],
  [/^forwarder\/agent$/i, (m, t) => t("demoflow.why.agent")],
  [/^company website$/i, (m, t) => t("demoflow.why.website")],
  [/^business id in trade register$/i, (m, t) => t("demoflow.why.businessId")],
  [/^registered (\d+) years ago$/i, (m, t) => t("demoflow.why.registered", { n: m[1] })],
  [/^found in several sources$/i, (m, t) => t("demoflow.why.sources")],
  [/^published role address$/i, (m, t) => t("demoflow.why.roleAddress")],
  [/^published personal business address$/i, (m, t) => t("demoflow.why.personalAddress")],
  [/^declined before$/i, (m, t) => t("demoflow.why.declined")],
];
const countryName = (cc, locale) => {
  try { return new Intl.DisplayNames([locale === "fi" ? "fi" : "en"], { type: "region" }).of(cc.toUpperCase()) || cc; } catch { return cc; }
};

function Reason({ text, t, locale, scored }) {
  const points = reasonPoints(text);
  if (!points.length) return text ? <p className="df-p">{text}</p> : null;
  const show = (p) => {
    if (!scored) return p;
    const hit = WHY.map(([re, f]) => [p.match(re), f]).find(([m]) => m);
    return hit ? hit[1](hit[0], t, locale) : p;
  };
  return <ul className="df-bullets">{points.map((p) => <li key={p}>{show(p)}</li>)}</ul>;
}

const BADGE = { active: "in-progress", waiting: "pending", done: "success", failed: "failed", idle: "expired" };

export const clockTime = (iso, locale) => {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(locale === "fi" ? "fi-FI" : "en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
};

function Section({ title, count, children }) {
  return (
    <section className="df-section">
      <h3 className="df-section-title">
        {title}
        {count ? <span className="df-count">{count}</span> : null}
      </h3>
      {children}
    </section>
  );
}

function Email({ e, locale, t }) {
  const [open, setOpen] = useState(false);
  const out = e.direction === "out";
  const who = out ? e.to : e.from;
  return (
    <li className={cn("df-email", e.earlier && "df-email-earlier")}>
      <button type="button" className="df-row-btn" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className={cn("df-dir", out ? "df-dir-out" : "df-dir-in")} aria-label={out ? t("demoflow.email.out") : t("demoflow.email.in")}>
          {out ? <ArrowUpRight /> : <ArrowDownLeft />}
        </span>
        <span className="df-email-main">
          <span className="df-email-subject">{e.subject || t("demoflow.email.noSubject")}</span>
          {/* An earlier run's message shows when it really arrived, and which run it belongs to. */}
          <span className="df-email-meta">{who}{e.earlier && e.receivedAt ? ` · ${clockTime(e.receivedAt, locale)}` : e.at ? ` · ${clockTime(e.sim_at || e.at, locale)}` : ""}</span>
          {e.earlier ? <span className="df-tag df-email-earlier-tag">{t("demoflow.email.earlier")}{e.earlierRef ? ` · ${e.earlierRef}` : ""}</span> : null}
        </span>
        <ChevronRight className={cn("df-chev", open && "df-chev-open")} aria-hidden />
      </button>
      {open ? (
        <div className="df-email-body">
          <dl className="df-kv df-kv-tight">
            {e.from ? <><dt>{t("demoflow.email.from")}</dt><dd>{e.from}</dd></> : null}
            {e.to ? <><dt>{t("demoflow.email.to")}</dt><dd>{e.to}</dd></> : null}
            {e.provider || e.status ? <><dt>{t("demoflow.email.delivery")}</dt><dd>{[e.provider, e.status].filter(Boolean).join(" · ")}</dd></> : null}
            {e.message_id ? <><dt>Message-ID</dt><dd className="df-mono">{e.message_id}</dd></> : null}
            {e.simulated ? <><dt>{t("demoflow.email.source")}</dt><dd><span className="df-tag">{t(e.direction === "in" && e.partnerReply ? "demoflow.chip.simReply" : "demoflow.simulated")}</span></dd></> : null}
          </dl>
          {e.text ? <pre className="df-pre">{e.text}</pre> : null}
        </div>
      ) : null}
    </li>
  );
}

function Tool({ c, t }) {
  return (
    <li className="df-tool">
      <div className="df-tool-head">
        <span className="df-mono df-tool-name">{c.tool}</span>
        {c.latency_ms ? <span className="df-num">{(c.latency_ms / 1000).toFixed(1)} s</span> : null}
      </div>
      {c.input ? <div className="df-tool-io"><span>{t("demoflow.tool.in")}</span>{String(c.input)}</div> : null}
      {c.output ? <div className="df-tool-io"><span>{t("demoflow.tool.out")}</span>{String(c.output)}</div> : null}
      {c.simulated ? <span className="df-tag">{t("demoflow.simulated")}</span> : null}
      {(c.urls || []).length ? (
        <ul className="df-links">{c.urls.slice(0, 8).map((u) => <li key={u}><a href={u} target="_blank" rel="noreferrer">{u.replace(/^https?:\/\//, "")}</a></li>)}</ul>
      ) : null}
    </li>
  );
}

function Partners({ items, t, locale }) {
  return (
    <ul className="df-list">
      {items.map((p) => (
        <li key={p.name} className="df-partner">
          <div className="df-partner-head">
            <span className="df-strong">{p.name}</span>
            <span className="df-muted">{partnerMeta(p, t, locale)}</span>
          </div>
          <Reason text={p.reason_i18n?.[locale] || p.reason} t={t} locale={locale} scored={!p.reason_i18n?.[locale]} />
          <div className="df-muted df-small">
            {displayText(p.found_by || "", locale)}{p.website ? <> · <a href={p.website} target="_blank" rel="noreferrer">{p.website.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "")}</a></> : null}
          </div>
        </li>
      ))}
    </ul>
  );
}

function Ranking({ items, locale, t, best = true }) {
  return (
    <div className="df-table-wrap">
      <table className="df-table">
        <thead><tr><th>{t("demoflow.rank.partner")}</th><th className="df-r">{t("demoflow.rank.cost")}</th><th className="df-r">{t("demoflow.rank.transit")}</th></tr></thead>
        <tbody>
          {items.map((r, i) => (
            <tr key={`${r.partner}-${i}`} className={best && i === 0 ? "df-best" : undefined}>
              <td>
                {r.partner}
                <div className="df-cell-sub">
                  {r.valid_to ? t("demoflow.rank.validTo", { d: valueText(r.valid_to, locale) }) : null}
                  {r.agreed ? <span className="df-tag df-tag-inline">{t("demoflow.rank.agreed")}</span> : null}
                  {r.reply_source === "world" || (!r.reply_source && r.simulated) ? <span className="df-tag df-tag-inline">{t("demoflow.chip.simReply")}</span> : null}
                </div>
              </td>
              <td className="df-r df-num">{money(r.cost_cents, locale)}</td>
              <td className="df-r df-num">{r.transit_days != null ? daysText(r.transit_days, t) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Lines({ items, locale, t }) {
  const total = items.reduce((s, l) => s + (l.amount_cents || 0), 0);
  return (
    <div className="df-table-wrap">
      <table className="df-table">
        <thead><tr><th>{t("demoflow.line.item")}</th><th className="df-r">{t("demoflow.line.cost")}</th><th className="df-r">{t("demoflow.line.price")}</th></tr></thead>
        <tbody>
          {items.map((l, i) => (
            <tr key={`${l.code}-${i}`}><td>{l.description ? valueText(l.description, locale) : l.code}</td><td className="df-r df-num">{money((l.unit_cost_cents || 0) * (l.qty || 1), locale)}</td><td className="df-r df-num">{money(l.amount_cents, locale)}</td></tr>
          ))}
          <tr className="df-total"><td>{t("demoflow.line.total")}</td><td /><td className="df-r df-num">{money(total, locale)}</td></tr>
        </tbody>
      </table>
    </div>
  );
}

// "approval_id" -> "Approval id": fact keys come straight from the event data.
const factLabel = (k, t) => {
  const key = `demoflow.fact.${k.toLowerCase().replace(/[^a-z0-9]+/g, "_")}`;
  const s = t(key);
  return s !== key ? s : k.replace(/[_-]+/g, " ").replace(/^./, (c) => c.toUpperCase());
};

/** "trucker · FI" in the page's language: a role from the dictionary, a country by its ISO code. */
export function partnerMeta(p, t, locale) {
  if (!p) return "";
  const roleKey = `demoflow.role.${String(p.role || "").toLowerCase()}`;
  const role = p.role ? (t(roleKey) !== roleKey ? t(roleKey) : p.role) : null;
  let country = p.country || null;
  try {
    if (country && /^[A-Z]{2}$/.test(country)) country = new Intl.DisplayNames([locale === "fi" ? "fi-FI" : "en-GB"], { type: "region" }).of(country);
  } catch (e) { /* keep the code */ }
  return [role, country].filter(Boolean).join(" · ");
}

// "power_of_attorney" -> the dictionary label, or the readable words when the kind is new.
// Coded fact values from the backend (a broker release's direction, yes/no), read in the page's language.
const FACT_VALUES = { export: "demoflow.dir.export", import: "demoflow.dir.import", yes: "demoflow.yes", no: "demoflow.no" };

function factValue(v, t, texts, locale) {
  if (typeof v === "boolean") return t(v ? "demoflow.yes" : "demoflow.no");
  if (v && typeof v === "object") return JSON.stringify(v);
  if (FACT_VALUES[v]) return t(FACT_VALUES[v]);
  const d = typeof v === "string" && v.match(/^(\d+) days?$/);
  if (d) return daysText(Number(d[1]), t);
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?Z$/.test(v)) return clockTime(v, locale);
  return texts.value(v);
}

// Reasoning built from structured data reads in the page's language; other reasoning goes through the catalogue.
function reasonText(r, partner, t, locale) {
  const a = r.agreed;
  if (r.key === "partner.rate_agreed" && a && a.cost_cents != null) {
    return t("demoflow.reason.rateAgreed", { partner: partner?.name || "", amount: money(a.cost_cents, locale),
      card: String(a.rate_card_id || "").slice(0, 8), valid: a.valid_to ? valueText(a.valid_to, locale) : "—" });
  }
  return displayText(r.text, locale);
}

export default function NodePanel({ runId, model, id, onClose }) {
  const { t, locale } = useT();
  const texts = useTexts();
  const nodeLabel = useNodeLabel();
  const token = useContext(RunAccess);
  const node = model.nodes[id];
  const det = useMemo(() => nodeDetail(model, id), [model, id]);
  const closeRef = useRef(null);
  useEffect(() => { closeRef.current?.focus(); }, [id]);
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const Icon = nodeIcon(id, node);
  const state = node?.state || "idle";
  const partner = node?.meta?.partner;
  const facts = Object.entries(det.facts);
  const reasoning = det.reasoning.filter((r) => r.text !== partner?.reason && r.text !== partner?.reason_i18n?.en);
  const empty = !node;

  return (
    <aside className="df-panel" aria-label={nodeLabel(id, node)}>
      <header className="df-panel-head">
        <span className="df-icon df-icon-lg" aria-hidden><Icon /></span>
        <div className="df-panel-title">
          <h2>{nodeLabel(id, node)}</h2>
          <div className="df-panel-sub">
            <StatusBadge size="sm" state={BADGE[state]} label={state === "waiting" ? t(node?.stateKind === "email_out" || !node?.title ? "demoflow.awaiting" : "demoflow.waiting") : t(`demoflow.state.${state}`)} />
            {partner ? <span className="df-muted">{partnerMeta(partner, t, locale)}</span> : null}
          </div>
        </div>
        <button ref={closeRef} type="button" className="df-iconbtn" onClick={onClose} aria-label={t("demoflow.close")}><X /></button>
      </header>
      <div className="df-panel-body">
        {empty ? <p className="df-muted df-p">{t(nodeGroup(id) === "actor" ? "demoflow.emptyActor" : "demoflow.emptyNode")}</p> : null}
        {node?.title && state !== "waiting" ? <p className="df-lead">{texts.title(node.titleEv) || node.title}</p> : null}
        {partner ? (
          <Section title={t("demoflow.sec.partner")}>
            <Reason text={partner.reason_i18n?.[locale] || partner.reason} t={t} locale={locale} scored={!partner.reason_i18n?.[locale]} />
            <dl className="df-kv">
              {partner.contact_email ? <><dt>{t("demoflow.email.to")}</dt><dd>{partner.contact_email}</dd></> : null}
              {partner.website ? <><dt>{t("demoflow.web")}</dt><dd><a href={partner.website} target="_blank" rel="noreferrer">{partner.website.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "")}</a></dd></> : null}
              {partner.found_by ? <><dt>{t("demoflow.foundBy")}</dt><dd>{displayText(partner.found_by, locale)}</dd></> : null}
              {partner.contact_source_url ? <><dt>{t("demoflow.contactSource")}</dt><dd><a href={partner.contact_source_url} target="_blank" rel="noreferrer">{host(partner.contact_source_url)}</a></dd></> : null}
            </dl>
          </Section>
        ) : null}
        {facts.length ? (
          <Section title={t("demoflow.sec.facts")}>
            <dl className="df-kv">{facts.map(([k, v]) => <React.Fragment key={k}><dt>{factLabel(k, t)}</dt><dd>{factValue(v, t, texts, locale)}</dd></React.Fragment>)}</dl>
          </Section>
        ) : null}
        {det.documents.length ? (
          <Section title={t("demoflow.sec.documents")} count={det.documents.length}>
            <ul className="df-list">
              {det.documents.map((d) => (
                <li key={d.file_id}>
                  <a className="df-doc" href={fileUrl(runId, d.file_id, token)} target="_blank" rel="noreferrer">
                    <FileText aria-hidden /> <span className="df-doc-text"><span className="df-doc-title">{d.title || d.filename}</span> <span className="df-doc-kind">{d.filename && d.filename !== d.title ? d.filename : docKind(d.kind, t)}</span></span> <ExternalLink className="df-ext" aria-hidden />
                  </a>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}
        {det.extra.ranking ? <Section title={t("demoflow.sec.ranking")}><Ranking items={det.extra.ranking} locale={locale} t={t} /></Section> : null}
        {/* Rates that were in but had expired by the ready date (replies.expired, and next to the best-rate ranking). */}
        {det.extra.expired?.length ? <Section title={t("demoflow.sec.expired")}><Ranking items={det.extra.expired} best={false} locale={locale} t={t} /></Section> : null}
        {/* Each partner's answer when no rate is usable (replies.no_rate, replies.no_valid_rate). */}
        {det.extra.replies?.length ? (
          <Section title={t("demoflow.sec.partnerReplies")}>
            <ul className="df-list">
              {det.extra.replies.map((r, i) => (
                <li key={`${r.outreach_id || r.partner}-${i}`}>
                  {r.partner} · {t(`demoflow.reply.${String(r.reply || "").replace(/ /g, "_")}`)}
                  {r.source === "world" ? <span className="df-tag df-tag-inline">{t("demoflow.chip.simReply")}</span> : null}
                </li>
              ))}
            </ul>
          </Section>
        ) : null}
        {det.extra.lines ? <Section title={t("demoflow.sec.lines")}><Lines items={det.extra.lines} locale={locale} t={t} /></Section> : null}
        {reasoning.length ? (
          <Section title={t("demoflow.sec.reasoning")}>
            {reasoning.slice().reverse().map((r) => <p key={r.seq} className="df-p df-reason">{reasonText(r, partner, t, locale)}</p>)}
          </Section>
        ) : null}
        {det.extra.partners ? <Section title={t("demoflow.sec.partners")} count={det.extra.partners.length}><Partners items={det.extra.partners} t={t} locale={locale} /></Section> : null}
        {det.emails.length ? (
          <Section title={t("demoflow.sec.emails")} count={det.emails.length}>
            <ul className="df-list">{det.emails.map((e, i) => <Email key={`${e.seq}-${i}`} e={e} locale={locale} t={t} />)}</ul>
          </Section>
        ) : null}
        {det.tools.length ? (
          <Section title={t("demoflow.sec.tools")} count={det.tools.length}>
            <ul className="df-list">{det.tools.map((c, i) => <Tool key={`${c.seq}-${i}`} c={c} t={t} />)}</ul>
          </Section>
        ) : null}
        {det.sources.length ? (
          <Section title={t("demoflow.sec.sources")}>
            <ul className="df-links">{det.sources.map((s) => <li key={s.url}><a href={s.url} target="_blank" rel="noreferrer">{s.label || s.url}</a></li>)}</ul>
          </Section>
        ) : null}
        {det.events.length ? (
          <Section title={t("demoflow.sec.activity")} count={det.events.length}>
            <ol className="df-trace">
              {det.events.slice().reverse().map((ev) => (
                <li key={ev.seq}>
                  <span className="df-num df-muted">{clockTime(ev.sim_at || ev.at, locale)}</span>
                  <span>{texts.title(ev)}{ev.simulated ? <span className="df-tag df-tag-inline">{t("demoflow.simulated")}</span> : null}</span>
                </li>
              ))}
            </ol>
          </Section>
        ) : null}
      </div>
    </aside>
  );
}
