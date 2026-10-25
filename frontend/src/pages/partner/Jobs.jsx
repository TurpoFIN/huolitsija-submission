// Partner portal: legs assigned to the haulier's company (GET /api/partner/legs). Decision first: what to accept,
// what to pick up next, what is late. No prices are ever shown or fetched.
import { tEnum, useT } from "@/lib/i18n";
import React, { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { AlertTriangle, CheckCircle2, ChevronRight, Inbox } from "lucide-react";
import { PageHeader } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar } from "@/components/app/FilterBar";
import { EmptyState, Loadable, LoadingRows } from "@/components/app/states";
import { StatusBadge } from "@/components/app/StatusBadge";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { dateShort, kg, num } from "@/lib/format";
import { cn } from "@/lib/utils";
import { DataAge, ModeIcon, shortPlace, timeHM } from "@/features/shipments/common";
import { usePT } from "@/features/shipments/partnerI18n";

const POLL_MS = 60000;

function when(iso, locale) {
  if (!iso) return "—";
  return `${dateShort(iso, locale)} ${timeHM(iso, locale)}`;
}

function placeLine(p) {
  if (!p) return "—";
  const a = p.address || {};
  return [a.street, [a.postal_code, a.city].filter(Boolean).join(" ")].filter(Boolean).join(", ") || p.name || "—";
}

function cargoLine(c, locale, t) {
  if (!c) return "—";
  const parts = [];
  if (c.packages) parts.push(`${num(c.packages, locale)} ${tEnum(t, "package_type", c.package_type)}`.trim());
  if (c.weight_kg) parts.push(kg(c.weight_kg, locale));
  return parts.join(" · ") || "—";
}

function nextKey(i) {
  if (!i.leg.accepted_at && !i.leg.atd && i.next_action) return "accept";
  return i.next_action || "none";
}

function NextStep({ item, pt }) {
  const k = nextKey(item);
  if (k === "none") {
    return (
      <span className="inline-flex items-center gap-1 text-ink-tertiary">
        <CheckCircle2 className="size-3.5" aria-hidden />
        {pt("p.next.none")}
      </span>
    );
  }
  return (
    <span className={cn("inline-flex h-6 items-center rounded-full px-2 text-xs font-medium whitespace-nowrap",
      k === "accept" ? "bg-primary text-primary-foreground" : "bg-secondary text-foreground")}>
      {pt(`p.next.${k}`)}
    </span>
  );
}

