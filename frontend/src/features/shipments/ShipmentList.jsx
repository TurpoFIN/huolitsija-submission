// Shipment list for the customer portal and the ops console.
// Decision: "which shipment needs me?" -> attention tab first, dense rows, ETA right-aligned with lateness.
import React, { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ChevronRight, Columns3, List, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { PageHeader } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar, Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import {
  ACTIVE_STATUSES, DataAge, EtaValue, LastUpdate, MODES, ModeIcon, ModeLabel, Reasons, RouteText, STATUS_ORDER, relTime,
} from "@/features/shipments/common";
import { defaultTab } from "@/features/shipments/listTabs";

const POLL_MS = 60000;

function useDebounced(value, ms = 250) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const h = setTimeout(() => setV(value), ms);
    return () => clearTimeout(h);
  }, [value, ms]);
  return v;
}

/** Needs-action reasons minus the ones the status badge already shows. */
const extraReasons = (s) => (s.needs_action || []).filter((r) => !(r === "exception" && s.status === "exception") && !(r === "hold" && s.status === "customs_hold"));

const rank = (s) => {
  const i = STATUS_ORDER.indexOf(s.status);
  return i < 0 ? 99 : i;
};

function sortRows(rows, tab) {
  const ts = (x) => (x ? new Date(x).getTime() : Number.MAX_SAFE_INTEGER);
  if (tab === "delivered") return [...rows].sort((a, b) => ts(b.delivered_at) - ts(a.delivered_at));
  return [...rows].sort((a, b) => {
    const na = a.needs_action?.length ? 0 : 1;
    const nb = b.needs_action?.length ? 0 : 1;
    if (na !== nb) return na - nb;
    const da = a.status === "delivered" ? 1 : 0;
    const db = b.status === "delivered" ? 1 : 0;
    if (da !== db) return da - db;
    return ts(a.eta) - ts(b.eta);
  });
}

