// Ops exception queue. Decision: which exception to act on now (most severe first, then least SLA time left).
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Plus, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader, Metric } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Age, DetectedBy, SlaDue, SplitView, useMedia, useNow, useWide } from "@/features/exceptions/common";
import ExceptionDetail from "@/features/exceptions/ExceptionDetail";
import NewExceptionDialog from "@/features/exceptions/NewExceptionDialog";
import { SEVERITIES, localTitle, typeLabel } from "@/features/exceptions/text";

const STATUSES = ["active", "open", "in_progress", "resolved", "all"];

function SubLine({ ex, now, withAge }) {
  const { t } = useT();
  return (
    <span className="flex min-w-0 items-center gap-1.5 text-xs text-ink-tertiary">
      <span className="shrink-0 font-medium text-ink-secondary">{ex.shipment_reference || t("exceptions.no_shipment")}</span>
      {ex.company_name && <span className="min-w-[4.5rem] truncate">· {ex.company_name}</span>}
      <span className="min-w-[3rem] shrink-[3] truncate">· {typeLabel(t, ex.type)}</span>
      {withAge && <span className="shrink-0">· <Age at={ex.created_at} now={now} /></span>}
    </span>
  );
}

function MobileQueue({ items, selected, onOpen, now }) {
  const { t, locale } = useT();
  return (
    <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
      {items.map((ex) => (
        <li key={ex.id}>
          <button type="button" onClick={() => onOpen(ex.id)}
            className={cn("flex w-full flex-col gap-1 px-3 py-2.5 text-left hover:bg-hover", selected === ex.id && "bg-subtle")}>
            <span className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1.5">
                <StatusBadge size="sm" group="severity" value={ex.severity} />
                {ex.status !== "open" && <StatusBadge size="sm" group="exception" value={ex.status} icon={false} />}
              </span>
              <span className="text-xs"><SlaDue due={ex.sla_due} status={ex.status} now={now} /></span>
            </span>
            <span className="line-clamp-2 text-sm font-medium">{localTitle(ex, t, locale)}</span>
            <SubLine ex={ex} now={now} withAge />
            <span className="flex items-center gap-2 text-xs text-ink-tertiary">
              <span>{ex.owner?.name || t("exceptions.no_owner")}</span>
              <span>·</span>
              <DetectedBy value={ex.detected_by} />
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

export default function Exceptions() {
  const { t, locale } = useT();
  const [params, setParams] = useSearchParams();
  const wide = useWide();
  const narrow = useMedia("(max-width: 767px)");
  const now = useNow();
  const status = STATUSES.includes(params.get("status")) ? params.get("status") : "active";
  const severity = SEVERITIES.includes(params.get("severity")) ? params.get("severity") : null;
  const owner = ["me", "none"].includes(params.get("owner")) ? params.get("owner") : null;
  const overdueOnly = params.get("sla") === "overdue";
  const selected = params.get("id") || params.get("focus");
  const [search, setSearch] = useState("");
  const [newOpen, setNewOpen] = useState(false);
  const [detect, setDetect] = useState({ busy: false, result: null, error: null });

  const setParam = useCallback((changes, replace = false) => {
    setParams((p) => {
      const n = new URLSearchParams(p);
      if (Object.prototype.hasOwnProperty.call(changes, "id")) n.delete("focus");
      Object.entries(changes).forEach(([k, v]) => (v === null || v === undefined || v === "" ? n.delete(k) : n.set(k, v)));
      return n;
    }, { replace });
  }, [setParams]);

  const path = `/exceptions?status=${status}${severity ? `&severity=${severity}` : ""}${owner ? `&owner=${owner}` : ""}`;
  const q = useApi(path, { pollMs: 30000 });
  const counts = q.data?.counts;

  const items = useMemo(() => {
    let list = q.data?.items || [];
    if (overdueOnly) list = list.filter((e) => e.status !== "resolved" && e.sla_due && new Date(e.sla_due).getTime() < now);
    const s = search.trim().toLowerCase();
    if (s) {
      list = list.filter((e) => [e.shipment_reference, e.company_name, e.title, localTitle(e, t, locale)].some((v) => (v || "").toLowerCase().includes(s)));
    }
    return list;
  }, [q.data, overdueOnly, search, now, t, locale]);

  // Two panes: keep the most urgent exception open so the pane is never empty.
  useEffect(() => {
    if (wide && !selected && items.length) setParam({ id: items[0].id }, true);
  }, [wide, selected, items, setParam]);

  const runDetect = async () => {
    setDetect({ busy: true, result: null, error: null });
    try {
      const r = await apiPost("/exceptions/detect", {});
      setDetect({ busy: false, result: r, error: null });
      q.reload();
    } catch (e) {
      setDetect({ busy: false, result: null, error: e });
    }
  };

  const filtered = !!(severity || owner || overdueOnly || search.trim() || status !== "active");
  const statusCount = (s) => (s === "active" ? counts?.active : s === "all" ? undefined : counts?.status?.[s] ?? 0);

  const columns = [
    { key: "severity", header: t("exceptions.col.severity"), sort: (r) => -SEVERITIES.indexOf(r.severity), className: "w-[1%]",
      cell: (r) => (
        <span className="flex flex-col items-start gap-1">
          <StatusBadge size="sm" group="severity" value={r.severity} />
          {r.status !== "open" && <StatusBadge size="sm" group="exception" value={r.status} icon={false} />}
        </span>
      ) },
    { key: "title", header: t("exceptions.col.exception"), sort: (r) => localTitle(r, t, locale), className: "w-full max-w-0",
      cell: (r) => (
        <span className="flex min-w-0 flex-col gap-0.5">
          <span className="truncate text-sm font-medium" title={localTitle(r, t, locale)}>{localTitle(r, t, locale)}</span>
          <SubLine ex={r} now={now} />
        </span>
      ) },
    { key: "sla_due", header: t("exceptions.col.sla"), align: "right", className: "w-[1%] whitespace-nowrap text-xs",
      cell: (r) => (
        <span className="flex flex-col items-end">
          <SlaDue due={r.sla_due} status={r.status} now={now} />
          <span className="text-ink-tertiary"><Age at={r.created_at} now={now} /></span>
        </span>
      ) },
    { key: "owner", header: t("exceptions.col.owner"), sort: (r) => r.owner?.name || "",
      className: "w-[1%] whitespace-nowrap text-xs",
      cell: (r) => (r.owner ? <span className="text-ink-secondary">{r.owner.name}</span> : <span className="text-ink-tertiary">—</span>) },
    { key: "detected_by", header: t("exceptions.col.source"), className: "w-[1%] whitespace-nowrap", cell: (r) => <DetectedBy value={r.detected_by} /> },
  ];

  const meta = counts ? (
    <>
      <Metric value={counts.active} label={t("exceptions.meta.active")} to="/ops/exceptions" />
      <span className="text-ink-tertiary">·</span>
      <Metric value={counts.severity?.critical || 0} label={t("exceptions.meta.critical")} tone={counts.severity?.critical ? "danger" : undefined}
        to="/ops/exceptions?severity=critical" />
      <span className="text-ink-tertiary">·</span>
      <Metric value={counts.overdue_sla || 0} label={t("exceptions.meta.overdue")} tone={counts.overdue_sla ? "danger" : undefined}
        to="/ops/exceptions?sla=overdue" />
      <span className="text-ink-tertiary">·</span>
      <Metric value={counts.unassigned || 0} label={t("exceptions.meta.unassigned")} tone={counts.unassigned ? "warning" : undefined}
        to="/ops/exceptions?owner=none" />
    </>
  ) : q.loading ? <span className="text-ink-tertiary">{t("state.loading")}</span> : null;

  const list = (
    <Loadable q={q} skeleton={<LoadingRows rows={8} />}
      isEmpty={() => items.length === 0}
      empty={filtered ? (
        <EmptyState title={t("exceptions.empty.filtered")} body={t("exceptions.empty.filtered_body")}
          action={<Button size="sm" variant="outline" onClick={() => { setSearch(""); setParams({}); }}>{t("exceptions.filter.clear")}</Button>} />
      ) : (
        <EmptyState title={t("exceptions.empty.active")} body={t("exceptions.empty.active_body")}
          action={<div className="flex gap-2"><Button size="sm" onClick={() => setNewOpen(true)}><Plus /> {t("exceptions.new")}</Button>
            <Button size="sm" variant="outline" onClick={runDetect} disabled={detect.busy}>{t("exceptions.detect")}</Button></div>} />
      )}>
      {() => (narrow ? (
        <MobileQueue items={items} selected={selected} onOpen={(id) => setParam({ id })} now={now} />
      ) : (
        <div className="overflow-x-auto rounded-xl border border-[color:var(--border-subtle)]">
          <DataTable dense columns={columns} rows={items} onRowClick={(r) => setParam({ id: r.id })}
            rowClassName={(r) => cn(selected === r.id && "bg-subtle", r.status === "resolved" && "opacity-70")} />
        </div>
      ))}
    </Loadable>
  );

  return (
    <div>
      <PageHeader
        title={t("exceptions.title")}
        meta={meta}
        actions={
          <>
            <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
            {detect.result && (
              <span role="status" className="text-xs text-ink-secondary tabular">{t("exceptions.detect.result", detect.result)}</span>
            )}
            <Button variant="outline" size="sm" onClick={runDetect} disabled={detect.busy}>
              <RefreshCw className={cn(detect.busy && "animate-spin")} /> {detect.busy ? t("exceptions.detect.running") : t("exceptions.detect")}
            </Button>
            <Button size="sm" onClick={() => setNewOpen(true)}><Plus /> {t("exceptions.new")}</Button>
          </>
        }
      />
      {detect.error && <ErrorState className="mb-3" compact error={detect.error} onRetry={runDetect} />}
      <FilterBar
        search={search}
        onSearch={setSearch}
        placeholder={t("exceptions.filter.search")}
        filters={[
          { key: "status", label: t("exceptions.filter.status"), value: status, onChange: (v) => setParam({ status: v === "active" ? null : v }),
            options: STATUSES.map((s) => ({ value: s, label: t(`exceptions.filter.${s}`), count: statusCount(s) })) },
          { key: "severity", label: t("exceptions.filter.severity"), value: severity, onChange: (v) => setParam({ severity: v }),
            options: [{ value: null, label: t("exceptions.filter.any_severity") },
              ...SEVERITIES.map((s) => ({ value: s, label: tEnum(t, "severity", s), count: counts?.severity?.[s] || 0 }))] },
          { key: "owner", label: t("exceptions.filter.owner"), value: owner, onChange: (v) => setParam({ owner: v }),
            options: [{ value: null, label: t("exceptions.filter.owner_all") }, { value: "me", label: t("exceptions.filter.owner_me") },
              { value: "none", label: t("exceptions.filter.owner_none"), count: counts?.unassigned }] },
        ]}
        right={overdueOnly ? (
          <Button size="xs" variant="outline" onClick={() => setParam({ sla: null })}>{t("exceptions.filter.overdue_only")} ×</Button>
        ) : null}
      />
      <SplitView
        wide={wide}
        open={!!selected}
        onClose={() => setParam({ id: null })}
        title={t("exceptions.title")}
        list={list}
        detail={selected ? (
          <ExceptionDetail key={selected} id={selected} onChanged={q.reload} onClose={wide ? null : () => setParam({ id: null })} />
        ) : (
          <p className="p-4 text-sm text-ink-tertiary">{t("exceptions.select")}</p>
        )}
      />
      <NewExceptionDialog open={newOpen} onOpenChange={setNewOpen} onCreated={(ex) => { q.reload(); setParam({ id: ex.id }); }} />
    </div>
  );
}
