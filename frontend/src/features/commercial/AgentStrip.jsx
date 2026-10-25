// Sales agent status: is the agent allowed to act, how much of today's send budget is used, when it last searched.
import React, { useState } from "react";
import { Link } from "react-router-dom";
import { Play } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/app/StatusBadge";
import { apiPost } from "@/lib/api";
import { num, relTime, dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { ComError, mutationError } from "./common";
import { crmStatus } from "./shapes";

export function AgentStrip({ q, onRan, canRun }) {
  const { t, locale } = useT();
  const [busy, setBusy] = useState(false);
  if (q.error && !q.data) return <div className="mb-4"><ComError error={q.error} onRetry={q.reload} perm="customers.write" compact /></div>;
  const s = crmStatus(q.data);
  const run = async () => {
    setBusy(true);
    try {
      const r = await apiPost("/crm/prospect/run", {});
      toast.success(t("com.agent.ran_n", { n: num(r?.found ?? 0, locale), sent: num(r?.sent ?? 0, locale), drafts: num(r?.drafts ?? 0, locale) }));
      onRan?.(r);
    } catch (e) {
      toast.error(mutationError(e, t, locale));
    } finally {
      setBusy(false);
    }
  };
  const lc = s?.last_cycle || {};
  const lcBits = [
    lc.found !== undefined ? t("com.agent.found_n", { n: num(lc.found, locale) }) : null,
    lc.sent ? t("com.agent.sent_n", { n: num(lc.sent, locale) }) : null,
    lc.drafts ? t("com.agent.drafts_n", { n: num(lc.drafts, locale) }) : null,
    lc.held_limit ? t("com.agent.held_n", { n: num(lc.held_limit, locale) }) : null,
  ].filter(Boolean);
  const over = s && s.daily_limit && s.sends_today >= s.daily_limit;
  return (
    <section aria-label={t("com.agent.title")} aria-busy={q.loading || undefined}
      className="mb-4 flex flex-wrap items-center gap-x-5 gap-y-2 rounded-xl px-3 py-2.5 shadow-[inset_0_0_0_0.5px_var(--border-default)]">
      <span className="flex items-center gap-2 text-sm">
        <span className="text-ink-tertiary">{t("com.agent.autopilot")}</span>
        {!s ? <span className="text-ink-tertiary">…</span> : s.halted ? (
          <StatusBadge size="sm" state="failed" label={t("com.agent.halted")} />
        ) : s.autopilot === null ? <span className="text-ink-tertiary">—</span> : (
          <StatusBadge size="sm" state={s.autopilot ? "success" : "expired"} label={s.autopilot ? t("com.agent.on") : t("com.agent.off")} />
        )}
      </span>
      <span className="text-sm">
        <span className="text-ink-tertiary">{t("com.agent.sends_today")} </span>
        <span className={over ? "tabular text-warning-fg" : "tabular"}>{s ? `${num(s.sends_today, locale)} / ${num(s.daily_limit, locale)}` : "…"}</span>
      </span>
      <span className="text-sm">
        <span className="text-ink-tertiary">{t("com.agent.last_cycle")} </span>
        {s?.last_cycle_at ? <time dateTime={s.last_cycle_at} title={dateTime(s.last_cycle_at, locale)}>{relTime(s.last_cycle_at, locale)}</time> : <span>{s ? t("com.agent.never") : "…"}</span>}
        {lcBits.length > 0 && <span className="text-ink-tertiary"> · {lcBits.join(" · ")}</span>}
      </span>
      {s?.sandbox_domain ? <span className="text-xs text-warning-fg">{t("com.agent.sandbox", { domain: s.sandbox_domain })}</span>
        : s?.email_provider === "log" ? <span className="text-xs text-warning-fg">{t("com.agent.log_only")}</span> : null}
      {s && s.autopilot === false && !s.halted && <span className="text-xs text-ink-tertiary">{t("com.agent.drafts_wait")}</span>}
      <span className="ml-auto flex items-center gap-2">
        <Button variant="link" size="sm" asChild><Link to="/ops/settings">{t("com.agent.settings")}</Link></Button>
        {canRun && (
          <Button size="sm" variant="outline" onClick={run} disabled={busy || !s || s.halted} aria-busy={busy || undefined}>
            <Play />{busy ? t("com.agent.running") : t("com.agent.run_now")}
          </Button>
        )}
      </span>
    </section>
  );
}
