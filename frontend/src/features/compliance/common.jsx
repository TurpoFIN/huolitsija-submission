// Shared pieces for the compliance page and privacy panels.
// ApiFail shows the API's own answer (HTTP status + detail) next to the translated message, so a missing or failing
// backend is visible as such and never looks like "no data". Block is Loadable with that error view.
import React from "react";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/app/StatusBadge";
import { LoadingRows } from "@/components/app/states";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { label, rawDetail } from "./logic";

export { label, rawDetail };

/** Failure in place: translated message, then "HTTP 404 · Not Found" exactly as the server said it. */
export function ApiFail({ error, onRetry, compact, className, what }) {
  const { t } = useT();
  if (!error) return null;
  const status = error.status;
  const detail = rawDetail(error);
  const routeMissing = status === 404 && (!detail || detail === "Not Found");
  const msg = routeMissing ? t("compliance.not_available") : status === 0 ? t("review.offline") : error.message || t("state.error");
  return (
    <div role="alert" className={cn("flex items-start gap-3 rounded-lg px-3 py-2 status-failed", compact ? "text-xs" : "text-sm", className)}>
      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="block">{what ? <span className="font-medium">{what}: </span> : null}{msg}</span>
        {status > 0 && (
          <span className="mt-0.5 block font-mono text-[11px] opacity-80">
            {t("compliance.http", { status })}{detail && detail !== msg ? ` · ${detail}` : ""}
          </span>
        )}
      </span>
      {onRetry && <Button size="xs" variant="outline" onClick={onRetry}>{t("action.retry")}</Button>}
    </div>
  );
}

/** Loading / failed / empty / data for one useApi() result, keeping last good data on a failed refresh. */
export function Block({ q, children, empty, isEmpty, rows = 4 }) {
  if (q.loading && !q.data) return <LoadingRows rows={rows} />;
  if (q.error && !q.data) return <ApiFail error={q.error} onRetry={q.reload} />;
  if (!q.data) return null;
  return (
    <>
      {q.error && <ApiFail className="mb-2" compact error={q.error} onRetry={q.reload} />}
      {isEmpty && isEmpty(q.data) ? empty : children(q.data)}
    </>
  );
}

// Screening outcome -> one of the nine workflow states (StatusBadge VISUAL); no new colour meanings.
const SCREEN_STATE = { clear: "success", potential_match: "flagged", error: "failed", pending: "in-progress",
  cleared: "success", false_positive: "success", blocked: "failed" };

export function ScreenStatus({ value, size = "sm" }) {
  const { t } = useT();
  return <StatusBadge size={size} state={SCREEN_STATE[value] || "expired"} label={label(t, "compliance.status", value)} />;
}

/** Plain hairline list used for compact key facts in expanded rows. */
export function Hairline({ children, className }) {
  return <ul className={cn("flex flex-col divide-y divide-[color:var(--border-subtle)] rounded-lg border border-[color:var(--border-subtle)]", className)}>{children}</ul>;
}

export const fieldCls = "mt-1 h-8 w-full rounded-md border bg-background px-2 text-md md:text-sm";
