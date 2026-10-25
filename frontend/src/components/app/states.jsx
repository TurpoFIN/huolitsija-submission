import React from "react";
import { Link, useLocation } from "react-router-dom";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useT } from "@/lib/i18n";
import { relTime } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Empty: say what is empty and offer the relevant action. */
export function EmptyState({ title, body, action, icon: Icon, className }) {
  return (
    <div className={cn("flex flex-col items-start gap-2 rounded-xl border border-dashed px-5 py-8", className)}>
      {Icon && <Icon className="size-5 text-ink-tertiary" aria-hidden />}
      <p className="text-sm font-medium">{title}</p>
      {body && <p className="text-sm text-ink-tertiary max-w-prose">{body}</p>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}

/** Local failure with Retry. Keeps the rest of the screen usable. */
export function ErrorState({ error, onRetry, className, compact }) {
  const { t } = useT();
  const loc = useLocation();
  const missing = error?.status === 404;
  const parent = loc.pathname.split("/").slice(0,-1).join("/") || "/";
  const msg = missing ? t("review.not_found") : error?.status === 0 ? t("review.offline") : error?.message || t("state.error");
  return (
    <div role="alert" className={cn("flex items-center gap-3 rounded-lg px-3 py-2 status-failed", compact ? "text-xs" : "text-sm", className)}>
      <AlertTriangle className="size-4 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1">{msg}</span>
      {missing ? <Button size="xs" variant="outline" asChild><Link to={parent}>{t("review.back")}</Link></Button> : onRetry && (
        <Button size="xs" variant="outline" onClick={onRetry}>
          {t("action.retry")}
        </Button>
      )}
    </div>
  );
}

/** Bounded skeleton rows for first load. */
export function LoadingRows({ rows = 6, className }) {
  return (
    <div className={cn("flex flex-col gap-2 py-2", className)} aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-8 w-full rounded-md" style={{ opacity: 1 - i * 0.12 }} />
      ))}
    </div>
  );
}

/**
 * Renders the standard state machine for a useApi() result:
 * first load -> skeleton; failed first load -> error + retry; failed refresh -> data + inline error.
 */
export function Loadable({ q, children, skeleton, empty, isEmpty }) {
  const { t } = useT();
  if (q.loading && !q.data) return skeleton || <LoadingRows />;
  if (q.error && !q.data) return <ErrorState error={q.error} onRetry={q.reload} />;
  if (!q.data) return null;
  const emptyNow = isEmpty ? isEmpty(q.data) : false;
  return (
    <>
      {q.error && q.data && <ErrorState className="mb-3" compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
      {emptyNow ? empty : children(q.data)}
    </>
  );
}

/** "Updated 2 min ago" + refresh: freshness is shown independently of a new attempt. */
export function Freshness({ at, onRefresh, refreshing }) {
  const { t, locale } = useT();
  if (!at) return null;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink-tertiary">
      {t("state.updated", { time: relTime(at.toISOString ? at.toISOString() : at, locale) })}
      {onRefresh && (
        <button type="button" onClick={onRefresh} className="rounded-full p-1 hover:bg-hover" aria-label={t("action.refresh")}>
          <RefreshCw className={cn("size-3.5", refreshing && "animate-spin")} />
        </button>
      )}
    </span>
  );
}
