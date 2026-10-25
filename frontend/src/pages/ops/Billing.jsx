// Ops: accounts receivable. Decision: "what is outstanding, what is late, what needs issuing?"
// First line: outstanding, overdue (danger), drafts to review. One compact aging row. "Ready to invoice" strip for
// delivered shipments without an invoice. Then the invoice table with state filters, search and row actions.
import React, { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ChevronRight, Plus, Receipt } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Metric, PageHeader } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { ActionNote, ActionsMenu, useInvoiceActions } from "@/features/billing/InvoiceActions";
import NewInvoiceSheet from "@/features/billing/NewInvoiceSheet";
import { dueInfo, eur, isoDate } from "@/features/billing/invoiceFormat";

const TABS = [
  { value: "all", status: null },
  { value: "draft", status: "draft" },
  { value: "open", status: "open,processing" },
  { value: "overdue", status: "overdue" },
  { value: "paid", status: "paid" },
  { value: "void", status: "void" },
  { value: "credited", status: "credited" },
];
const BUCKETS = ["current", "d1_30", "d31_60", "d61_90", "d90_plus"];

function Aging({ aging }) {
  const { t, locale } = useT();
  const by = Object.fromEntries((aging || []).map((a) => [a.bucket, a]));
  return (
    <p className="mb-4 flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm">
      <span className="text-ink-tertiary">{t("billing.aging.title")}</span>
      {BUCKETS.map((b, i) => {
        const a = by[b] || { amount_cents: 0, count: 0 };
        const late = b !== "current" && a.count > 0;
        return (
          <span key={b} className="whitespace-nowrap">
            <span className="text-ink-secondary">{t(`billing.aging.${b}`)}</span>{" "}
            <span className={cn("tabular", a.count === 0 ? "text-ink-tertiary" : late ? "text-danger-fg" : "text-foreground")}>{eur(a.amount_cents, locale)}</span>
            {a.count > 0 && <span className="text-ink-tertiary tabular"> ({a.count})</span>}
            {i < BUCKETS.length - 1 && <span aria-hidden className="text-ink-tertiary"> ·</span>}
          </span>
        );
      })}
    </p>
  );
}

function ReadyToInvoice({ q, canWrite, onCreated }) {
  const { t, locale } = useT();
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState({});
  const [done, setDone] = useState({});
  const [errors, setErrors] = useState({});
  const items = q.data?.items || [];
  const shown = Object.keys(done).length;
  if (q.error && !q.data) return <ErrorState className="mb-4" error={q.error} onRetry={q.reload} compact />;
  if (!q.data || (items.length === 0 && shown === 0)) return null;
  const doneRows = Object.values(done).filter((d) => !items.some((s) => s.id === d.shipment.id));
  const list = [...items.map((s) => ({ shipment: s, inv: done[s.id]?.inv })), ...doneRows];
  const visible = expanded ? list : list.slice(0, 5);
  const create = async (s) => {
    setBusy((b) => ({ ...b, [s.id]: true }));
    setErrors((e) => ({ ...e, [s.id]: null }));
    try {
      const inv = await apiPost(`/invoices/from-shipment/${s.id}`, {});
      setDone((d) => ({ ...d, [s.id]: { shipment: s, inv } }));
      onCreated?.();
    } catch (e) {
      setErrors((x) => ({ ...x, [s.id]: e.message }));
    } finally {
      setBusy((b) => ({ ...b, [s.id]: false }));
    }
  };
  return (
    <section className="mb-5">
      <div className="mb-1 flex items-center justify-between gap-3 border-b border-[color:var(--border-subtle)] pb-1.5">
        <h2 className="text-sm font-medium">{t("billing.ready.title")} <span className="tabular text-ink-tertiary">{items.length}</span></h2>
        <span className="hidden text-xs text-ink-tertiary sm:inline">{t("billing.ready.hint")}</span>
      </div>
      <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)]">
        {visible.map(({ shipment: s, inv }) => (
          <li key={s.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5">
            <Link to={`/ops/shipments/${s.id}`} className="w-36 shrink-0 text-sm font-medium hover:underline">{s.reference}</Link>
            <span className="min-w-0 flex-1 truncate text-sm">{s.customer_name || "—"}</span>
            <span className="hidden min-w-0 truncate text-xs text-ink-tertiary md:inline md:w-56">
              {s.origin || "—"} → {s.destination || "—"}{s.mode ? ` · ${tEnum(t, "mode", s.mode)}` : ""}
            </span>
            <span className="text-xs text-ink-tertiary tabular whitespace-nowrap">{t("billing.ready.delivered", { date: isoDate(s.delivered_at, locale) })}</span>
            <span className="hidden w-24 text-right text-sm tabular sm:inline">{s.price?.total_cents != null ? eur(s.price.total_cents, locale) : "—"}</span>
            <span className="flex w-full items-center justify-end gap-2 sm:w-auto">
              {errors[s.id] && <span className="text-xs text-danger-fg">{errors[s.id]}</span>}
              {inv ? (
                <span className="inline-flex items-center gap-2 text-xs">
                  <span className="status-success inline-flex h-5 items-center rounded-full px-2 font-medium">
                    {inv.status === "draft" ? t("billing.ready.draft_created") : t("billing.ready.issued", { n: inv.number || "" })}
                  </span>
                  <Link to={`/ops/billing/${inv.id}`} className="font-medium hover:underline">{t("action.open")}</Link>
                </span>
              ) : (
                <Button size="xs" variant="outline" disabled={!canWrite || busy[s.id]} title={!canWrite ? t("billing.act.no_permission") : undefined}
                  onClick={() => create(s)}>
                  {busy[s.id] ? t("billing.panel.creating") : t("billing.panel.create")}
                </Button>
              )}
            </span>
          </li>
        ))}
      </ul>
      {list.length > 5 && (
        <Button size="xs" variant="ghost" className="mt-1" onClick={() => setExpanded((x) => !x)}>
          {expanded ? t("billing.ready.less") : t("billing.ready.more", { n: list.length - 5 })}
        </Button>
      )}
    </section>
  );
}

