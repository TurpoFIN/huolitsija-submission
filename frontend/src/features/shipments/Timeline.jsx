// Milestone timeline (planned vs actual + source) and the raw tracking-event history.
import React from "react";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { dateTime, durText, msLabel, sourceLabel } from "@/features/shipments/common";

const DANGER = new Set(["exception", "customs_hold", "cancelled"]);
const WARN = new Set(["hold_added", "eta_changed"]);

function sortMilestones(ms) {
  return (ms || [])
    .map((m, i) => ({ ...m, at: m.source === "schedule" ? null : m.at, planned: m.planned || (m.source === "schedule" ? m.at : null), _i: i }))
    .sort((a, b) => {
      const ta = new Date(a.at || a.planned || 0).getTime();
      const tb = new Date(b.at || b.planned || 0).getTime();
      if (!a.at && !a.planned) return 1;
      if (!b.at && !b.planned) return -1;
      return ta - tb || a._i - b._i;
    });
}

/** Done = filled dot + actual time + source; future = hollow dot + "arvio" planned time; overdue = amber "awaiting confirmation". */
export function MilestoneTimeline({ milestones, legs }) {
  const { t, locale } = useT();
  const list = sortMilestones(milestones);
  if (!list.length) return <p className="text-sm text-ink-tertiary">{t("shipments.ms.empty")}</p>;
  const now = Date.now();
  const legName = (seq) => {
    const lg = (legs || []).find((l) => l.seq === seq);
    return lg ? t(`shipments.leg.role.${lg.role || "main"}`) : null;
  };
  return (
    <ol className="relative ml-1.5 border-l border-[color:var(--border-default)]">
      {list.map((m) => {
        const done = !!m.at;
        const overdue = !done && m.planned && new Date(m.planned).getTime() < now;
        const tone = DANGER.has(m.code) ? "danger" : WARN.has(m.code) ? "warning" : null;
        const delta = done && m.planned && m.at ? new Date(m.at).getTime() - new Date(m.planned).getTime() : 0;
        const place = m.location?.name;
        const schedule = done && m.source === "schedule";
        return (
          <li key={`${m.code}-${m._i}`} className="relative pb-3.5 pl-4 last:pb-0">
            <span aria-hidden className={cn("absolute -left-[5px] top-1.5 size-2.5 rounded-full border-2",
              done
                ? cn("border-[color:var(--bg-surface)]", tone === "danger" ? "bg-[var(--danger-500)]" : tone === "warning" ? "bg-[var(--warning-500)]" : schedule ? "bg-[var(--gray-400,var(--text-tertiary))]" : "bg-[var(--text-primary)]")
                : overdue ? "border-[color:var(--warning-500)] bg-[var(--bg-surface)]" : "border-[color:var(--gray-300,var(--border-default))] bg-[var(--bg-surface)]")} />
            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
              <p className={cn("text-sm", done ? "font-medium" : "text-ink-secondary")}>
                {msLabel(t, m)}
                {place && <span className={cn("font-normal", done ? "text-ink-secondary" : "text-ink-tertiary")}> · {place}</span>}
              </p>
              <time className="text-xs tabular text-ink-secondary" dateTime={m.at || m.planned || undefined}>
                {done ? dateTime(m.at, locale) : m.planned ? (
                  <span className={overdue ? "text-warning-fg" : "text-ink-tertiary"}>
                    {dateTime(m.planned, locale)} <span className="italic">({t("shipments.ms.estimate")})</span>
                  </span>
                ) : "—"}
              </time>
            </div>
            <p className="text-xs text-ink-tertiary">
              {[
                done ? sourceLabel(t, m.source) : overdue ? t("shipments.ms.awaiting") : null,
                done && Math.abs(delta) >= 3600000 ? t(delta > 0 ? "shipments.ms.vs_planned_late" : "shipments.ms.vs_planned_early", { n: durText(delta, locale) }) : null,
                done && m.by && m.by !== m.source && !["Schedule", "schedule", "Aikataulu"].includes(m.by) ? t("shipments.ms.by", { name: m.by }) : null,
                m.leg_seq ? legName(m.leg_seq) : null,
              ].filter(Boolean).join(" · ")}
            </p>
            {m.note && <p className="mt-0.5 text-sm text-ink-secondary">{m.note}</p>}
          </li>
        );
      })}
    </ol>
  );
}

/** Every tracking event, newest first (History tab). */
export function EventHistory({ events }) {
  const { t, locale } = useT();
  const list = [...(events || [])].sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime());
  if (!list.length) return <p className="text-sm text-ink-tertiary">{t("shipments.history.empty")}</p>;
  return (
    <ul className="divide-y divide-[color:var(--border-subtle)]">
      {list.map((e) => (
        <li key={e.id} className="flex flex-col gap-0.5 py-2 sm:flex-row sm:items-baseline sm:gap-4">
          <time className="w-36 shrink-0 text-xs tabular text-ink-tertiary" dateTime={e.at}>{dateTime(e.at, locale)}</time>
          <div className="min-w-0 flex-1">
            <p className="text-sm">
              {msLabel(t, e)}
              {e.location?.name && <span className="text-ink-secondary"> · {e.location.name}</span>}
            </p>
            {e.note && <p className="text-sm text-ink-secondary">{e.note}</p>}
          </div>
          <span className="text-xs text-ink-tertiary">{[sourceLabel(t, e.source), e.by !== e.source ? e.by : null].filter(Boolean).join(" · ")}</span>
        </li>
      ))}
    </ul>
  );
}