export default function ShipmentList({ scope = "customer" }) {
  const ops = scope === "ops";
  const { t, locale } = useT();
  const nav = useNavigate();
  const base = ops ? "/ops/shipments" : "/portal/shipments";
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState(params.get("q") || "");
  const q = useDebounced(search.trim());
  const mode = params.get("mode") || "";
  const company = params.get("customer") || "";
  const view = ops && params.get("view") === "board" ? "board" : "list";

  const setParam = (k, v) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v);
    else p.delete(k);
    setParams(p, { replace: true });
  };
  useEffect(() => {
    if ((params.get("q") || "") !== q) setParam("q", q);
  }, [q]); // eslint-disable-line react-hooks/exhaustive-deps

  const qs = new URLSearchParams({ limit: "1000" });
  if (q) qs.set("q", q);
  if (mode) qs.set("mode", mode);
  if (company) qs.set("company_id", company);
  const res = useApi(`/shipments?${qs.toString()}`, { pollMs: POLL_MS });
  const counts = res.data?.counts || {};

  const tabParam = params.get("tab");
  const tab = tabParam || defaultTab(counts, { ops, loaded: !!res.data });

  const customers = useMemo(() => {
    if (!ops || !res.data) return [];
    const m = new Map();
    res.data.items.forEach((s) => m.set(s.company_id, s.customer_name));
    return [...m.entries()].sort((a, b) => (a[1] || "").localeCompare(b[1] || "", "fi"));
  }, [ops, res.data]);

  const rows = useMemo(() => {
    if (!res.data) return null;
    let r = res.data.items;
    if (tab === "needs_action") r = r.filter((s) => s.needs_action?.length);
    else if (tab === "active") r = r.filter((s) => ACTIVE_STATUSES.includes(s.status));
    else if (tab !== "all") r = r.filter((s) => s.status === tab);
    return sortRows(r, tab);
  }, [res.data, tab]);

  const tabOptions = [
    { value: "needs_action", label: ops ? t("shipments.tab.needs_action") : t("shipments.tab.attention"), count: counts.needs_action ?? null },
    { value: "active", label: t("shipments.tab.active"), count: counts.active ?? null },
    ...STATUS_ORDER.filter((s) => s !== "cancelled" && (counts[s] || 0) > 0).map((s) => ({ value: s, label: tEnum(t, "status", s), count: counts[s] })),
    ...((counts.cancelled || 0) > 0 ? [{ value: "cancelled", label: tEnum(t, "status", "cancelled"), count: counts.cancelled }] : []),
    { value: "all", label: t("shipments.tab.all"), count: counts.all ?? null },
  ];

  const filtered = !!(q || mode || company);
  const clear = () => {
    setSearch("");
    setParams(new URLSearchParams(view === "board" ? { view: "board" } : {}), { replace: true });
  };

  const columns = [
    { key: "status", header: t("shipments.col.status"), sort: rank, cell: (s) => <StatusBadge group="status" value={s.status} size="sm" />, className: "w-[1%]" },
    {
      key: "reference", header: t("shipments.col.reference"), sort: (s) => s.reference,
      cell: (s) => (
        <span className="flex flex-col leading-tight">
          <Link to={`${base}/${s.id}`} onClick={(e) => e.stopPropagation()} className="whitespace-nowrap font-mono text-[13px] hover:underline">{s.reference}</Link>
          <span className="truncate text-xs text-ink-tertiary max-w-[160px]">
            {[s.references?.customer_ref, ...(s.references?.po_numbers || []).slice(0, 1)].filter(Boolean).join(" · ") || "—"}
          </span>
        </span>
      ),
    },
    ...(ops ? [{ key: "customer", header: t("shipments.col.customer"), sort: (s) => s.customer_name, hideBelow: "lg",
      cell: (s) => <span className="block max-w-[150px] truncate" title={s.customer_name}>{s.customer_name}</span> }] : []),
    {
      key: "route", header: t("shipments.col.route"), sort: (s) => s.origin?.name,
      cell: (s) => (
        <span className="flex min-w-0 flex-col leading-tight">
          <RouteText origin={s.origin} destination={s.destination} className={ops ? "max-w-[220px]" : "max-w-[300px]"} />
          <span className={cn("truncate text-xs text-ink-tertiary", ops ? "max-w-[220px]" : "max-w-[300px]")} title={s.cargo_description || ""}>{s.cargo_description || "—"}</span>
        </span>
      ),
    },
    { key: "mode", header: t("shipments.col.mode"), sort: (s) => s.mode, hideBelow: "md", cell: (s) => <ModeLabel mode={s.mode} /> },
    {
      key: "carrier", header: t("shipments.col.carrier"), sort: (s) => s.carrier || "", hideBelow: "lg",
      cell: (s) => (
        <span className="flex flex-col leading-tight">
          <span className={cn("truncate", ops ? "max-w-[140px]" : "max-w-[200px]")} title={s.carrier || ""}>{s.carrier || <span className="text-ink-tertiary">{t("shipments.no_carrier")}</span>}</span>
          <span className={cn("truncate text-xs text-ink-tertiary", ops ? "max-w-[140px]" : "max-w-[200px]")}>{s.vessel || s.flight || " "}</span>
        </span>
      ),
    },
    { key: "eta", header: t("shipments.col.eta"), align: "right", sort: (s) => (s.status === "delivered" ? s.delivered_at : s.eta), cell: (s) => <EtaValue s={s} /> },
    ...(ops ? [{ key: "action", header: t("shipments.col.action"), sortable: false, hideBelow: "md", cell: (s) => <Reasons reasons={extraReasons(s)} max={2} /> }] : []),
    { key: "last", header: t("shipments.col.last_update"), sort: (s) => s.last_event?.at || "", hideBelow: ops ? undefined : "lg",
      headClassName: ops ? "hidden 2xl:table-cell" : undefined, className: ops ? "hidden 2xl:table-cell" : undefined,
      cell: (s) => <LastUpdate e={s.last_event} /> },
  ];

  const emptyAll = res.data && counts.all === 0 && !filtered;
  const emptyNode = emptyAll ? (
    <EmptyState title={t("shipments.empty.none")} body={ops ? t("shipments.empty.none_ops") : t("shipments.empty.none_body")}
      action={!ops && <Button asChild size="sm"><Link to="/portal/quotes/new"><Plus /> {t("shipments.empty.quote")}</Link></Button>} />
  ) : tab === "needs_action" && !filtered ? (
    <EmptyState title={t("shipments.empty.attention")} body={t("shipments.empty.attention_body")}
      action={<Button size="sm" variant="outline" onClick={() => setParam("tab", "active")}>{t("shipments.tab.active")}</Button>} />
  ) : (
    <EmptyState title={t("shipments.empty.filtered")} body={t("shipments.empty.filtered_body")}
      action={<Button size="sm" variant="outline" onClick={clear}>{t("shipments.filter.clear")}</Button>} />
  );

  return (
    <div>
      <PageHeader
        title={t("shipments.title")}
        meta={res.data ? (
          <>
            <span className="tabular">{ops
              ? t("shipments.meta.ops", { active: counts.active ?? 0, action: counts.needs_action ?? 0 })
              : t("shipments.meta.portal", { active: counts.active ?? 0, attention: counts.needs_action ?? 0 })}</span>
            <DataAge q={res} />
          </>
        ) : null}
        actions={!ops ? <Button asChild size="sm" variant="outline"><Link to="/portal/quotes/new"><Plus /> {t("shipments.empty.quote")}</Link></Button> : null}
      />

      <div className="mb-2">
        <Segmented label={t("shipments.tabs")} value={tab} onChange={(v) => setParam("tab", v)} options={tabOptions} />
      </div>
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
        <FilterBar className="mb-0" search={search} onSearch={setSearch} placeholder={t("shipments.search")} />
        <div className={cn("grid min-w-0 gap-2 sm:ml-auto sm:flex sm:items-center", ops ? "grid-cols-2" : "grid-cols-1")}>
          {ops && customers.length > 1 && (
            <Select value={company || "all"} onValueChange={(v) => setParam("customer", v === "all" ? "" : v)}>
              <SelectTrigger className="h-8 w-full min-w-0 sm:w-[200px]" aria-label={t("shipments.filter.customer")}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t("shipments.filter.all_customers")}</SelectItem>
                {customers.map(([id, name]) => <SelectItem key={id} value={id}>{name}</SelectItem>)}
              </SelectContent>
            </Select>
          )}
          <Select value={mode || "all"} onValueChange={(v) => setParam("mode", v === "all" ? "" : v)}>
            <SelectTrigger className="h-8 w-full min-w-0 sm:w-[190px]" aria-label={t("shipments.filter.mode")}><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("shipments.filter.all_modes")}</SelectItem>
              {MODES.map((m) => <SelectItem key={m} value={m}><ModeLabel mode={m} /></SelectItem>)}
            </SelectContent>
          </Select>
          {ops && (
            <Segmented className="col-span-2 justify-self-start" label={t("shipments.view")} value={view} onChange={(v) => setParam("view", v === "board" ? "board" : "")}
              options={[{ value: "list", label: <span className="inline-flex items-center gap-1"><List className="size-3.5" />{t("shipments.view.list")}</span> },
                { value: "board", label: <span className="inline-flex items-center gap-1"><Columns3 className="size-3.5" />{t("shipments.view.board")}</span> }]} />
          )}
        </div>
      </div>

      {res.loading && !res.data ? (
        <LoadingRows rows={8} />
      ) : res.error && !res.data ? (
        <ErrorState error={res.error} onRetry={res.reload} />
      ) : view === "board" ? (
        <ShipmentBoard items={sortRows((res.data.items || []).filter((s) => s.status !== "cancelled"), "all")} base={base} />
      ) : rows.length === 0 ? (
        emptyNode
      ) : (
        <>
          <div className="hidden md:block">
            <DataTable dense columns={columns} rows={rows} onRowClick={(s) => nav(`${base}/${s.id}`)}
              rowClassName={(s) => (s.status === "delivered" ? "text-ink-secondary" : "")} />
          </div>
          <ul className="md:hidden divide-y divide-[color:var(--border-subtle)] border-y border-[color:var(--border-subtle)]">
            {rows.map((s) => (
              <li key={s.id}>
                <Link to={`${base}/${s.id}`} className="flex items-center gap-3 py-2.5 hover:bg-hover">
                  <ModeIcon mode={s.mode} />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center justify-between gap-2">
                      <span className="font-mono text-[13px]">{s.reference}</span>
                      <StatusBadge group="status" value={s.status} size="sm" />
                    </span>
                    <span className="mt-0.5 flex items-center justify-between gap-2 text-sm">
                      <RouteText origin={s.origin} destination={s.destination} className="text-ink-secondary" />
                      <span className="shrink-0 text-right"><EtaValue s={s} compact /></span>
                    </span>
                    {ops && extraReasons(s).length > 0 && <Reasons reasons={extraReasons(s)} className="mt-1" />}
                  </span>
                  <ChevronRight className="size-4 shrink-0 text-ink-tertiary" aria-hidden />
                </Link>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-ink-tertiary tabular">{t("shipments.count_shown", { n: rows.length })}</p>
        </>
      )}
    </div>
  );
}

/** Ops board: one column per status, oldest update per column, cards link to the detail. */
function ShipmentBoard({ items, base }) {
  const { t, locale } = useT();
  const cols = STATUS_ORDER.filter((s) => s !== "cancelled");
  const by = Object.fromEntries(cols.map((c) => [c, []]));
  items.forEach((s) => by[s.status]?.push(s));
  return (
    <div className="-mx-1 flex gap-3 overflow-x-auto px-1 pb-2" role="list">
      {cols.map((c) => {
        const list = by[c];
        const oldest = list.reduce((m, s) => (s.last_event?.at && (!m || s.last_event.at < m) ? s.last_event.at : m), null);
        return (
          <section key={c} role="listitem" aria-label={tEnum(t, "status", c)} className="flex w-[264px] shrink-0 flex-col rounded-xl bg-[var(--bg-subtle)] p-2">
            <header className="mb-2 flex items-center justify-between gap-2 px-1">
              <StatusBadge group="status" value={c} size="sm" />
              <span className="text-xs tabular text-ink-tertiary">{list.length}</span>
            </header>
            {oldest && c !== "delivered" && <p className="mb-2 px-1 text-[11px] text-ink-tertiary">{t("shipments.board.since", { age: relTime(oldest, locale) })}</p>}
            <div className="flex flex-col gap-1.5">
              {list.length === 0 && <p className="px-1 py-2 text-xs text-ink-tertiary">{t("shipments.board.none")}</p>}
              {list.map((s) => (
                <Link key={s.id} to={`${base}/${s.id}`}
                  className="surface-raised block rounded-lg px-2.5 py-2 text-sm hover:bg-hover focus-visible:outline-2">
                  <span className="flex items-center justify-between gap-2">
                    <span className="inline-flex items-center gap-1.5 font-mono text-[12px]"><ModeIcon mode={s.mode} className="size-3.5" />{s.reference}</span>
                    <span className="text-xs"><EtaValue s={s} compact /></span>
                  </span>
                  <span className="mt-0.5 block truncate text-xs text-ink-secondary">{s.customer_name}</span>
                  <RouteText origin={s.origin} destination={s.destination} className="block text-xs" />
                  {extraReasons(s).length > 0 && <Reasons reasons={extraReasons(s)} max={2} className={cn("mt-1")} />}
                </Link>
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}
