// s11 shared customs pieces: the hub tab row (Import / Export / Transit / ICS2 + reference pages), bilingual text
// helpers and the "API not installed" note. Feature data mapping stays in the Taric/Origin/Controls/Export/Transit/Ens files.
import React from "react";
import { Link, NavLink } from "react-router-dom";
import { ArrowDownLeft, ArrowUpRight, CheckCircle2, Clock, Download, XCircle } from "lucide-react";
import { StatusBadge } from "@/components/app/StatusBadge";
import { Timeline } from "@/components/app/Timeline";
import { dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export const HUB_FLOWS = [
  { key: "import", to: "/ops/customs", end: true },
  { key: "export", to: "/ops/customs/export" },
  { key: "transit", to: "/ops/customs/transit" },
  { key: "ens", to: "/ops/customs/ens" },
];
export const HUB_REFERENCE = [
  { key: "taric", to: "/ops/customs/taric" },
  { key: "licences", to: "/ops/customs/licences" },
  { key: "controls", to: "/ops/customs/controls" },
];

/** Import paths that belong to the import tab even though they are deeper than /ops/customs (declaration detail). */
export function hubKeyFor(pathname) {
  const p = (pathname || "").replace(/\/+$/, "");
  const all = [...HUB_FLOWS, ...HUB_REFERENCE].filter((x) => x.key !== "import");
  const hit = all.find((x) => p === x.to || p.startsWith(`${x.to}/`));
  if (hit) return hit.key;
  return p.startsWith("/ops/customs") ? "import" : null;
}

function Pill({ to, end, label, active }) {
  return (
    <NavLink to={to} end={end} aria-current={active ? "page" : undefined}
      className={cn("inline-flex h-[26px] items-center whitespace-nowrap rounded-full px-3 text-sm font-medium text-ink-secondary hover:text-foreground",
        active && "bg-surface text-foreground shadow-[inset_0_0_0_0.5px_var(--border-default)]")}>
      {label}
    </NavLink>
  );
}

/** Hub row on every customs page: the four flows as a pill group, the reference pages as quiet links. */
export function CustomsHubTabs({ active, className }) {
  const { t } = useT();
  return (
    <nav aria-label={t("cx.hub.label")} className={cn("mb-4 flex flex-wrap items-center gap-x-4 gap-y-2", className)}>
      <div className="inline-flex h-8 max-w-full items-center overflow-x-auto rounded-full bg-secondary p-[3px] no-scrollbar">
        {HUB_FLOWS.map((f) => <Pill key={f.key} to={f.to} end={f.end} label={t(`cx.hub.${f.key}`)} active={active === f.key} />)}
      </div>
      <div className="flex items-center gap-1 overflow-x-auto no-scrollbar">
        <span className="mr-1 hidden text-xs text-ink-tertiary sm:inline">{t("cx.hub.reference")}</span>
        {HUB_REFERENCE.map((f) => (
          <NavLink key={f.key} to={f.to} aria-current={active === f.key ? "page" : undefined}
            className={cn("inline-flex h-7 items-center whitespace-nowrap rounded-full px-2.5 text-sm text-ink-secondary hover:bg-hover hover:text-foreground",
              active === f.key && "bg-hover text-foreground font-medium")}>
            {t(`cx.hub.${f.key}`)}
          </NavLink>
        ))}
      </div>
    </nav>
  );
}

/** The locale's text from a bilingual value: {fi, en}, or obj[`${field}_fi`] / obj[`${field}_en`] / obj[field]. */
export function bi(locale, v, field) {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (field) {
    const a = v[`${field}_${locale}`];
    if (a) return a;
    const other = v[`${field}_${locale === "fi" ? "en" : "fi"}`];
    const plain = v[field];
    if (plain && typeof plain === "object") return bi(locale, plain);
    return plain || other || "";
  }
  return v[locale] || v[locale === "fi" ? "en" : "fi"] || "";
}

/** Reason chips/list from backend reasons [{code, fi, en}] (s2 decide() / s11 controls). */
export function Reasons({ reasons, className, empty }) {
  const { locale } = useT();
  const list = (reasons || []).filter(Boolean);
  if (!list.length) return empty || null;
  return (
    <ul className={cn("flex flex-col gap-1", className)}>
      {list.map((r, i) => (
        <li key={`${r.code || "r"}-${i}`} className="text-sm" title={r.code}>
          {bi(locale, r) || bi(locale, r, "text") || r.code}
        </li>
      ))}
    </ul>
  );
}

/** A 404 from a route that is not installed: say so, show nothing else (never a stand-in value).
 *  any404: for reference endpoints (TARIC, licences, controls) every 404 means "not installed": before the s11 router
 *  exists, /api/customs/taric falls into /api/customs/{decl_id} and answers "declaration not found". */
export function isMissingApi(error, { any404 = false } = {}) {
  if (!error || error.status !== 404) return false;
  return any404 || !error.data || error.data.detail === "Not Found";
}

export function ApiMissing({ className }) {
  const { t } = useT();
  return (
    <div className={cn("rounded-xl border border-dashed px-4 py-3", className)}>
      <p className="text-sm font-medium">{t("cx.api_missing")}</p>
      <p className="text-xs text-ink-tertiary">{t("cx.api_missing_body")}</p>
    </div>
  );
}

/** Formats a TARIC goods code 8712003000 -> "8712 00 30 00". */
export function goodsCode(code) {
  const d = String(code || "").replace(/\D/g, "");
  if (!d) return "—";
  const parts = [d.slice(0, 4), d.slice(4, 6), d.slice(6, 8), d.slice(8, 10)].filter(Boolean);
  return parts.join(" ");
}

// ---------------------------------------------------------------------------------------------
// Filing building blocks shared by export (ECS/AES), transit (NCTS) and ENS (ICS2) detail pages.
// The flow files map their API rows onto these props; nothing here knows a backend field name.
// ---------------------------------------------------------------------------------------------

/** Decision banner at the top of a filing: what happens next and who acts. tone: neutral | success | danger | pending */
export function FilingBanner({ tone = "neutral", icon: Icon, spin, title, body, children }) {
  return (
    <div className={cn("mb-6 flex flex-col gap-3 rounded-xl px-4 py-3 sm:flex-row sm:items-center sm:justify-between",
      tone === "neutral" ? "surface-sunken" : tone === "success" ? "status-success" : tone === "danger" ? "status-failed" : "status-pending")}>
      <div className="flex min-w-0 items-start gap-2.5">
        {Icon && <Icon className={cn("mt-0.5 size-4 shrink-0", spin && "animate-spin")} aria-hidden />}
        <div className="min-w-0">
          <p className="text-sm font-medium">{title}</p>
          {body && <div className="mt-0.5 text-sm opacity-90">{body}</div>}
        </div>
      </div>
      {children && <div className="flex shrink-0 flex-wrap items-center gap-2">{children}</div>}
    </div>
  );
}

/** XSD validation glyph + label: valid / invalid (with error count) / not validated. */
export function XsdBadge({ valid, errors }) {
  const { t } = useT();
  if (valid === true) return <span className="inline-flex items-center gap-1 text-xs text-[color:var(--status-success-fg)]"><CheckCircle2 className="size-3.5" aria-hidden />{t("cx.msg.xsd_ok")}</span>;
  if (valid === false) return <span className="inline-flex items-center gap-1 text-xs text-danger-fg"><XCircle className="size-3.5" aria-hidden />{t("cx.msg.xsd_bad", { n: (errors || []).length || 1 })}</span>;
  return <span className="inline-flex items-center gap-1 text-xs text-ink-tertiary"><Clock className="size-3.5" aria-hidden />{t("cx.msg.xsd_none")}</span>;
}

/**
 * Messages out (to Tulli / EU) and in (responses), newest last.
 * messages: [{id, dir: "out"|"in", type, label, at, valid, errors[], xmlHref, mrn, note, schema}]
 */
export function MessageLog({ messages }) {
  const { t, locale } = useT();
  const [open, setOpen] = React.useState(null);
  if (!(messages || []).length) return <p className="text-sm text-ink-tertiary">{t("cx.msg.none")}</p>;
  return (
    <ol className="flex flex-col divide-y divide-[color:var(--border-subtle)]">
      {messages.map((m) => (
        <li key={m.id} className="py-2">
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
            <span className="inline-flex min-w-0 items-center gap-2">
              {m.dir === "in" ? <ArrowDownLeft className="size-4 shrink-0 text-ink-tertiary" aria-label={t("cx.msg.in")} />
                : <ArrowUpRight className="size-4 shrink-0 text-ink-tertiary" aria-label={t("cx.msg.out")} />}
              <span className="font-mono text-[13px]">{m.type || "—"}</span>
              {m.label && <span className="truncate text-sm text-ink-secondary">{m.label}</span>}
            </span>
            <span className="inline-flex items-center gap-3">
              <XsdBadge valid={m.valid} errors={m.errors} />
              <time className="text-xs tabular text-ink-tertiary" dateTime={m.at}>{m.at ? dateTime(m.at, locale) : ""}</time>
            </span>
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 pl-6 text-xs text-ink-tertiary">
            {m.mrn && <span>MRN <span className="font-mono text-foreground">{m.mrn}</span></span>}
            {m.schema && <span className="font-mono">{m.schema}</span>}
            {m.note && <span className="text-ink-secondary">{m.note}</span>}
            {(m.errors || []).length > 0 && (
              <button type="button" className="text-danger-fg hover:underline" aria-expanded={open === m.id} onClick={() => setOpen(open === m.id ? null : m.id)}>
                {open === m.id ? t("cx.msg.hide_errors") : t("cx.msg.show_errors")}
              </button>
            )}
            {m.xmlHref && (
              <a href={m.xmlHref} className="inline-flex items-center gap-1 text-ink-accent hover:underline" download>
                <Download className="size-3" aria-hidden />{t("cx.msg.xml")}
              </a>
            )}
          </div>
          {open === m.id && (
            <ul className="surface-sunken mt-1 ml-6 max-h-60 overflow-auto rounded-lg px-3 py-2 font-mono text-[11px] leading-4">
              {m.errors.map((e, i) => <li key={i} className="break-words">{typeof e === "string" ? e : e.message || JSON.stringify(e)}</li>)}
            </ul>
          )}
        </li>
      ))}
    </ol>
  );
}

// s2 decide() outcomes -> workflow state.
export const DECISION_STATE = { auto: "success", owner_queue: "in-review", blocked: "failed" };

/**
 * Autopilot / controls decision behind a filing. decision: {outcome, reasons[], approval_id, approval_status, at, agent}
 */
export function DecisionCard({ decision, approvalsPath = "/ops/approvals" }) {
  const { t, locale } = useT();
  if (!decision) return <p className="text-sm text-ink-tertiary">{t("cx.dec.none")}</p>;
  const st = DECISION_STATE[decision.outcome] || "expired";
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge size="sm" state={st} label={t(`cx.dec.${decision.outcome || "unknown"}`)} />
        {decision.at && <span className="text-xs text-ink-tertiary">{dateTime(decision.at, locale)}</span>}
      </div>
      <Reasons reasons={decision.reasons} />
      {decision.approval_id && (
        <Link to={`${approvalsPath}?focus=${decision.approval_id}`} className="text-sm text-ink-accent hover:underline">
          {decision.approval_status && decision.approval_status !== "pending" ? t("cx.dec.open_card_done") : t("cx.dec.open_card")}
        </Link>
      )}
    </div>
  );
}

/** Status history + remaining standard steps (grey). history: [{at, status, note, by}] */
export function FlowTimeline({ history, path, status, label }) {
  const events = (history || []).map((h, i) => ({ id: `${h.at}-${i}`, at: h.at, title: label(h.status), detail: h.note, source: h.by, tone: h.tone }));
  const idx = (path || []).indexOf(status);
  if (idx >= 0) path.slice(idx + 1).forEach((s) => events.push({ id: `f-${s}`, title: label(s), future: true }));
  return <Timeline events={events} />;
}
