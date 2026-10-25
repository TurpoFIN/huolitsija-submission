// Ops › Finance › Carrier invoices (Ostolaskut). Decision: "which purchase invoices need a person (review, dispute),
// and what was received?" Status pills with counts, upload (PDF / Finvoice XML / image), one row per invoice with the
// amount in its own currency + EUR and the variance against the cost accrual.
// APIs: GET /finance/carrier-invoices?status=a,b&q= {items, total, counts}, POST /finance/carrier-invoices (multipart file).
import React, { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ChevronRight, FileText, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FilterBar } from "@/components/app/FilterBar";
import { FileUpload } from "@/components/app/FileUpload";
import { AgentMark } from "@/components/app/StatusBadge";
import { EmptyState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiUpload, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { isoDate } from "@/features/billing/invoiceFormat";
import { ActionNote, Dot, FinBar, FinStatus, MoneyCell } from "@/features/finance/kit";
import { FinTable } from "@/features/finance/Table";
import { qs, signedEur } from "@/features/finance/format";

// Pills: "attention" groups the two states that need a person.
const TABS = [
  { value: "attention", status: "needs_review,disputed" },
  { value: "all", status: null },
  { value: "received", status: "received,extracting" },
  { value: "matched", status: "matched" },
  { value: "approved", status: "approved,scheduled" },
  { value: "paid", status: "paid" },
  { value: "rejected", status: "rejected,duplicate" },
];

function tabFor(status) {
  if (!status) return "attention";
  const hit = TABS.find((x) => x.status === status);
  if (hit) return hit.value;
  return TABS.find((x) => x.status && x.status.split(",").includes(status.split(",")[0]))?.value || "attention";
}

export default function CarrierInvoices() {
  const { t, locale } = useT();
  const navigate = useNavigate();
  const { can } = useAuth();
  const canWrite = can("billing.write");
  const [params, setParams] = useSearchParams();
  const tabValue = params.get("tab") || tabFor(params.get("status"));
  const tab = TABS.find((x) => x.value === tabValue) || TABS[0];
  const [search, setSearch] = useState(params.get("q") || "");
  const [debounced, setDebounced] = useState(search.trim());
  const [showUpload, setShowUpload] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [note, setNote] = useState(null);
  useEffect(() => {
    const h = setTimeout(() => setDebounced(search.trim()), 250);
    return () => clearTimeout(h);
  }, [search]);

  const q = useApi(`/finance/carrier-invoices${qs({ status: tab.status, q: debounced })}`, { pollMs: 60000 });
  const counts = q.data?.counts || null;
  const setTab = (v) => {
    const next = new URLSearchParams(params);
    next.delete("status");
    if (v && v !== "attention") next.set("tab", v); else next.delete("tab");
    setParams(next, { replace: true });
  };
  const options = useMemo(() => TABS.map((x) => {
    let c;
    if (counts) {
      c = x.status ? x.status.split(",").reduce((a, s) => a + (counts[s] || 0), 0) : Object.values(counts).reduce((a, b) => a + b, 0);
    }
    return { value: x.value, label: t(`fin.ci.tab.${x.value}`), count: c };
  }), [counts, t]);

  const upload = async (files) => {
    setUploading(true);
    setNote(null);
    try {
      const fd = new FormData();
      fd.append("file", files[0]);
      const r = await apiUpload("/finance/carrier-invoices", fd);
      setShowUpload(false);
      if (r?.id) navigate(`/ops/finance/carrier-invoices/${r.id}`, { state: { note: r.status === "duplicate" ? t("fin.ci.uploaded_duplicate") : t("fin.ci.uploaded") } });
      else q.reload();
    } catch (e) {
      setNote({ tone: "error", text: e.message });
    } finally {
      setUploading(false);
    }
  };

  const columns = [
    { key: "supplier", header: t("fin.col.supplier"), sort: (r) => r.supplier?.name || "", className: "max-w-[16rem]",
      cell: (r) => (
        <span className="flex min-w-0 flex-col">
          <span className="truncate font-medium">{r.supplier?.name || <span className="text-ink-tertiary">{t("fin.ci.unknown_supplier")}</span>}</span>
          <span className="truncate text-[11px] text-ink-tertiary">{r.number ? <span className="font-mono">{r.number}</span> : "—"}</span>
        </span>
      ) },
    { key: "shipments", header: t("fin.col.shipments"), hideBelow: "lg", sortable: false,
      cell: (r) => {
        const refs = r.references?.shipment_refs || (r.matches || []).map((m) => m.shipment_ref).filter(Boolean);
        return refs.length ? <span className="whitespace-nowrap text-ink-secondary">{refs.slice(0, 2).join(", ")}{refs.length > 2 ? ` +${refs.length - 2}` : ""}</span> : <span className="text-ink-tertiary">—</span>;
      } },
    { key: "invoice_date", header: t("fin.col.invoice_date"), hideBelow: "md", sort: (r) => r.invoice_date || "",
      cell: (r) => <span className="tabular whitespace-nowrap text-ink-secondary">{isoDate(r.invoice_date, locale)}</span> },
    { key: "due_date", header: t("fin.col.due"), sort: (r) => r.due_date || "", cell: (r) => <span className="tabular whitespace-nowrap">{isoDate(r.due_date, locale)}</span> },
    { key: "total", header: t("fin.col.total"), align: "right", sort: (r) => r.eur?.total_cents ?? r.total_cents,
      cell: (r) => <MoneyCell cents={r.total_cents} currency={r.currency} eurCents={r.eur?.total_cents} strong /> },
    { key: "variance", header: t("fin.col.vs_accrual"), align: "right", hideBelow: "md", sort: (r) => r.audit?.variance_eur_cents,
      cell: (r) => {
        const v = r.audit?.variance_eur_cents;
        if (v === null || v === undefined) return <span className="text-ink-tertiary">—</span>;
        return <span className={cn("whitespace-nowrap", r.audit?.within_tolerance === false && "text-danger-fg")}>{signedEur(v, locale)}</span>;
      } },
    { key: "status", header: t("field.status"), sort: (r) => r.status || "",
      cell: (r) => (
        <span className="inline-flex items-center gap-1.5">
          <FinStatus group="carrier_invoice" value={r.status} />
          {r.decision?.outcome && <AgentMark title={t("fin.ci.agent_decided")} />}
        </span>
      ) },
    { key: "source", header: t("fin.col.source"), hideBelow: "lg", sort: (r) => r.source || "",
      cell: (r) => <span className="text-ink-tertiary">{tEnum(t, "fin.ci_source", r.source)}</span> },
  ];

  const filtered = tabValue !== "all" || !!debounced;
  const attention = counts ? (counts.needs_review || 0) + (counts.disputed || 0) : null;
  return (
    <div>
      <FinBar title={t("fin.tab.carrier_invoices")}
        meta={<>
          {attention !== null && <span className={attention ? "text-foreground" : undefined}>{attention ? t("fin.ci.attention_n", { n: attention }) : t("fin.ci.nothing_waiting")}</span>}
          {counts?.disputed > 0 && <><Dot /><span className="text-danger-fg">{t("fin.ov.disputed", { n: counts.disputed })}</span></>}
          <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
        </>}
        actions={<Button size="sm" onClick={() => setShowUpload((x) => !x)} disabled={!canWrite} title={!canWrite ? t("fin.no_permission") : undefined}>
          <Upload /> {t("fin.ci.upload")}
        </Button>} />
      {showUpload && (
        <FileUpload className="mb-3" onFiles={upload} busy={uploading} accept=".pdf,.xml,.png,.jpg,.jpeg,.webp" label={t("fin.ci.choose")} hint={t("fin.ci.upload_hint")} />
      )}
      <ActionNote note={note} onClose={() => setNote(null)} className="mb-3" />
      <FilterBar search={search} onSearch={setSearch} placeholder={t("fin.ci.search")}
        filters={[{ key: "tab", label: t("field.status"), value: tabValue, onChange: setTab, options }]} />
      <Loadable q={q} skeleton={<LoadingRows rows={10} />} isEmpty={(d) => (d.items || []).length === 0}
        empty={<EmptyState icon={FileText}
          title={filtered ? (tabValue === "attention" && !debounced ? t("fin.ci.none_attention") : t("fin.none_match")) : t("fin.ci.empty")}
          body={filtered ? null : t("fin.ci.empty_body")}
          action={filtered && tabValue !== "attention" ? <Button size="sm" variant="outline" onClick={() => { setSearch(""); setTab("all"); }}>{t("action.clear")}</Button> : null} />}>
        {(d) => (
          <>
            <div className="hidden md:block">
              <FinTable columns={columns} rows={d.items} rowKey="id" onRowClick={(r) => navigate(`/ops/finance/carrier-invoices/${r.id}`)} />
            </div>
            <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] md:hidden">
              {d.items.map((r) => (
                <li key={r.id}>
                  <Link to={`/ops/finance/carrier-invoices/${r.id}`} className="flex items-center gap-3 rounded-md px-1 py-2.5 hover:bg-hover">
                    <div className="flex min-w-0 flex-1 flex-col gap-1">
                      <div className="flex items-start justify-between gap-2">
                        <span className="min-w-0 truncate text-sm font-medium">{r.supplier?.name || t("fin.ci.unknown_supplier")}</span>
                        <MoneyCell cents={r.total_cents} currency={r.currency} eurCents={r.eur?.total_cents} strong />
                      </div>
                      <div className="flex items-center justify-between gap-2 text-xs">
                        <FinStatus group="carrier_invoice" value={r.status} />
                        <span className="tabular text-ink-tertiary">{t("fin.ci.due_on", { date: isoDate(r.due_date, locale) })}</span>
                      </div>
                      <span className="truncate text-xs text-ink-tertiary font-mono">{r.number || "—"}</span>
                    </div>
                    <ChevronRight className="size-4 shrink-0 text-ink-tertiary" aria-hidden />
                  </Link>
                </li>
              ))}
            </ul>
            {d.total > d.items.length && <p className="mt-2 text-xs text-ink-tertiary">{t("fin.showing", { n: d.items.length, total: d.total })}</p>}
          </>
        )}
      </Loadable>
    </div>
  );
}
