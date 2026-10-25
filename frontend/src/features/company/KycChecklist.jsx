// KYC / customs-readiness checklist with real register results and their provenance.
import React from "react";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { CUSTOMS_CHECKS, CheckBadge, RISK_CHECKS, SourceLabel, checkSummary, shortSource } from "./common";
import { ListVersions, ScreeningHits } from "./ScreeningHits";

function CheckSummary({ c, audience, running, extra }) {
  const { t, locale } = useT();
  // The API already masks sanctions details for customers; this guards against an unmasked payload.
  const sanctionsHidden = audience === "customer" && c.key === "sanctions" && c.hits?.length > 0;
  return (
    <div className={cn("min-w-0", running && "opacity-60")}>
      <p className="text-sm text-ink-secondary break-words">
        {sanctionsHidden ? t("company.sanctions.review_customer") : checkSummary(c, locale) || <span className="text-ink-tertiary">—</span>}
      </p>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5">
        {c.checked_at && <SourceLabel source={c.key === "sanctions" ? null : shortSource(c.source)} at={c.checked_at} url={c.url} withTime />}
        {c.key === "sanctions" && c.lists?.length > 0 && <ListVersions lists={c.lists} prefix={t("company.checks.lists")} />}
      </div>
      {audience === "ops" && c.key === "sanctions" && c.hits?.length > 0 && <ScreeningHits hits={c.hits} className="mt-2" />}
      {extra}
    </div>
  );
}

function CheckRow({ c, audience, running, renderExtra }) {
  const { t } = useT();
  const title = t(`company.check.${c.key}`);
  const extra = renderExtra?.(c) || null;
  return (
    <li className="py-2.5" data-check={c.key} data-status={c.status}>
      <div className="flex items-start justify-between gap-3 md:grid md:grid-cols-[132px_176px_minmax(0,1fr)] md:justify-normal md:gap-4">
        <div className="order-2 shrink-0 md:order-1"><CheckBadge status={c.status} running={running} audience={audience} /></div>
        <p className="order-1 text-sm font-medium md:order-2">{title}</p>
        <div className="hidden md:order-3 md:block"><CheckSummary c={c} audience={audience} running={running} extra={extra} /></div>
      </div>
      <div className="mt-1 md:hidden"><CheckSummary c={c} audience={audience} running={running} extra={extra} /></div>
    </li>
  );
}

function Group({ title, hint, checks, audience, running, renderExtra }) {
  if (!checks.length) return null;
  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-x-2 pt-1">
        <h3 className="text-xs font-medium text-ink-secondary">{title}</h3>
        {hint && <span className="text-xs text-ink-tertiary">{hint}</span>}
      </div>
      <ul className="divide-y divide-[color:var(--border-subtle)]">
        {checks.map((c) => <CheckRow key={c.key} c={c} audience={audience} running={running} renderExtra={renderExtra} />)}
      </ul>
    </div>
  );
}

/**
 * checklist: [{key, status, summary, checked_at, source, url, hits, lists}] from the API.
 * audience "customer" hides sanctions hit details (no tipping-off); "ops" shows them.
 */
export function KycChecklist({ checklist = [], audience = "customer", running, renderExtra }) {
  const { t } = useT();
  const by = Object.fromEntries(checklist.map((c) => [c.key, c]));
  const pick = (keys) => keys.map((k) => by[k]).filter(Boolean);
  return (
    <div className="flex flex-col gap-3">
      <Group title={t("company.group.verification")} hint={t("company.group.verification_hint")} checks={pick(RISK_CHECKS)} audience={audience} running={running} renderExtra={renderExtra} />
      <Group title={t("company.group.customs")} hint={t("company.group.customs_hint")} checks={pick(CUSTOMS_CHECKS)} audience={audience} running={running} renderExtra={renderExtra} />
    </div>
  );
}

/** "Last checked 3 min ago" + Run button, for Section actions. */
export function RunChecksAction({ checkedAt, running, onRun, canRun, disabledReason, label }) {
  const { t, locale } = useT();
  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <span className="text-xs text-ink-tertiary" aria-live="polite">
        {running ? t("company.checks.running") : checkedAt ? t("company.checks.last_run", { time: relTime(checkedAt, locale) }) : t("company.checks.never")}
      </span>
      {canRun && (
        <Button size="sm" variant={checkedAt ? "outline" : "default"} onClick={onRun} disabled={running || !!disabledReason} title={disabledReason || undefined}>
          <RefreshCw className={cn("size-3.5", running && "animate-spin")} />
          {label || (checkedAt ? t("company.checks.rerun") : t("company.checks.run"))}
        </Button>
      )}
    </div>
  );
}

export function checksNeverRun(checklist) {
  return !checklist?.length || checklist.every((c) => c.status === "not_run");
}
