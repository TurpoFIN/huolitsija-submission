import React from "react";
import { Link } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { HEALTH_STATE, SEVERITY_STATE } from "./shared";

const SEVERITY_ORDER = ["critical", "warning", "info"];

function Row({ label, count, badge, to, onClick }) {
  const body = (
    <>
      <span className="min-w-0 flex-1 text-sm">{label}</span>
      {badge}
      <span className={cn("w-10 text-right text-sm tabular", !count && "text-ink-tertiary")}>{count}</span>
      <ChevronRight className="size-4 shrink-0 text-ink-tertiary" aria-hidden />
    </>
  );
  const cls = "flex min-h-10 w-full items-center gap-3 rounded-md px-2 py-1.5 text-left hover:bg-hover";
  return <li>{to ? <Link to={to} className={cls}>{body}</Link> : <button type="button" onClick={onClick} className={cls}>{body}</button>}</li>;
}

/** What needs the owner now: overdue approvals first, then open alerts, failed jobs and health. */
export function OwnerNow({ status, openTab }) {
  const { t } = useT();
  const q = status.owner_queue || {};
  const alerts = (status.alerts || []).filter((a) => !a.acknowledged_at);
  const worst = SEVERITY_ORDER.find((s) => alerts.some((a) => a.severity === s));
  const dead = status.jobs?.dead_letters_open || 0;
  const health = status.health;
  const notOk = health ? Object.values(health.checks || {}).filter((c) => c.status !== "ok").length : 0;
  const nothing = !q.pending && !alerts.length && !dead && health?.status === "ok";
  return (
    <Section className="max-w-[44rem]" title={t("ap.now.title")} actions={nothing ? <StatusBadge size="sm" state="success" label={t("ap.now.clear")} /> : null}>
      <ul className="-mx-2 flex flex-col">
        <Row label={t("ap.now.approvals")} count={q.pending || 0} to="/ops/approvals"
          badge={q.overdue ? <StatusBadge size="sm" state="failed" label={t("ap.now.overdue", { n: q.overdue })} />
            : q.pending ? <span className="text-xs text-ink-tertiary">{t("ap.now.on_time")}</span> : null} />
        <Row label={t("ap.now.alerts")} count={alerts.length >= 20 ? "20+" : alerts.length} onClick={() => openTab("monitor", "ap-alerts")}
          badge={worst ? <StatusBadge size="sm" state={SEVERITY_STATE[worst]} label={t(`ap.severity.${worst}`)} /> : null} />
        <Row label={t("ap.now.dead")} count={dead} onClick={() => openTab("jobs", "ap-dead")}
          badge={dead ? <StatusBadge size="sm" state="failed" label={t("ap.now.dead_badge")} /> : null} />
        <Row label={t("ap.now.health")} count={health ? notOk : "—"} onClick={() => openTab("monitor", "ap-health")}
          badge={health ? <StatusBadge size="sm" state={HEALTH_STATE[health.status] || "expired"} label={t(`ap.health.${health.status}`)} />
            : <span className="text-xs text-ink-tertiary">{t("ap.health.never")}</span>} />
      </ul>
    </Section>
  );
}
