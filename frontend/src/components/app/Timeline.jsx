import React from "react";
import { cn } from "@/lib/utils";
import { dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";

/** Vertical event list. events: [{id, at, title, detail, source, done, future}] newest last. */
export function Timeline({ events = [], className }) {
  const { t, locale } = useT();
  return (
    <ol className={cn("relative ml-1.5 border-l border-[color:var(--border-default)]", className)}>
      {events.map((e, i) => (
        <li key={e.id || i} className="relative pb-4 pl-4 last:pb-0">
          <span aria-hidden className={cn("absolute -left-[5px] top-1.5 size-2.5 rounded-full border-2 border-[color:var(--bg-surface)]",
            e.future ? "bg-[var(--gray-300)]" : e.tone === "danger" ? "bg-[var(--danger-500)]" : e.tone === "warning" ? "bg-[var(--warning-500)]" : "bg-[var(--text-primary)]")} />
          <div className="flex flex-wrap items-baseline justify-between gap-x-3">
            <p className={cn("text-sm", e.future ? "text-ink-tertiary" : "font-medium")}>{e.title}</p>
            <time className="text-xs text-ink-tertiary tabular" dateTime={e.at}>{e.at ? dateTime(e.at, locale) : ""}{e.estimated ? ` (${t("shipments.ms.estimate")})` : ""}</time>
          </div>
          {(e.detail || e.source) && (
            <p className="text-xs text-ink-tertiary">{e.detail}{e.detail && e.source ? " · " : ""}{e.source}</p>
          )}
        </li>
      ))}
    </ol>
  );
}
