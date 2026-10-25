import React from "react";
import { useT } from "@/lib/i18n";
import { Link } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

/** Page title row: one title, the decision-relevant meta, actions on the right. */
export function PageHeader({ title, meta, actions, crumbs, className }) {
  const { t } = useT();
  return (
    <div className={cn("mb-4 flex flex-col gap-2", className)}>
      {crumbs && crumbs.length > 0 && (
        <nav aria-label={t("review.breadcrumb")} className="flex items-center gap-1 text-xs text-ink-tertiary">
          {crumbs.map((c, i) => (
            <React.Fragment key={i}>
              {i > 0 && <ChevronRight className="size-3" aria-hidden />}
              {c.to ? <Link to={c.to} className="hover:text-foreground">{c.label}</Link> : <span>{c.label}</span>}
            </React.Fragment>
          ))}
        </nav>
      )}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl leading-7 truncate">{title}</h1>
          {meta && <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-ink-secondary">{meta}</div>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}

/** Titled block separated by a hairline, no card chrome. */
export function Section({ title, actions, children, className, id }) {
  return (
    <section id={id} className={cn("mb-6", className)}>
      {(title || actions) && (
        <div className="mb-2 flex items-center justify-between gap-3 border-b border-[color:var(--border-subtle)] pb-2">
          {title && <h2 className="text-sm font-medium">{title}</h2>}
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

/** Label/value grid. items: [{label, value, hint, mono}] — missing values stay visible as "—". */
export function KV({ items, cols = 2, className }) {
  return (
    <dl className={cn("grid gap-x-6 gap-y-3", cols === 3 ? "sm:grid-cols-3" : cols === 1 ? "grid-cols-1" : "sm:grid-cols-2", className)}>
      {items.filter(Boolean).map((it, i) => (
        <div key={i} className="min-w-0">
          <dt className="text-xs text-ink-tertiary">{it.label}</dt>
          <dd className={cn("mt-0.5 text-sm break-words", it.mono && "font-mono text-[13px]", it.className)}>
            {it.value === null || it.value === undefined || it.value === "" ? <span className="text-ink-tertiary">—</span> : it.value}
            {it.hint && <span className="ml-1.5 text-xs text-ink-tertiary">{it.hint}</span>}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Inline metric: value first, label after. No giant KPI tiles. */
export function Metric({ label, value, tone, to, className }) {
  const body = (
    <span className={cn("inline-flex items-baseline gap-1.5", className)}>
      <span className={cn("text-md tabular", tone === "danger" && "text-danger-fg", tone === "warning" && "text-warning-fg")}>{value}</span>
      <span className="text-sm text-ink-tertiary">{label}</span>
    </span>
  );
  return to ? <Link to={to} className="rounded-md hover:bg-hover px-1 -mx-1">{body}</Link> : body;
}
