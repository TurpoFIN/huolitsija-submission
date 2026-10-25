/**
 * <ClaimNotice/> — runs useClaimPending() and shows its state in one line: "Adding your quote…" or the plain error.
 * Never blocks the page under it. No props.
 */
import React from "react";
import { AlertTriangle, Loader2, X } from "lucide-react";
import { useT } from "@/lib/i18n";
import useClaimPending from "./useClaimPending";

export default function ClaimNotice() {
  const { t } = useT();
  const { claiming, error, dismiss } = useClaimPending();
  if (claiming) {
    return (
      <p role="status" className="mb-3 flex items-center gap-2 text-sm text-ink-secondary">
        <Loader2 className="size-4 animate-spin" aria-hidden /> {t("pq.claim.working")}
      </p>
    );
  }
  if (!error) return null;
  return (
    <div role="alert" className="mb-3 flex items-start gap-2 rounded-lg px-3 py-2 text-sm status-pending">
      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1">
        {t("pq.claim.failed")} {error.status === 0 ? t("state.offline") : error.message}
      </span>
      <button type="button" onClick={dismiss} aria-label={t("action.close")} className="rounded-full p-0.5 hover:bg-hover">
        <X className="size-3.5" />
      </button>
    </div>
  );
}
