// Ops customs queue. Decision: which declarations need a broker now (review, holds, overdue), and what blocks them.
import React, { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { AlertTriangle, CheckCircle2, User, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar, Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Freshness, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { dateShort, money } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { GROUPS, SlaAge, groupCount, inGroup, slaState, useDebounced, useIsNarrow } from "@/features/customs/common";
import { CustomsHubTabs } from "@/features/customs/extCommon";

const CONTROL = ["under_control", "rejected", "not_released"];
const priority = (r) => (r.on_hold ? 0 : CONTROL.includes(r.status) ? 1 : r.status === "ready_for_review" ? 2 : 3);
const ageSince = (r) => r.updated_at || r.created_at;

function HoldBadge() {
  const { t } = useT();
  return <StatusBadge size="sm" state="failed" label={t("customs.on_hold")} />;
}

function Checks({ r }) {
  const { t } = useT();
  if (!r.blocking_count) {
    return <span className="inline-flex items-center gap-1 text-sm text-ink-secondary"><CheckCircle2 className="size-3.5 text-success-fg" aria-hidden />{t("customs.checks_ok")}</span>;
  }
  return (
    <span className="inline-flex flex-col">
      <span className="inline-flex items-center gap-1 text-sm whitespace-nowrap"><XCircle className="size-3.5 text-danger-fg" aria-hidden />{t("customs.blocking_n", { n: r.blocking_count })}</span>
      {r.blocking_customer > 0 && <span className="text-[11px] text-ink-tertiary">{t("customs.blocking_customer_n", { n: r.blocking_customer })}</span>}
    </span>
  );
}

function Goods({ r }) {
  const { t } = useT();
  const codes = [...new Set(r.cn_codes || [])].filter(Boolean);
  return (
    <span className="inline-flex flex-col">
      <span className="text-sm">{r.items_count === 1 ? t("customs.items_1") : t("customs.items_n", { n: r.items_count || 0 })}</span>
      <span className="font-mono text-[11px] text-ink-tertiary truncate max-w-[180px]">{codes.slice(0, 3).join(" · ") || "—"}{codes.length > 3 ? " …" : ""}</span>
      {(r.low_confidence_items || []).length > 0 && (
        <span className="inline-flex items-center gap-1 text-[11px] font-medium text-warning-fg">
          <AlertTriangle className="size-3" aria-hidden />{t("customs.low_conf", { seq: r.low_confidence_items.join(", ") })}
        </span>
      )}
    </span>
  );
}

function DutyVat({ r }) {
  const { t, locale } = useT();
  return (
    <span className="inline-flex flex-col items-end">
      <span className="tabular">{r.duty_complete === false || r.vat_complete === false || r.duty_cents == null || r.vat_cents == null
        ? t("customs.totals.not_calculated") : money(r.duty_cents + r.vat_cents, locale, { decimals: 2 })}</span>
      <span className="text-[11px] text-ink-tertiary whitespace-nowrap">{r.vat_route ? t(`customs.vat.${r.vat_route}`) : "—"}</span>
    </span>
  );
}

function MobileList({ rows, onOpen }) {
  const { t, locale } = useT();
  return (
    <ul className="divide-y divide-[color:var(--border-subtle)]">
      {rows.map((r) => (
        <li key={r.id}>
          <button type="button" onClick={() => onOpen(r)} className="flex w-full flex-col gap-1 py-3 text-left">
            <span className="flex items-center justify-between gap-2">
              <span className="font-mono text-[13px]">{r.lrn}</span>
              <span className="flex items-center gap-1">{r.on_hold && <HoldBadge />}<StatusBadge size="sm" group="customs" value={r.status} /></span>
            </span>
            <span className="text-sm">{r.company_name || "—"} <span className="text-ink-tertiary">· {r.shipment_ref || "—"}</span></span>
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-secondary">
              <Checks r={r} />
              <span className="tabular">{money(r.customs_value_cents, locale, { decimals: 0 })}</span>
              <SlaAge status={r.status} since={ageSince(r)} />
              {r.eta && <span>{t("customs.eta_short")} {dateShort(r.eta, locale)}</span>}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

export default function OpsCustoms() {
  const { t, locale } = useT();
  const nav = useNavigate();
  const narrow = useIsNarrow();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState(params.get("q") || "");
  const mine = params.get("mine") === "1";
  const dq = useDebounced(search.trim(), 250);
  const qs = new URLSearchParams();
  if (dq) qs.set("q", dq);
  if (mine) qs.set("mine", "true");
  const q = useApi(`/customs${qs.toString() ? `?${qs}` : ""}`, { pollMs: 60000 });
  const data = q.data;
  const counts = data?.counts || {};
  const items = useMemo(() => data?.items || [], [data]);

  // Default to the first action tab that has work; "all" when nothing is waiting.
  const tabParam = params.get("tab");
  const firstAction = ["review", "drafts", "filing", "control"].find((k) => groupCount(GROUPS, k, counts) > 0) || "all";
  const tab = tabParam || (data ? firstAction : "review");
  const setParam = (k, v) => {
    const n = new URLSearchParams(params);
    if (v) n.set(k, v); else n.delete(k);
    setParams(n, { replace: true });
  };
  useEffect(() => {
    const n = new URLSearchParams(params);
    if (dq) n.set("q", dq); else n.delete("q");
    if (n.toString() !== params.toString()) setParams(n, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dq]);

  const rows = useMemo(() => items.filter((r) => inGroup(GROUPS, tab, r.status))
    .sort((a, b) => priority(a) - priority(b) || new Date(ageSince(a)) - new Date(ageSince(b))), [items, tab]);

  const review = counts.ready_for_review || 0;
  const holds = items.filter((r) => r.on_hold).length;
  const control = groupCount(GROUPS, "control", counts);
  const overdue = items.filter((r) => slaState(r.status, ageSince(r)).state === "breached").length;
  const gw = data?.gateway;

  const columns = [
    { key: "lrn", header: t("customs.col.lrn"), sort: (r) => r.lrn,
      cell: (r) => (
        <span className="inline-flex flex-col">
          <span className="font-mono text-[13px] whitespace-nowrap">{r.lrn}</span>
          {r.mrn && <span className="font-mono text-[11px] text-ink-tertiary">{r.mrn}</span>}
        </span>
      ) },
    { key: "company_name", header: `${t("field.customer")} · ${t("customs.col.shipment").toLowerCase()}`, sort: (r) => r.company_name,
      cell: (r) => (
        <span className="inline-flex min-w-0 flex-col">
          <span className="inline-flex items-center gap-1.5">
            <span className="truncate max-w-[220px]">{r.company_name || "—"}</span>
            {r.demo && <span className="text-[11px] text-ink-tertiary">{t("customs.demo")}</span>}
          </span>
          <span className="text-[11px] text-ink-tertiary truncate max-w-[240px]">{[r.shipment_ref, r.route].filter(Boolean).join(" · ") || "—"}</span>
        </span>
      ) },
    { key: "goods", header: t("customs.col.goods"), sort: (r) => r.items_count, cell: (r) => <Goods r={r} />, hideBelow: "lg" },
    { key: "checks", header: t("customs.col.checks"), sort: (r) => r.blocking_count, cell: (r) => <Checks r={r} /> },
    { key: "value", header: t("customs.col.value"), align: "right", sort: (r) => r.customs_value_cents,
      cell: (r) => money(r.customs_value_cents, locale, { decimals: 2 }) },
    { key: "duty_vat", header: t("customs.col.duty_vat"), align: "right", sort: (r) => (r.duty_cents || 0) + (r.vat_cents || 0),
      cell: (r) => <DutyVat r={r} />, hideBelow: "lg" },
    { key: "status", header: t("field.status"), sort: (r) => priority(r),
      cell: (r) => (
        <span className="inline-flex flex-wrap items-center gap-1">
          {r.on_hold && <HoldBadge />}
          <StatusBadge size="sm" group="customs" value={r.status} />
        </span>
      ) },
    { key: "owner", header: t("customs.col.owner"), sort: (r) => r.owner || "",
      cell: (r) => (r.owner ? <span className="whitespace-nowrap">{r.owner}</span> : <span className="text-ink-tertiary" title={t("customs.unassigned")}>—</span>),
      hideBelow: "lg" },
    { key: "age", header: t("customs.col.age"), sort: (r) => new Date(ageSince(r)).getTime() * -1,
      cell: (r) => (
        <span className="inline-flex flex-col">
          <SlaAge status={r.status} since={ageSince(r)} />
          {r.eta && <span className="text-[11px] text-ink-tertiary whitespace-nowrap tabular">{t("customs.eta_short")} {dateShort(r.eta, locale)}</span>}
        </span>
      ) },
  ];

  const tabs = GROUPS.map((g) => ({ value: g.key, label: t(`customs.tab.${g.key}`), count: data ? groupCount(GROUPS, g.key, counts) : undefined }));
  const meta = data ? (
    <>
      <span className={review ? "text-foreground" : undefined}>{review ? t("customs.meta.review", { n: review }) : t("customs.meta.clear")}</span>
      {holds > 0 && <span>· <span className="text-danger-fg">{t("customs.meta.hold", { n: holds })}</span></span>}
      {control > 0 && <span>· {t("customs.meta.control", { n: control })}</span>}
      {overdue > 0 && <span>· {t("customs.sla.breached")} {overdue}</span>}
      {gw && (
        <span className="inline-flex h-5 items-center rounded-full bg-secondary px-2 text-[11px] font-medium text-ink-secondary" title={gw.description}>
          {t(`customs.gateway.${gw.mode}`) === `customs.gateway.${gw.mode}` ? gw.mode : t(`customs.gateway.${gw.mode}`)}
        </span>
      )}
      <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
    </>
  ) : null;

  const emptyTitle = dq ? t("customs.empty.search", { q: dq }) : mine ? t("customs.empty.mine") : t("customs.empty.title");

  return (
    <div>
      <PageHeader title={t("customs.title")} meta={meta} />
      <CustomsHubTabs active="import" />
      <FilterBar search={search} onSearch={setSearch} placeholder={t("customs.search")}
        right={(
          <Button variant={mine ? "secondary" : "outline"} size="default" aria-pressed={mine} onClick={() => setParam("mine", mine ? null : "1")}>
            <User /> {t("customs.mine")}
          </Button>
        )} />
      <div className="mb-3">
        <Segmented value={tab} onChange={(v) => setParam("tab", v)} options={tabs} label={t("customs.tab.label")} />
      </div>
      {q.error && data && <ErrorState className="mb-3" compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
      {q.loading && !data ? (
        <LoadingRows rows={8} />
      ) : q.error && !data ? (
        <ErrorState error={q.error} onRetry={q.reload} />
      ) : rows.length === 0 ? (
        <EmptyState title={emptyTitle} body={dq || mine ? null : t("customs.empty.body")} />
      ) : narrow ? (
        <MobileList rows={rows} onOpen={(r) => nav(`/ops/customs/${r.id}`)} />
      ) : (
        <DataTable dense columns={columns} rows={rows} onRowClick={(r) => nav(`/ops/customs/${r.id}`)}
          rowClassName={(r) => cn(r.on_hold && "shadow-[inset_2px_0_0_var(--danger-500)]")} />
      )}
    </div>
  );
}