export default function Jobs() {
  const { pt, locale } = usePT();
  const { t } = useT();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "open";
  const [search, setSearch] = useState("");
  const active = useApi("/partner/legs?state=active", { pollMs: POLL_MS });
  const done = useApi(tab === "done" ? "/partner/legs?state=done" : null);
  const q = tab === "done" ? done : active;
  const counts = active.data?.counts || {};

  const rows = useMemo(() => {
    let r = q.data?.items || [];
    if (tab === "accept") r = r.filter((i) => nextKey(i) === "accept");
    const s = search.trim().toLowerCase();
    if (s) {
      r = r.filter((i) => [i.reference, i.from?.name, i.from?.city, i.to?.name, i.to?.city, i.from?.address?.city, i.to?.address?.city,
        i.cargo?.description, i.leg?.transport_doc_no].filter(Boolean).some((v) => String(v).toLowerCase().includes(s)));
    }
    return r;
  }, [q.data, tab, search]);

  const setTab = (v) => setParams(v === "open" ? {} : { tab: v }, { replace: true });
  const open = (i) => navigate(`/partner/shipments/${i.shipment_id}`);
  const carrierName = active.data?.carrier?.name || "";

  const columns = [
    { key: "reference", header: pt("p.col.job"), sort: (i) => i.reference,
      cell: (i) => (
        <div className="flex min-w-0 flex-col">
          <span className="font-mono text-[13px]">{i.reference}</span>
          <span className="mt-0.5 flex items-center gap-1.5">
            <StatusBadge group="status" value={i.shipment_status} size="sm" />
            {i.holds?.length > 0 && <span className="text-xs text-warning-fg">{pt("p.on_hold")}</span>}
          </span>
        </div>
      ) },
    { key: "route", header: pt("p.col.route"), sortable: false,
      cell: (i) => (
        <div className="min-w-0 max-w-[24rem]">
          <div className="flex min-w-0 items-center gap-1.5">
            <ModeIcon mode={i.leg.mode} />
            <span className="truncate">{shortPlace(i.from)} <span className="text-ink-tertiary">→</span> {shortPlace(i.to)}</span>
          </div>
          <div className="truncate text-xs text-ink-tertiary">
            {pt(`p.role.${i.leg.role || "main"}`)} · {placeLine(i.from)} → {placeLine(i.to)}
          </div>
        </div>
      ) },
    { key: "etd", header: pt("p.col.pickup"), align: "right", sort: (i) => i.leg.atd || i.leg.etd,
      cell: (i) => (
        <span className="flex flex-col items-end leading-tight">
          <span className="tabular whitespace-nowrap">{when(i.leg.atd || i.leg.etd, locale)}</span>
          <span className="text-xs text-ink-tertiary">{i.leg.atd ? pt("p.actual") : pt("p.estimate")}</span>
        </span>
      ) },
    { key: "eta", header: pt("p.col.delivery"), align: "right", sort: (i) => i.leg.ata || i.leg.eta,
      cell: (i) => (
        <span className="flex flex-col items-end leading-tight">
          <span className="tabular whitespace-nowrap">{when(i.leg.ata || i.leg.eta, locale)}</span>
          {i.late ? <span className="text-xs text-danger-fg">{pt("p.late")}</span>
            : <span className="text-xs text-ink-tertiary">{i.leg.ata ? pt("p.actual") : pt("p.estimate")}</span>}
        </span>
      ) },
    { key: "cargo", header: pt("p.col.cargo"), sortable: false, hideBelow: "lg",
      cell: (i) => (
        <div className="min-w-0 max-w-[12rem]">
          <div className="truncate">{i.cargo?.description || "—"}</div>
          <div className="text-xs text-ink-tertiary tabular">{cargoLine(i.cargo, locale, t)}</div>
        </div>
      ) },
    { key: "next", header: pt("p.col.next"), sortable: false, align: "right", cell: (i) => <NextStep item={i} pt={pt} /> },
  ];

  const emptyFor = search ? (
    <EmptyState icon={Inbox} title={pt("p.empty_search")} action={<Button size="sm" variant="outline" onClick={() => setSearch("")}>{pt("p.clear")}</Button>} />
  ) : tab === "accept" ? (
    <EmptyState icon={CheckCircle2} title={pt("p.empty_accept")} />
  ) : tab === "done" ? (
    <EmptyState icon={Inbox} title={pt("p.empty_done")} />
  ) : (
    <EmptyState icon={Inbox} title={pt("p.empty")} body={pt("p.empty_body")} />
  );

  return (
    <div className="mx-auto w-full max-w-6xl">
      <PageHeader
        title={pt("p.title")}
        meta={active.data ? <span>{pt("p.meta", { carrier: carrierName, active: counts.active ?? 0, accept: counts.to_accept ?? 0 })}</span> : null}
        actions={<DataAge q={q} />}
      />
      <FilterBar
        search={search}
        onSearch={setSearch}
        placeholder={pt("p.search")}
        filters={[{ key: "tab", label: pt("p.title"), value: tab, onChange: setTab, options: [
          { value: "open", label: pt("p.tab.open"), count: active.data ? counts.active ?? 0 : null },
          { value: "accept", label: pt("p.tab.accept"), count: active.data ? counts.to_accept ?? 0 : null },
          { value: "done", label: pt("p.tab.done"), count: active.data ? counts.done ?? 0 : null },
        ] }]}
      />
      <Loadable q={q} skeleton={<LoadingRows rows={6} />} isEmpty={() => rows.length === 0} empty={emptyFor}>
        {() => (
          <>
            {/* desktop / tablet: dense table */}
            <div className="hidden sm:block">
              <DataTable columns={columns} rows={rows.map((i) => ({ ...i, _key: `${i.shipment_id}:${i.leg.seq}` }))} rowKey="_key" onRowClick={open} />
            </div>
            {/* 390 px: two-line rows */}
            <ul className="sm:hidden divide-y divide-[color:var(--border-subtle)] border-y border-[color:var(--border-subtle)]">
              {rows.map((i) => (
                <li key={`${i.shipment_id}:${i.leg.seq}`}>
                  <Link to={`/partner/shipments/${i.shipment_id}`} className="flex items-start gap-3 px-1 py-2.5 hover:bg-hover">
                    <ModeIcon mode={i.leg.mode} className="mt-0.5" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-sm">
                          {shortPlace(i.from)} <span className="text-ink-tertiary">→</span> {shortPlace(i.to)}
                        </span>
                        <NextStep item={i} pt={pt} />
                      </div>
                      <div className="mt-0.5 flex items-center justify-between gap-2 text-xs text-ink-tertiary">
                        <span className="truncate">
                          <span className="font-mono">{i.reference}</span> · {pt(`p.role.${i.leg.role || "main"}`)}
                        </span>
                        <span className={cn("tabular whitespace-nowrap", i.late && "text-danger-fg")}>
                          {i.late && <AlertTriangle className="mr-1 inline size-3" aria-hidden />}
                          {when(i.leg.atd ? i.leg.eta : i.leg.etd, locale)}
                        </span>
                      </div>
                    </div>
                    <ChevronRight className="mt-0.5 size-4 text-ink-tertiary" aria-hidden />
                  </Link>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-ink-tertiary tabular">{pt("p.count", { n: rows.length })}</p>
          </>
        )}
      </Loadable>
    </div>
  );
}
