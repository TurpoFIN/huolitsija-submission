import { aiErrorInfo, aiRunGroups, aiRunLabel, agentLabel as labelAgent, recordLabel } from "@/lib/aiDisplay";
import React, { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { PageHeader, Section, KV } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Loadable } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { dateTime, num } from "@/lib/format";
import { useT } from "@/lib/i18n";

export default function AiRuns() {
  const { t, locale } = useT();
  const [params, setParams] = useSearchParams();
  const agent = params.get("agent");
  const status = params.get("status");
  // filters live in the URL so links like /ops/ai?agent=quoting_agent open filtered
  const setFilter = (k, v) => setParams((p) => { const n = new URLSearchParams(p); v ? n.set(k, v) : n.delete(k); return n; }, { replace: true });
  const setAgent = (v) => setFilter("agent", v);
  const setStatus = (v) => setFilter("status", v);
  const [sel, setSel] = useState(null);
  const q = useApi(`/ai-runs?limit=200${agent ? `&agent=${agent}` : ""}${status ? `&status=${status}` : ""}`, { pollMs: 30000 });
  const agentLabel = a => labelAgent(a,t);
  const renderRuns = (rows) => (
<DataTable rows={rows} onRowClick={setSel} empty={<EmptyState title={t("ai.empty")} />} columns={[
              { key: "created_at", header: t("field.created"), cell: (r) => dateTime(r.created_at, locale) },
              { key: "agent", header: t("ai.agent"), cell: (r) => agentLabel(r.agent) },
              { key: "status", header: t("field.status"), cell: (r) => <StatusBadge size="sm" group="generic" value={r.status === "ok" ? "ok" : r.status === "error" ? "error" : "info"} label={t(`review.ai.${r.status}`)} /> },
              { key: "model", header: t("ai.model"), cell: (r) => <span className="font-mono text-xs">{aiRunLabel(r)}</span>, hideBelow: "md" },
              { key: "ref", header: t("ai.ref"), cell: (r) => r.ref?.type ? recordLabel(r.ref,t) : "—", hideBelow: "lg" },
              { key: "failure", header: t("review.ai.reason"), sortable: false, cell: r => r.status === "error" ? <span className="text-xs">{aiErrorInfo(r,t).reason}<span className="block text-ink-secondary">{aiErrorInfo(r,t).next}</span></span> : null },
              { key: "latency_ms", header: t("ai.latency"), align: "right", cell: (r) => `${num((r.latency_ms || 0) / 1000, locale, 1)} s` },
            ]} />
  );
  return (
    <div>
      <PageHeader title={t("ai.title")} meta={q.data && <span>{t("ai.provider")}: {q.data.provider === "none" ? t("review.ai.no_provider") : q.data.provider}</span>} />
      <Loadable q={q}>
        {(d) => (
          <>
            {!d.available && <ErrorState className="mb-4" error={{ message: t("ai.unavailable") }} />}
            <Section title={t("ai.agent")}>
              <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
                {d.agents.length === 0 && <span className="text-ink-tertiary">{t("ai.empty")}</span>}
                {d.agents.map((a) => (
                  <button key={a.agent} type="button" onClick={() => setAgent(agent === a.agent ? null : a.agent)} aria-pressed={agent === a.agent}
                    className={`rounded-md px-1 -mx-1 hover:bg-hover text-left ${agent === a.agent ? "bg-hover ring-1 ring-border" : ""}`}>
                    <span className="font-medium">{agentLabel(a.agent)}</span>{" "}
                    <span className="tabular text-ink-secondary">{a.ok || 0} {t("review.ai.ok")}</span>
                    {a.error ? <span className="tabular text-danger-fg"> · {t("ai.error_count", { n: a.error })}</span> : null}
                    <span className="tabular text-ink-tertiary"> · {num((a.avg_ms || 0) / 1000, locale, 1)} s</span>
                  </button>
                ))}
              </div>
            </Section>
            <div className="mb-3">
              <Segmented value={status} onChange={setStatus} label={t("field.status")} options={[{ value: null, label: t("approvals.all") }, { value: "ok", label: t("review.ai.ok") }, { value: "error", label: t("ai.errors") }]} />
            </div>
            {renderRuns(aiRunGroups(d.items, d.provider).current)}
            <details className="mt-6">
              <summary className="cursor-pointer text-sm text-ink-secondary">{t("ai.earlier_runs")}</summary>
              <div className="mt-3">{renderRuns(aiRunGroups(d.items, d.provider).earlier)}</div>
            </details>
          </>
        )}
      </Loadable>
      <Sheet open={!!sel} onOpenChange={(o) => !o && setSel(null)}>
        <SheetContent side="right" className="w-full sm:max-w-xl overflow-y-auto">
          {sel && (
            <>
              <SheetTitle className="mb-3 text-md font-medium">{agentLabel(sel.agent)} · {dateTime(sel.created_at, locale)}</SheetTitle>
              <KV items={[{ label: t("ai.model"), value: aiRunLabel(sel), mono: true }, { label: t("ai.latency"), value: `${sel.latency_ms} ms` },
                { label: t("ai.ref"), value: sel.ref?.type ? recordLabel(sel.ref,t) : null, mono: true }, { label: t("review.ai.files"), value: (sel.files || []).join(", ") }]} />
              {sel.status === "error" && <Section title={t("review.ai.reason")}><p className="text-sm">{aiErrorInfo(sel,t).reason}</p><p className="mt-2 text-sm"><strong>{t("review.ai.next")}: </strong>{aiErrorInfo(sel,t).next}</p></Section>}
              {!sel.legacy && <details className="mt-4"><summary className="cursor-pointer text-sm">{t("review.ai.raw")}</summary>
              <Section title={t("ai.prompt")} className="mt-4"><pre className="whitespace-pre-wrap text-xs font-mono text-ink-secondary">{sel.prompt_preview}</pre></Section>
              {sel.output_preview && <Section title={t("ai.output")}><pre className="whitespace-pre-wrap text-xs font-mono">{sel.output_preview}</pre></Section>}
              </details>}
            </>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}
