// Provenance and confidence for one extracted value: AI (violet), Demo (neutral), Edited (person).
import { useAuth } from "@/lib/auth";
import React from "react";
import { AlertTriangle, PencilLine } from "lucide-react";
import { AgentMark } from "@/components/app/StatusBadge";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { LOW_CONFIDENCE, pctText } from "@/features/documents/fields";

export function ProvenanceChip({ source, className }) {
  const { t } = useT();
  const { user } = useAuth();
  if (source === "ai") return <AgentMark label={t("doc.prov.ai")} title={t("doc.prov.ai_title")} className={className} />;
  if (source === "user") {
    return (
      <span title={t("doc.prov.user_title")} className={cn("inline-flex h-5 items-center gap-1 rounded-full bg-secondary px-1.5 text-[11px] font-medium text-ink-secondary", className)}>
        <PencilLine className="size-3" aria-hidden />
        {t("doc.prov.user")}
      </span>
    );
  }
  if (source === "seed" && user?.org_type === "internal") {
    return (
      <span title={t("doc.prov.seed_title")} className={cn("inline-flex h-5 items-center rounded-full px-1.5 text-[11px] font-medium text-ink-tertiary shadow-[inset_0_0_0_0.5px_var(--border-default)]", className)}>
        {t("doc.prov.seed")}
      </span>
    );
  }
  return null;
}

/** Confidence as a right-aligned percentage; below 70 % it turns warning-toned and says why. Null -> "—". */
export function Confidence({ value, className }) {
  const { t, locale } = useT();
  if (value === null || value === undefined) return <span className={cn("tabular text-xs text-ink-tertiary", className)}>—</span>;
  const low = value < LOW_CONFIDENCE;
  return (
    <span className={cn("inline-flex items-center gap-1 tabular text-xs", low ? "text-warning-fg" : "text-ink-tertiary", className)}
      title={low ? t("doc.review.low", { pct: pctText(value, locale) }) : t("doc.review.confidence")}>
      {low && <AlertTriangle className="size-3" aria-hidden />}
      {pctText(value, locale)}
    </span>
  );
}