function Amount({ inv }) {
  const { t, locale } = useT();
  const partly = inv.kind === "invoice" && ["open", "overdue", "processing"].includes(inv.status) && inv.amount_due_cents !== inv.total_cents;
  return (
    <span className="inline-flex flex-col items-end">
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
        {inv.kind === "credit_note" && <span className="rounded-full bg-secondary px-1.5 text-[11px] font-medium text-ink-secondary">{t("billing.kind.credit_note")}</span>}
        <span className="tabular font-medium">{eur(inv.total_cents, locale)}</span>
      </span>
      {partly && <span className="text-[11px] text-ink-tertiary tabular whitespace-nowrap">{t("billing.list.left", { amount: eur(inv.amount_due_cents, locale) })}</span>}
    </span>
  );
}

function Due({ inv }) {
  const { t, locale } = useT();
  if (inv.status === "draft") return <span className="text-ink-tertiary whitespace-nowrap">{t("billing.fact.terms_days", { n: inv.payment_terms_days ?? "—" })}</span>;
  if (inv.kind === "credit_note" || !inv.due_date) return <span className="text-ink-tertiary">—</span>;
  const d = dueInfo(t, inv);
  return (
    <span className="inline-flex flex-col">
      <span className="tabular whitespace-nowrap">{isoDate(inv.due_date, locale)}</span>
      {d && <span className={cn("text-[11px] whitespace-nowrap", d.tone === "warning" ? "text-warning-fg" : "text-ink-tertiary")}>{d.text}</span>}
    </span>
  );
}

