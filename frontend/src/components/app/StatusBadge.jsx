import React from "react";
import { AlertTriangle, CalendarClock, CheckCircle2, CircleDashed, Clock, Eye, Flag, Send, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { tEnum, useT } from "@/lib/i18n";

// The nine workflow states (Factory IMPLEMENTATION.md) and their glyphs. Never invent new meanings.
const VISUAL = {
  pending: { cls: "status-pending", Icon: AlertTriangle },
  "in-progress": { cls: "status-in-progress", Icon: CircleDashed },
  submitted: { cls: "status-submitted", Icon: Send },
  "in-review": { cls: "status-in-review", Icon: Eye },
  scheduled: { cls: "status-scheduled", Icon: CalendarClock },
  flagged: { cls: "status-flagged", Icon: Flag },
  success: { cls: "status-success", Icon: CheckCircle2 },
  failed: { cls: "status-failed", Icon: XCircle },
  expired: { cls: "status-expired", Icon: Clock },
};

// Domain value -> visual state, per enum group. i18n label key: enum.<group>.<value>.
export const STATE_MAP = {
  status: { draft: "expired", booked: "scheduled", picked_up: "in-progress", in_transit: "in-progress", at_hub: "in-progress",
    customs_hold: "pending", out_for_delivery: "in-progress", delivered: "success", exception: "failed", cancelled: "expired" },
  quote: { draft: "expired", pending_approval: "in-review", sent: "submitted", accepted: "success", booked: "success",
    expired: "expired", rejected: "failed" },
  customs: { draft: "pending", needs_info: "pending", ready_for_review: "in-review", approved: "scheduled", submitted: "submitted",
    accepted: "in-progress", rejected: "failed", under_control: "flagged", released: "success", not_released: "failed",
    invalidated: "expired", closed: "expired" },
  invoice: { draft: "expired", open: "scheduled", processing: "in-progress", paid: "success", overdue: "pending", void: "expired", credited: "expired" },
  kyc: { not_started: "pending", pending: "in-progress", in_review: "in-review", approved: "success", rejected: "failed" },
  approval: { pending: "in-review", approved: "success", rejected: "failed", cancelled: "expired" },
  exception: { open: "flagged", in_progress: "in-progress", resolved: "success" },
  severity: { low: "expired", medium: "pending", high: "flagged", critical: "failed" },
  claim: { draft: "expired", submitted: "submitted", in_review: "in-review", approved: "success", rejected: "failed", paid: "success" },
  extraction: { pending: "in-progress", processing: "in-progress", done: "success", needs_review: "in-review", failed: "failed" },
  generic: { ok: "success", warning: "pending", error: "failed", info: "in-progress" },
};

export function StatusBadge({ group = "status", value, label, size = "default", icon = true, className, state }) {
  const { t } = useT();
  const visual = VISUAL[state || STATE_MAP[group]?.[value] || "expired"];
  const Icon = visual.Icon;
  const text = label ?? tEnum(t, group, value);
  return (
    <span
      data-state={value}
      className={cn(
        "inline-flex items-center rounded-full font-medium whitespace-nowrap",
        size === "sm" ? "h-5 px-2 gap-1 text-[11px]" : "h-6 px-2.5 gap-1.5 text-xs",
        visual.cls,
        className
      )}
    >
      {icon && <Icon className={size === "sm" ? "size-3" : "size-3.5"} aria-hidden />}
      {text}
    </span>
  );
}

/** Small violet provenance chip: this value came from an AI agent. */
export function AgentMark({ label, title, className }) {
  const { t } = useT();
  return (
    <span title={title || t("ai.drafted")} className={cn("agent-mark inline-flex h-5 items-center rounded-full px-1.5 text-[11px] font-medium", className)}>
      {label || t("ai.badge")}
    </span>
  );
}
