// Customer customs list. Decision: which declarations need my confirmation, and what each costs in duty and VAT.
import React, { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Freshness, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { dateShort, money, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { PORTAL_GROUPS, groupCount, inGroup, useDebounced, useIsNarrow } from "@/features/customs/common";

function NeedsYou({ r }) {
  const { t } = useT();
  if (!["draft", "needs_info"].includes(r.status)) return null;
  return (
    <span className="inline-flex items-center gap-1 text-[11px] font-medium text-warning-fg whitespace-nowrap">
      <AlertTriangle className="size-3" aria-hidden />
      {r.blocking_customer ? t("customs.portal.next.missing", { n: r.blocking_customer }) : t("customs.portal.confirm")}
    </span>
  );
}

export default function PortalCustoms() {
  const { t, locale } = useT();
  const nav = useNavigate();
  const narrow = useIsNarrow();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const dq = useDebounced(search.trim(), 250);
  const q = useApi(`/customs${dq ? `?q=${encodeURIComponent(dq)}` : ""}`, { pollMs: 60000 });
  const data = q.data;
  const counts = data?.counts || {};
  const items = useMemo(() => data?.items || [], [data]);
  const input = groupCount(PORTAL_GROUPS, "input", counts);
  const progress = groupCount(PORTAL_GROUPS, "progress", counts);
  const tab = params.get("tab") || (input ? "input" : progress ? "progress" : "all");
  const rows = useMemo(() => items.filter((r) => inGroup(PORTAL_GROUPS, tab, r.status))
    .sort((a, b) => new Date(b.updated_at || b.created_at) - new Date(a.updated_at || a.created_at)), [items, tab]);
  const open = (r) => nav(`/portal/customs/${r.id}`);

  const columns = [
    { key: "shipment_ref", header: t("customs.col.shipment"), sort: (r) => r.shipment_ref,
      cell: (r) => (
        <span className="inline-flex flex-col">
          <span className="whitespace-nowrap">{r.shipment_ref || "—"}</span>
          <span className="font-mono text-[11px] text-ink-tertiary">{r.lrn}</span>
        </span>
      ) },
    { key: "status", header: t("field.status"), sort: (r) => r.status,
      cell: (r) => <span className="inline-flex flex-col items-start gap-0.5"><StatusBadge size="sm" group="customs" value={r.status} /><NeedsYou r={r} /></span> },
    { key: "goods", header: t("customs.portal.col.declared"), sort: (r) => r.items_count,
      cell: (r) => (
        <span className="inline-flex flex-col">
          <span>{r.items_count === 1 ? t("customs.items_1") : t("customs.items_n", { n: r.items_count || 0 })}</span>
          <span className="font-mono text-[11px] text-ink-tertiary">{[...new Set(r.cn_codes || [])].slice(0, 3).join(" · ") || "—"}</span>
        </span>
      ) },
    { key: "value", header: t("customs.col.value"), align: "right", sort: (r) => r.customs_value_cents,
      cell: (r) => money(r.customs_value_cents, locale, { decimals: 2 }) },
    { key: "duty_vat", header: t("customs.portal.col.payable"), align: "right", sort: (r) => (r.duty_cents || 0) + (r.vat_cents || 0),
      cell: (r) => (
        <span className="inline-flex flex-col items-end">
          <span className="tabular">{money((r.duty_cents || 0) + (r.vat_cents || 0), locale, { decimals: 2 })}</span>
          <span className="text-[11px] text-ink-tertiary whitespace-nowrap">{r.vat_route ? t(`customs.vat.${r.vat_route}`) : ""} · {t("customs.estimate").toLowerCase()}</span>
        </span>
      ) },
    { key: "mrn", header: "MRN", sort: (r) => r.mrn, cell: (r) => <span className="font-mono text-[12px]">{r.mrn || "—"}</span>, hideBelow: "lg" },
    { key: "eta", header: t("customs.eta_short"), sort: (r) => r.eta, cell: (r) => <span className="tabular whitespace-nowrap">{dateShort(r.eta, locale)}</span>, hideBelow: "md" },
    { key: "updated_at", header: t("field.updated"), sort: (r) => r.updated_at, cell: (r) => <span className="text-ink-secondary whitespace-nowrap">{relTime(r.updated_at, locale)}</span>, hideBelow: "lg" },
  ];

  const tabs = PORTAL_GROUPS.map((g) => ({ value: g.key, label: t(g.key === "all" ? "customs.tab.all" : `customs.portal.tab.${g.key}`),
    count: data ? groupCount(PORTAL_GROUPS, g.key, counts) : undefined }));
  const meta = data ? (
    <>
      <span className={input ? "text-foreground" : undefined}>{input ? t("customs.portal.meta.input", { n: input }) : t("customs.portal.meta.none")}</span>
      {progress > 0 && <span>· {t("customs.portal.meta.progress", { n: progress })}</span>}
      <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
    </>
  ) : null;

  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  return (
    <div>
      <PageHeader title={t("customs.portal.title")} meta={meta} />
      {(total > 0 || dq) && (
        <FilterBar search={search} onSearch={setSearch} placeholder={t("customs.search")}
          filters={[{ key: "tab", value: tab, onChange: (v) => setParams({ tab: v }, { replace: true }), options: tabs, label: t("customs.tab.label") }]} />
      )}
      {q.error && data && <ErrorState className="mb-3" compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
      {q.loading && !data ? (
        <LoadingRows rows={5} />
      ) : q.error && !data ? (
        <ErrorState error={q.error} onRetry={q.reload} />
      ) : rows.length === 0 ? (
        <EmptyState title={dq ? t("customs.empty.search", { q: dq }) : total ? t("customs.empty.title") : t("customs.portal.empty")}
          body={dq ? null : t("customs.portal.empty_body")} />
      ) : narrow ? (
        <ul className="divide-y divide-[color:var(--border-subtle)]">
          {rows.map((r) => (
            <li key={r.id}>
              <button type="button" onClick={() => open(r)} className="flex w-full flex-col gap-1 py-3 text-left">
                <span className="flex items-center justify-between gap-2">
                  <span className="text-sm">{r.shipment_ref || r.lrn}</span>
                  <StatusBadge size="sm" group="customs" value={r.status} />
                </span>
                <span className="flex flex-wrap items-center gap-x-3 text-xs text-ink-secondary">
                  <span>{r.items_count === 1 ? t("customs.items_1") : t("customs.items_n", { n: r.items_count || 0 })}</span>
                  <span className="tabular">{t("customs.portal.consequence", { duty: money(r.duty_cents, locale, { decimals: 2 }), vat: money(r.vat_cents, locale, { decimals: 2 }) })}</span>
                </span>
                <NeedsYou r={r} />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <DataTable dense columns={columns} rows={rows} onRowClick={open} />
      )}
    </div>
  );
}
