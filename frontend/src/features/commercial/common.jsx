// Presentation pieces shared by the Commercial (Myynti) tabs and the credit / insurance panels.
import React from "react";
import { ExternalLink, Lock, PlugZap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingRows } from "@/components/app/states";
import { StatusBadge } from "@/components/app/StatusBadge";
import { money, num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

// Domain value -> one of the nine workflow states. Labels come from com.enum.<group>.<value>.
const VISUAL = {
  stage: { prospect: "scheduled", quoting: "submitted", tender: "submitted", new: "scheduled", contacted: "submitted", replied: "in-review", meeting: "in-progress", qualified: "in-progress",
    quoted: "submitted", negotiation: "in-progress", won: "success", lost: "expired", disqualified: "expired", opted_out: "expired" },
  lead: { new: "scheduled", scored: "scheduled", no_contact: "expired", contactable: "submitted", awaiting_person: "in-review", contacted: "submitted",
    replied: "in-review", qualified: "in-progress", nurture: "pending", converted: "success", disqualified: "expired", opted_out: "expired" },
  tender: { uploaded: "pending", parsed: "pending", draft: "pending", needs_review: "in-review", pricing: "in-progress", priced: "in-review",
    returned: "submitted", submitted: "submitted", awarded: "success", lost: "expired", failed: "failed", cancelled: "expired" },
  tariff: { draft: "pending", scheduled: "scheduled", active: "success", expired: "expired", superseded: "expired", cancelled: "expired" },
  credit: { ok: "success", watch: "pending", hold: "flagged", blocked: "failed", unknown: "expired" },
  provider: { live: "success", live_ready: "pending", not_configured: "expired" },
  policy: { quoted: "scheduled", pending: "in-progress", bound: "success", active: "success", cancelled: "expired", expired: "expired", failed: "failed", declined: "failed" },
};

export function Label({ group, value }) {
  const { t } = useT();
  if (value === null || value === undefined || value === "") return "—";
  const key = `com.enum.${group}.${value}`;
  const s = t(key);
  return s === key ? String(value).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : s;
}

export function useLabel() {
  const { t } = useT();
  return (group, value) => {
    if (value === null || value === undefined || value === "") return "—";
    const key = `com.enum.${group}.${value}`;
    const s = t(key);
    return s === key ? String(value).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : s;
  };
}

/** A lead's state: its opportunity's stage once there is one, otherwise the lead's own status (found, scored, contacted ...). */
export function LeadState({ lead, size }) {
  return lead?.stage ? <ComBadge group="stage" value={lead.stage} size={size} /> : <ComBadge group="lead" value={lead?.status} size={size} />;
}

export function ComBadge({ group, value, size = "sm", className }) {
  const label = useLabel();
  if (!value) return <span className="text-ink-tertiary">—</span>;
  return <StatusBadge size={size} state={VISUAL[group]?.[value] || "expired"} value={value} label={label(group, value)} className={className} />;
}

export const Dash = () => <span className="text-ink-tertiary">—</span>;

/** Money in cents; whole euros above 10 000 €. Missing stays "—". */
export function Eur({ cents, decimals, className }) {
  const { locale } = useT();
  return <span className={cn("tabular whitespace-nowrap", className)}>{money(cents, locale, decimals !== undefined ? { decimals } : undefined)}</span>;
}

/** Score 0–100 as number + 40 px bar; colour stays neutral (the score is not a status). */
export function Score({ value }) {
  const { locale } = useT();
  if (value === null || value === undefined) return <Dash />;
  const v = Math.max(0, Math.min(100, Number(value)));
  return (
    <span className="inline-flex items-center gap-2 whitespace-nowrap">
      <span className="tabular text-sm w-7 text-right">{num(v, locale)}</span>
      <span aria-hidden className="h-1.5 w-10 overflow-hidden rounded-full bg-[var(--bg-subtle)] shadow-[inset_0_0_0_0.5px_var(--border-default)]">
        <span className="block h-full rounded-full bg-[var(--text-secondary)]" style={{ width: `${v}%` }} />
      </span>
    </span>
  );
}

/** Outbound link to an official source (PRH, Eurostat, Vero…). */
export function SourceLink({ href, children, className }) {
  if (!href) return children ? <span className={className}>{children}</span> : null;
  return (
    <a href={href} target="_blank" rel="noreferrer noopener" className={cn("inline-flex items-center gap-1 text-ink-accent hover:underline", className)}>
      {children}<ExternalLink className="size-3" aria-hidden />
    </a>
  );
}

/** Reasons list (score reasons, credit reasons) with their source links. */
export function Reasons({ items, empty }) {
  const { t, locale } = useT();
  if (!items || items.length === 0) return <p className="text-xs text-ink-tertiary">{empty || t("com.no_reasons")}</p>;
  return (
    <ul className="flex flex-col gap-1">
      {items.map((r) => (
        <li key={r.key} className="flex items-baseline gap-2 text-sm">
          <span aria-hidden className="mt-1.5 size-1 shrink-0 rounded-full bg-[var(--text-tertiary)]" />
          <span className="min-w-0 flex-1">
            {r.text}
            {r.points !== null && r.points !== undefined && <span className="ml-1.5 text-xs tabular text-ink-tertiary">{r.points > 0 ? "+" : ""}{num(r.points, locale)}</span>}
            {(r.url || r.source) && <> <SourceLink href={r.url} className="text-xs">{r.source || t("com.source")}</SourceLink></>}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Per-tab state machine with commercial-specific failures: 401 -> sign in, 403 -> which permission, 404/405 -> the
 *  endpoint is not on this server yet (said plainly, never a fake empty list). */
export function ComLoadable({ q, children, isEmpty, empty, perm, skeleton }) {
  const { t } = useT();
  if (q.loading && !q.data) return skeleton || <LoadingRows rows={5} />;
  if (q.error && !q.data) return <ComError error={q.error} onRetry={q.reload} perm={perm} />;
  if (!q.data) return null;
  return (
    <>
      {q.error && <ErrorState className="mb-3" compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
      {isEmpty && isEmpty(q.data) ? empty : children(q.data)}
    </>
  );
}

export function ComError({ error, onRetry, perm, compact }) {
  const { t } = useT();
  const s = error?.status;
  if (s === 401) {
    return <EmptyState icon={Lock} title={t("com.err.401")} action={<Button size="sm" asChild><a href="/login">{t("com.err.sign_in")}</a></Button>} />;
  }
  if (s === 403) return <EmptyState icon={Lock} title={t("com.err.403")} body={perm ? t("com.err.403_perm", { perm }) : null} />;
  if (s === 404 || s === 405 || s === 501) {
    return <EmptyState icon={PlugZap} title={t("com.err.missing")} body={t("com.err.missing_body")} action={onRetry && <Button size="sm" variant="outline" onClick={onRetry}>{t("action.retry")}</Button>} />;
  }
  return <ErrorState error={error} onRetry={onRetry} compact={compact} />;
}

/** One-line "what needs me" banner at the top of a tab. tone: warning | danger | quiet. */
export function NeedsLine({ items, quiet }) {
  const shown = (items || []).filter(Boolean);
  if (!shown.length) return quiet ? <p className="mb-3 text-sm text-ink-secondary">{quiet}</p> : null;
  return (
    <p className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
      {shown.map((it, i) => (
        <span key={i} className={cn("inline-flex items-center gap-1.5", it.tone === "danger" ? "text-danger-fg" : it.tone === "warning" ? "text-warning-fg" : "text-ink-secondary")}>
          {it.icon && <it.icon className="size-4" aria-hidden />}
          {it.onClick ? <button type="button" onClick={it.onClick} className="hover:underline">{it.text}</button> : it.text}
        </span>
      ))}
    </p>
  );
}

/** Wide table wrapper: scrolls inside itself, the first column stays visible (390 px). */
export function ScrollTable({ children, className, label }) {
  return (
    <div role="region" aria-label={label} tabIndex={0}
      className={cn("com-scroll relative overflow-x-auto rounded-lg focus-visible:outline-2 [&_th:first-child]:sticky [&_th:first-child]:left-0 [&_th:first-child]:z-[1] [&_th:first-child]:bg-background [&_td:first-child]:sticky [&_td:first-child]:left-0 [&_td:first-child]:z-[1] [&_td:first-child]:bg-background", className)}>
      {children}
    </div>
  );
}

/** Small neutral chip (rule scope, lane, provenance). */
export function Chip({ children, title, className }) {
  return (
    <span title={title} className={cn("inline-flex h-5 items-center whitespace-nowrap rounded-full bg-secondary px-2 text-[11px] font-medium text-ink-secondary shadow-[inset_0_0_0_0.5px_var(--border-default)]", className)}>
      {children}
    </span>
  );
}

/** Location code + name from {unlocode|country|name}. */
export function Place({ loc }) {
  if (!loc) return <Dash />;
  const code = loc.unlocode || loc.locode || loc.country || null;
  const name = loc.name || loc.city || loc.label || null;
  return (
    <span className="flex min-w-0 flex-col">
      {code && <span className="font-mono text-xs">{code}</span>}
      {name && name !== code && <span className="truncate text-xs text-ink-tertiary">{name}</span>}
      {!code && !name && <Dash />}
    </span>
  );
}

export const COMMERCIAL_TABS = ["pipeline", "tenders", "tariffs", "margins", "credit", "insurance"];
export const COVERS = ["ICC_A", "ICC_B", "ICC_C"];

/** Live / live-ready (waiting for the insurer contract) / not configured: always next to a provider name. */
export function ProviderMark({ status, name }) {
  const s = status || "not_configured";
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {name && <span className="text-sm">{name}</span>}
      <ComBadge group="provider" value={s} />
    </span>
  );
}

/** Errors from mutations: message for toasts, keeping 403 explicit. */
export function mutationError(e, t, locale) {
  if (e?.status === 403) return t("com.err.403");
  if (e?.status === 404 || e?.status === 405) return t("com.err.missing");
  const d = e?.data?.detail;
  if (d && typeof d === "object") return (locale === "fi" && d.message_fi) || d.message || d.message_en || e.message;
  if (typeof d === "string" && e?.status === 409) return d;
  return e?.message || t("state.error");
}
