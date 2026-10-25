// Putki: what the sales agent is doing, which replies need a person, and the funnel by stage.
import React, { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { MessageSquareReply, Search } from "lucide-react";
import { EmptyState } from "@/components/app/states";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar } from "@/components/app/FilterBar";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { useDebounced, useIsNarrow } from "@/features/quotes/ops/common";
import { AgentStrip } from "./AgentStrip";
import { LeadSheet } from "./LeadSheet";
import { ComLoadable, Eur, LeadState, NeedsLine, Score, SourceLink, useLabel } from "./common";
import { isOpenStage, lead as normLead, listOf, pipelineStages, pipelineTotals } from "./shapes";

const needsMe = (l) => l.stage === "replied" || (l.replied && isOpenStage(l.stage) && l.stage !== "won");

export default function PipelineTab() {
  const { t, locale } = useT();
  const label = useLabel();
  const { can } = useAuth();
  const canWrite = can("customers.write");
  const narrow = useIsNarrow();
  const [params, setParams] = useSearchParams();
  const stage = params.get("stage") || null;
  const openId = params.get("lead");
  const [search, setSearch] = useState("");
  const qText = useDebounced(search.trim(), 250);
  const setParam = (k, v) => {
    const next = new URLSearchParams(params);
    if (!v) next.delete(k); else next.set(k, v);
    setParams(next, { replace: true });
  };

  const status = useApi("/crm/status", { pollMs: 60000 });
  const pipe = useApi("/crm/pipeline", { pollMs: 60000 });
  const qs = new URLSearchParams();
  if (stage) qs.set("stage", stage);
  if (qText) qs.set("q", qText);
  qs.set("limit", "200");
  const leads = useApi(`/crm/leads?${qs}`, { pollMs: 60000 });

  const stages = useMemo(() => pipelineStages(pipe.data), [pipe.data]);
  const totals = pipelineTotals(pipe.data, stages);
  const rows = useMemo(() => listOf(leads.data, "leads").map((l) => normLead(l, locale))
    .sort((a, b) => Number(needsMe(b)) - Number(needsMe(a)) || (b.score ?? -1) - (a.score ?? -1)), [leads.data, locale]);
  const replied = stages.find((s) => s.stage === "replied")?.count ?? rows.filter(needsMe).length;
  const reload = () => { status.reload(); pipe.reload(); leads.reload(); };

  const columns = [
    { key: "name", header: t("com.lead.company"), sort: (l) => l.name.toLowerCase(), cell: (l) => (
      <div className="min-w-0 max-w-[260px]">
        <div className="flex items-center gap-2">
          <span className="truncate font-medium">{l.name}</span>
          {needsMe(l) && <MessageSquareReply className="size-3.5 text-warning-fg" aria-label={t("com.pipe.replied_mark")} />}
        </div>
        <span className="text-xs text-ink-tertiary"><span className="font-mono">{l.business_id || "—"}</span>{l.city ? ` · ${l.city}` : ""}</span>
      </div>
    ) },
    { key: "stage", header: t("field.status"), cell: (l) => <LeadState lead={l} /> },
    { key: "score", header: t("com.lead.score"), sort: (l) => l.score, cell: (l) => <Score value={l.score} /> },
    { key: "why", header: t("com.lead.why"), sortable: false, hideBelow: "lg", cell: (l) => (
      <span className="line-clamp-2 max-w-[320px] text-xs text-ink-secondary">{l.score_reasons.slice(0, 2).map((r) => r.text).join(" · ") || "—"}</span>
    ) },
    { key: "source", header: t("com.lead.source"), sortable: false, hideBelow: "md", cell: (l) => (
      <span className="flex flex-col text-xs">
        <SourceLink href={l.source_url}>{label("src", l.source_kind || "prh")}</SourceLink>
        {l.trade_url && <SourceLink href={l.trade_url}>{t("com.src.eurostat")}</SourceLink>}
      </span>
    ) },
    { key: "next_follow_up_at", header: t("com.lead.next"), hideBelow: "md", cell: (l) => <span className="whitespace-nowrap text-xs">{l.next_follow_up_at ? date(l.next_follow_up_at, locale) : "—"}</span> },
    { key: "value_cents", header: t("com.lead.value"), align: "right", cell: (l) => <Eur cents={l.value_cents} /> },
  ];

  return (
    <div>
      <AgentStrip q={status} canRun={canWrite} onRan={reload} />
      <NeedsLine items={[
        replied > 0 && { tone: "warning", icon: MessageSquareReply, text: t("com.pipe.replies_n", { n: num(replied, locale) }), onClick: () => setParam("stage", "replied") },
      ]} quiet={pipe.data ? t("com.pipe.nothing_waiting") : null} />

      <ComLoadable q={pipe} perm="customers.write" skeleton={<div className="mb-4 h-16 animate-pulse rounded-lg bg-[var(--bg-subtle)]" />}>
        {() => (
          <div className="mb-4">
            <p className="mb-2 text-sm">
              <span className="tabular">{num(totals.open_count, locale)}</span> <span className="text-ink-tertiary">{t("com.pipe.open")}</span>
              {totals.open_value_cents !== null && <> · <Eur cents={totals.open_value_cents} /> <span className="text-ink-tertiary">{t("com.pipe.est_annual")}</span></>}
            </p>
            <ol aria-label={t("com.pipe.stages")} className="flex gap-1.5 overflow-x-auto no-scrollbar pb-1">
              <li>
                <StageButton active={!stage} onClick={() => setParam("stage", null)} label={t("com.all")} count={stages.reduce((a, s) => a + s.count, 0)} />
              </li>
              {stages.map((s) => (
                <li key={s.stage}>
                  <StageButton active={stage === s.stage} onClick={() => setParam("stage", s.stage)} label={label("stage", s.stage)}
                    count={s.count} value={s.value_cents} warn={s.stage === "replied" && s.count > 0} />
                </li>
              ))}
            </ol>
          </div>
        )}
      </ComLoadable>

      <FilterBar search={search} onSearch={setSearch} placeholder={t("com.pipe.search")} />
      <ComLoadable q={leads} perm="customers.write" isEmpty={() => rows.length === 0}
        empty={stage || qText ? (
          <EmptyState icon={Search} title={t("com.pipe.empty_filtered")} action={<Button size="sm" variant="outline" onClick={() => { setSearch(""); setParam("stage", null); }}>{t("com.clear_filters")}</Button>} />
        ) : (
          <EmptyState title={t("com.pipe.empty")} body={t("com.pipe.empty_body")} />
        )}>
        {() => (narrow ? (
          <ul className="divide-y divide-[color:var(--border-subtle)] border-y">
            {rows.map((l) => (
              <li key={l.id}>
                <button type="button" onClick={() => setParam("lead", l.id)} className="flex w-full flex-col gap-1 px-1 py-3 text-left hover:bg-hover">
                  <span className="flex w-full items-start justify-between gap-3">
                    <span className="min-w-0">
                      <span className="flex items-center gap-1.5 truncate text-sm font-medium">{l.name}
                        {needsMe(l) && <MessageSquareReply className="size-3.5 shrink-0 text-warning-fg" aria-label={t("com.pipe.replied_mark")} />}</span>
                      <span className="block text-xs text-ink-tertiary"><span className="font-mono">{l.business_id || "—"}</span>{l.city ? ` · ${l.city}` : ""}</span>
                    </span>
                    <LeadState lead={l} />
                  </span>
                  <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-secondary">
                    <Score value={l.score} />
                    {l.value_cents !== null && <Eur cents={l.value_cents} />}
                    {l.next_follow_up_at && <span>{t("com.lead.next")}: {date(l.next_follow_up_at, locale)}</span>}
                  </span>
                  {l.score_reasons[0] && <span className="line-clamp-1 text-xs text-ink-tertiary">{l.score_reasons[0].text}</span>}
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <div className="overflow-x-auto">
            <DataTable columns={columns} rows={rows} onRowClick={(l) => setParam("lead", l.id)} dense
              rowClassName={(l) => cn(needsMe(l) && "bg-[var(--bg-subtle)]")} pageSize={50} />
          </div>
        ))}
      </ComLoadable>

      <LeadSheet id={openId} open={!!openId} onOpenChange={(o) => !o && setParam("lead", null)} onChanged={reload} canWrite={canWrite} />
    </div>
  );
}

function StageButton({ active, onClick, label, count, value, warn }) {
  const { locale } = useT();
  return (
    <button type="button" onClick={onClick} aria-pressed={active}
      className={cn("flex min-w-[96px] flex-col items-start rounded-lg px-2.5 py-1.5 text-left shadow-[inset_0_0_0_0.5px_var(--border-default)] hover:bg-hover",
        active && "bg-secondary shadow-[inset_0_0_0_1px_var(--border-strong)]")}>
      <span className={cn("whitespace-nowrap text-xs", warn ? "text-warning-fg font-medium" : "text-ink-secondary")}>{label}</span>
      <span className="flex items-baseline gap-1.5">
        <span className="tabular text-sm">{num(count, locale)}</span>
        {value !== null && value !== undefined && <Eur cents={value} className="text-[11px] text-ink-tertiary" />}
      </span>
    </button>
  );
}