export default function Billing() {
  const { t, locale } = useT();
  const navigate = useNavigate();
  const { can } = useAuth();
  const canWrite = can("billing.write");
  const [params, setParams] = useSearchParams();
  const tabValue = TABS.some((x) => x.value === params.get("tab")) ? params.get("tab") : "all";
  const tab = TABS.find((x) => x.value === tabValue);
  const [search, setSearch] = useState(params.get("q") || "");
  const [debounced, setDebounced] = useState(search.trim());
  const [sheet, setSheet] = useState(false);
  const [note, setNote] = useState(null);
  useEffect(() => {
    const h = setTimeout(() => setDebounced(search.trim()), 250);
    return () => clearTimeout(h);
  }, [search]);

  const qs = new URLSearchParams();
  if (tab.status) qs.set("status", tab.status);
  if (debounced) qs.set("q", debounced);
  const q = useApi(`/invoices${qs.toString() ? `?${qs}` : ""}`, { pollMs: 60000 });
  const ready = useApi("/billing/uninvoiced");
  const acts = useInvoiceActions({
    onDone: (r) => {
      setNote({ text: r.text, link: r.link, tone: r.tone });
      q.reload();
      if (r.kind === "void" || r.kind === "issue") ready.reload();
    },
  });

  const setTab = (v) => {
    const next = new URLSearchParams(params);
    if (v && v !== "all") next.set("tab", v);
    else next.delete("tab");
    setParams(next, { replace: true });
  };

  const counts = q.data?.counts || {};
  const totals = q.data?.totals || {};
  const options = useMemo(() => TABS.map((x) => {
    const counts = q.data?.counts || {};
    const c = !q.data ? null : x.value === "all" ? Object.values(counts).reduce((a, b) => a + b, 0)
      : x.value === "open" ? (counts.open || 0) + (counts.processing || 0) : counts[x.value] || 0;
    return { value: x.value, label: t(`billing.tab.${x.value}`), count: c };
  }), [q.data, t]);

  const columns = [
    { key: "total_cents", header: t("billing.col.amount"), align: "right", sort: (r) => r.total_cents, cell: (r) => <Amount inv={r} /> },
    { key: "status", header: t("field.status"), sort: (r) => r.status, cell: (r) => <StatusBadge group="invoice" size="sm" value={r.status} /> },
    { key: "number", header: t("billing.col.number"), sort: (r) => r.number || "",
      cell: (r) => (r.number ? <span className="font-mono text-[13px] whitespace-nowrap">{r.number}</span> : <span className="text-ink-tertiary">{r.status === "draft" ? t("billing.draft") : "—"}</span>) },
    { key: "company_name", header: t("field.customer"), sort: (r) => r.company_name || "", className: "max-w-[16rem]",
      cell: (r) => <span className="block truncate">{r.company_name || "—"}</span> },
    { key: "shipment_reference", header: t("billing.col.shipment"), sort: (r) => r.shipment_reference || "", hideBelow: "lg",
      cell: (r) => (r.shipment_id ? (
        <Link to={`/ops/shipments/${r.shipment_id}`} onClick={(e) => e.stopPropagation()} className="whitespace-nowrap text-ink-accent hover:underline">{r.shipment_reference || "—"}</Link>
      ) : <span className="text-ink-tertiary">{r.origin === "manual" ? t("billing.origin.manual") : "—"}</span>) },
    { key: "issue_date", header: t("billing.col.issued"), sort: (r) => r.issue_date || "", hideBelow: "md",
      cell: (r) => <span className="tabular whitespace-nowrap text-ink-secondary">{r.issue_date ? isoDate(r.issue_date, locale) : "—"}</span> },
    { key: "due_date", header: t("billing.col.due"), sort: (r) => r.due_date || "", cell: (r) => <Due inv={r} /> },
    { key: "actions", header: "", sortable: false, className: "w-10 text-right",
      cell: (r) => <ActionsMenu inv={r} acts={acts} canWrite={canWrite} /> },
  ];

  const filtered = tabValue !== "all" || !!debounced;
  const empty = (
    <EmptyState icon={Receipt}
      title={filtered ? t("billing.list.none_match") : t("billing.list.empty_ops")}
      body={filtered ? t("billing.list.none_match_body") : t("billing.list.empty_ops_body")}
      action={filtered
        ? <Button size="sm" variant="outline" onClick={() => { setSearch(""); setTab("all"); }}>{t("billing.list.clear")}</Button>
        : canWrite ? <Button size="sm" onClick={() => setSheet(true)}><Plus /> {t("billing.new.title")}</Button> : null} />
  );

  return (
    <div>
      <PageHeader
        title={t("nav.billing")}
        meta={q.data ? (
          <>
            <Metric value={eur(totals.outstanding_cents || 0, locale)}
              label={t((totals.open_count || 0) + (totals.overdue_count || 0) === 1 ? "billing.metric.outstanding_one" : "billing.metric.outstanding", { n: (totals.open_count || 0) + (totals.overdue_count || 0) })} />
            <span aria-hidden className="text-ink-tertiary">·</span>
            <button type="button" onClick={() => setTab("overdue")} className="rounded-md px-1 -mx-1 hover:bg-hover">
              <Metric value={eur(totals.overdue_cents || 0, locale)} tone={(totals.overdue_count || 0) > 0 ? "danger" : undefined}
                label={t("billing.metric.overdue", { n: totals.overdue_count || 0 })} />
            </button>
            <span aria-hidden className="text-ink-tertiary">·</span>
            <button type="button" onClick={() => setTab("draft")} className="rounded-md px-1 -mx-1 hover:bg-hover">
              <Metric value={counts.draft || 0} tone={(counts.draft || 0) > 0 ? "warning" : undefined}
                label={`${t((counts.draft || 0) === 1 ? "billing.metric.drafts_one" : "billing.metric.drafts")}${totals.draft_cents ? ` · ${eur(totals.draft_cents, locale)}` : ""}`} />
            </button>
            <Freshness at={q.updatedAt} onRefresh={() => { q.reload(); ready.reload(); }} refreshing={q.refreshing} />
          </>
        ) : null}
        actions={
          <Button onClick={() => setSheet(true)} disabled={!canWrite} title={!canWrite ? t("billing.act.no_permission") : undefined}>
            <Plus /> {t("billing.new.title")}
          </Button>
        }
      />
      {q.data && <Aging aging={q.data.aging} />}
      <ReadyToInvoice q={ready} canWrite={canWrite} onCreated={() => q.reload()} />
      <FilterBar search={search} onSearch={setSearch} placeholder={t("billing.list.search_ops")}
        filters={[{ key: "tab", label: t("field.status"), value: tabValue, onChange: setTab, options }]} />
      <ActionNote note={note} onClose={() => setNote(null)} className="mb-3" />
      <Loadable q={q} skeleton={<LoadingRows rows={10} />} isEmpty={(d) => (d.items || []).length === 0} empty={empty}>
        {(d) => (
          <>
            <div className="hidden overflow-x-auto md:block">
              <DataTable rows={d.items} columns={columns} dense onRowClick={(r) => navigate(`/ops/billing/${r.id}`)} />
            </div>
            <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] md:hidden">
              {d.items.map((r) => {
                const due = dueInfo(t, r);
                return (
                  <li key={r.id}>
                    <Link to={`/ops/billing/${r.id}`} className="flex items-center gap-3 rounded-md px-1 py-2.5 hover:bg-hover">
                      <div className="flex min-w-0 flex-1 flex-col gap-1">
                        <div className="flex items-center justify-between gap-2">
                          <span className="min-w-0 truncate text-sm font-medium">{r.company_name || "—"}</span>
                          <Amount inv={r} />
                        </div>
                        <div className="flex items-center justify-between gap-2 text-xs">
                          <StatusBadge group="invoice" size="sm" value={r.status} />
                          <span className={cn("tabular whitespace-nowrap", due?.tone === "warning" ? "text-warning-fg" : "text-ink-tertiary")}>
                            {r.due_date && r.kind !== "credit_note" ? `${isoDate(r.due_date, locale)}${due ? ` · ${due.text}` : ""}` : ""}
                          </span>
                        </div>
                        <span className="truncate text-xs text-ink-tertiary">
                          <span className="font-mono">{r.number || t("billing.draft")}</span>{r.shipment_reference ? ` · ${r.shipment_reference}` : ""}
                        </span>
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
      {acts.dialogs}
      <NewInvoiceSheet open={sheet} onOpenChange={setSheet}
        onCreated={(inv, issued) => {
          setSheet(false);
          navigate(`/ops/billing/${inv.id}`, { state: { note: issued ? t("billing.done.issued", { n: inv.number || "" }) + ` ${t("billing.done.emailed")}` : t("billing.done.draft_saved") } });
        }} />
    </div>
  );
}
