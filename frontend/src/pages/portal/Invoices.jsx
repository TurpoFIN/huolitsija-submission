// Portal: invoices. Decision: "what do I owe and by when?" -> open and overdue totals first, then a dense
// Stripe-like list (amount, status, number, shipment, issued, due) with state filters and search.
import React, { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ChevronRight, Receipt } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Metric, PageHeader } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { dueInfo, eur, isoDate } from "@/features/billing/invoiceFormat";

const TABS = {
  all: () => true,
  open: (i) => i.status === "open" || i.status === "processing",
  overdue: (i) => i.status === "overdue",
  paid: (i) => i.status === "paid",
};

function Amount({ inv }) {
  const { t, locale } = useT();
  const credit = inv.kind === "credit_note";
  const partly = !credit && ["open", "overdue", "processing"].includes(inv.status) && inv.amount_due_cents !== inv.total_cents;
  return (
    <span className="inline-flex flex-col items-end">
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
        {credit && <span className="rounded-full bg-secondary px-1.5 text-[11px] font-medium text-ink-secondary">{t("billing.kind.credit_note")}</span>}
        <span className="tabular font-medium">{eur(inv.total_cents, locale)}</span>
      </span>
      {partly && <span className="text-[11px] text-ink-tertiary tabular whitespace-nowrap">{t("billing.list.left", { amount: eur(inv.amount_due_cents, locale) })}</span>}
    </span>
  );
}

function Due({ inv }) {
  const { t, locale } = useT();
  if (inv.kind === "credit_note" || !inv.due_date) return <span className="text-ink-tertiary">—</span>;
  const d = dueInfo(t, inv);
  return (
    <span className="inline-flex flex-col">
      <span className="tabular whitespace-nowrap">{isoDate(inv.due_date, locale)}</span>
      {d && <span className={cn("text-[11px] whitespace-nowrap", d.tone === "warning" ? "text-warning-fg" : "text-ink-tertiary")}>{d.text}</span>}
      {!d && inv.status === "paid" && inv.paid_at && (
        <span className="text-[11px] text-ink-tertiary whitespace-nowrap">{t("billing.list.paid_on", { date: isoDate(String(inv.paid_at).slice(0, 10), locale) })}</span>
      )}
    </span>
  );
}

export default function Invoices() {
  const { t, locale } = useT();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab = TABS[params.get("tab")] ? params.get("tab") : "all";
  const [search, setSearch] = useState(params.get("q") || "");
  const [debounced, setDebounced] = useState(search.trim());
  useEffect(() => {
    const h = setTimeout(() => setDebounced(search.trim()), 250);
    return () => clearTimeout(h);
  }, [search]);
  const q = useApi(`/invoices${debounced ? `?q=${encodeURIComponent(debounced)}` : ""}`);

  const setTab = (v) => {
    const next = new URLSearchParams(params);
    if (v && v !== "all") next.set("tab", v);
    else next.delete("tab");
    setParams(next, { replace: true });
  };

  // A document without a number was never issued (e.g. a draft voided before issue): not the customer's invoice.
  const issued = useMemo(() => (q.data?.items || []).filter((i) => i.number), [q.data]);
  const totals = q.data?.totals || {};
  const count = (k) => (q.data ? issued.filter(TABS[k]).length : null);
  const options = [
    { value: "all", label: t("billing.tab.all"), count: count("all") },
    { value: "open", label: t("billing.tab.open"), count: count("open") },
    { value: "overdue", label: t("billing.tab.overdue"), count: count("overdue") },
    { value: "paid", label: t("billing.tab.paid"), count: count("paid") },
  ];

  const nextDue = useMemo(() => {
    const open = issued.filter((i) => i.kind === "invoice" && i.status === "open" && i.due_date && i.amount_due_cents > 0 && (i.days_until_due ?? 0) >= 0);
    open.sort((a, b) => (a.due_date < b.due_date ? -1 : 1));
    return open[0] || null;
  }, [issued]);

  const rows = useMemo(() => issued.filter(TABS[tab]), [issued, tab]);

  const columns = [
    { key: "total_cents", header: t("billing.col.amount"), align: "right", sort: (r) => r.total_cents, cell: (r) => <Amount inv={r} /> },
    { key: "status", header: t("field.status"), sort: (r) => r.status, cell: (r) => <StatusBadge group="invoice" size="sm" value={r.status} /> },
    { key: "number", header: t("billing.col.number"), sort: (r) => r.number || "", cell: (r) => <span className="font-mono text-[13px] whitespace-nowrap">{r.number || "—"}</span> },
    { key: "shipment_reference", header: t("billing.col.shipment"), sort: (r) => r.shipment_reference || "", hideBelow: "lg",
      cell: (r) => (r.shipment_id ? (
        <Link to={`/portal/shipments/${r.shipment_id}`} onClick={(e) => e.stopPropagation()} className="whitespace-nowrap text-ink-accent hover:underline">
          {r.shipment_reference || "—"}
        </Link>
      ) : <span className="text-ink-tertiary">—</span>) },
    { key: "customer_reference", header: t("billing.col.your_ref"), hideBelow: "lg", sortable: false,
      cell: (r) => <span className="text-ink-secondary">{r.customer_reference || <span className="text-ink-tertiary">—</span>}</span> },
    { key: "issue_date", header: t("billing.col.issued"), sort: (r) => r.issue_date || "", hideBelow: "md",
      cell: (r) => <span className="tabular whitespace-nowrap text-ink-secondary">{isoDate(r.issue_date, locale)}</span> },
    { key: "due_date", header: t("billing.col.due"), sort: (r) => r.due_date || "", cell: (r) => <Due inv={r} /> },
  ];

  const filtered = tab !== "all" || !!debounced;
  const empty = (
    <EmptyState icon={Receipt}
      title={filtered ? t("billing.list.none_match") : t("billing.list.empty_customer")}
      body={filtered ? t("billing.list.none_match_body") : t("billing.list.empty_customer_body")}
      action={filtered ? <Button size="sm" variant="outline" onClick={() => { setSearch(""); setTab("all"); }}>{t("billing.list.clear")}</Button> : null} />
  );

  return (
    <div>
      <PageHeader
        title={t("nav.invoices")}
        meta={q.data ? (
          <>
            <Metric value={eur(totals.open_cents || 0, locale)} label={t((totals.open_count || 0) === 1 ? "billing.metric.open_one" : "billing.metric.open", { n: totals.open_count || 0 })} />
            <span aria-hidden className="text-ink-tertiary">·</span>
            <button type="button" onClick={() => setTab("overdue")} className="rounded-md px-1 -mx-1 hover:bg-hover">
              <Metric value={eur(totals.overdue_cents || 0, locale)} tone={(totals.overdue_count || 0) > 0 ? "danger" : undefined}
                label={t("billing.metric.overdue", { n: totals.overdue_count || 0 })} />
            </button>
            {nextDue && (
              <>
                <span aria-hidden className="text-ink-tertiary">·</span>
                <span className="text-sm text-ink-secondary">
                  {t("billing.metric.next_due", { date: isoDate(nextDue.due_date, locale), amount: eur(nextDue.amount_due_cents, locale) })}
                </span>
              </>
            )}
            <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
          </>
        ) : null}
      />
      <FilterBar search={search} onSearch={setSearch} placeholder={t("billing.list.search_customer")}
        filters={[{ key: "tab", label: t("field.status"), value: tab, onChange: setTab, options }]} />
      <Loadable q={q} skeleton={<LoadingRows rows={8} />} isEmpty={() => rows.length === 0} empty={empty}>
        {(d) => (
          <>
            <div className="hidden overflow-x-auto md:block">
              <DataTable rows={rows} columns={columns} dense onRowClick={(r) => navigate(`/portal/invoices/${r.id}`)} />
            </div>
            <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] md:hidden">
              {rows.map((r) => {
                const due = dueInfo(t, r);
                return (
                  <li key={r.id}>
                    <Link to={`/portal/invoices/${r.id}`} className="flex items-center gap-3 rounded-md px-1 py-2.5 hover:bg-hover">
                      <div className="flex min-w-0 flex-1 flex-col gap-1">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-mono text-[13px]">{r.number || "—"}</span>
                          <Amount inv={r} />
                        </div>
                        <div className="flex items-center justify-between gap-2 text-xs">
                          <StatusBadge group="invoice" size="sm" value={r.status} />
                          <span className={cn("tabular", due?.tone === "warning" ? "text-warning-fg" : "text-ink-tertiary")}>
                            {r.kind === "credit_note" || !r.due_date ? isoDate(r.issue_date, locale)
                              : r.status === "paid" && r.paid_at ? t("billing.list.paid_on", { date: isoDate(String(r.paid_at).slice(0, 10), locale) })
                              : `${t("billing.col.due")} ${isoDate(r.due_date, locale)}${due ? ` · ${due.text}` : ""}`}
                          </span>
                        </div>
                        {r.shipment_reference && <span className="text-xs text-ink-tertiary">{r.shipment_reference}</span>}
                      </div>
                      <ChevronRight className="size-4 shrink-0 text-ink-tertiary" aria-hidden />
                    </Link>
                  </li>
                );
              })}
            </ul>
            {d.total > d.items.length && (
              <p className="mt-2 text-xs text-ink-tertiary">{t("billing.list.showing", { n: d.items.length, total: d.total })}</p>
            )}
          </>
        )}
      </Loadable>
    </div>
  );
}
