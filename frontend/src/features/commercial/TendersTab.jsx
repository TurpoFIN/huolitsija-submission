// Tarjouskilpailut: upload an RFQ sheet, let the tenders agent price every lane, return the filled file, record the award.
import React, { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { AlertTriangle, FileSpreadsheet } from "lucide-react";
import { EmptyState } from "@/components/app/states";
import { DataTable } from "@/components/app/DataTable";
import { FileUpload } from "@/components/app/FileUpload";
import { apiUpload, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, num, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { NativeSelect, useIsNarrow } from "@/features/quotes/ops/common";
import { ComBadge, ComLoadable, Eur, NeedsLine, mutationError } from "./common";
import { listOf, tender as normTender } from "./shapes";

export function useCustomerOptions(enabled = true) {
  const q = useApi(enabled ? "/ops/customers?limit=500" : null);
  return useMemo(() => listOf(q.data).map((c) => ({ value: c.id, label: c.business_id ? `${c.name} · ${c.business_id}` : c.name }))
    .sort((a, b) => a.label.localeCompare(b.label)), [q.data]);
}

export default function TendersTab() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("quotes.write");
  const narrow = useIsNarrow();
  const navigate = useNavigate();
  const q = useApi("/tenders", { pollMs: 30000 });
  const customers = useCustomerOptions(canWrite);
  const [companyId, setCompanyId] = useState(null);
  const [busy, setBusy] = useState(false);
  const rows = useMemo(() => listOf(q.data, "tenders").map(normTender)
    .sort((a, b) => String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || ""))), [q.data]);
  const toPrice = rows.filter((r) => ["uploaded", "parsed", "draft", "needs_review"].includes(r.status)).length;
  const toReturn = rows.filter((r) => r.status === "priced").length;
  const awaiting = rows.filter((r) => ["returned", "submitted"].includes(r.status)).length;
  const pricing = rows.filter((r) => r.status === "pricing").length;

  const upload = async (files) => {
    const f = files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    if (companyId) fd.append("company_id", companyId);
    setBusy(true);
    try {
      const r = await apiUpload("/tenders", fd);
      toast.success(t("com.tender.uploaded", { n: num((r?.lanes || []).length || r?.lane_count || 0, locale) }));
      q.reload();
      if (r?.id) navigate(`/ops/commercial/tenders/${r.id}`);
    } catch (e) {
      toast.error(mutationError(e, t, locale));
    } finally {
      setBusy(false);
    }
  };

  const columns = [
    { key: "name", header: t("com.tender.name"), sort: (r) => String(r.name).toLowerCase(), cell: (r) => (
      <span className="flex min-w-0 max-w-[280px] flex-col">
        <Link to={`/ops/commercial/tenders/${r.id}`} className="truncate font-medium hover:underline" onClick={(e) => e.stopPropagation()}>{r.name}</Link>
        <span className="truncate text-xs text-ink-tertiary">{r.customer || t("com.tender.no_customer")}</span>
      </span>
    ) },
    { key: "status", header: t("field.status"), cell: (r) => <ComBadge group="tender" value={r.status} /> },
    { key: "lanes", header: t("com.tender.lanes"), align: "right", sort: (r) => r.lane_count, cell: (r) => (
      <span className="whitespace-nowrap">{num(r.priced_count, locale)} / {num(r.lane_count, locale)}</span>
    ) },
    { key: "total_annual_cents", header: t("com.tender.annual_value"), align: "right", cell: (r) => <Eur cents={r.total_annual_cents} /> },
    { key: "due_at", header: t("com.tender.due"), hideBelow: "md", cell: (r) => <span className="whitespace-nowrap text-xs">{r.due_at ? date(r.due_at, locale) : "—"}</span> },
    { key: "updated_at", header: t("field.updated"), hideBelow: "lg", cell: (r) => <span className="text-xs text-ink-tertiary">{relTime(r.updated_at || r.created_at, locale)}</span> },
  ];

  return (
    <div>
      <NeedsLine items={[
        toReturn > 0 && { tone: "warning", icon: AlertTriangle, text: t("com.tender.to_return_n", { n: num(toReturn, locale) }) },
        toPrice > 0 && { tone: "warning", text: t("com.tender.to_price_n", { n: num(toPrice, locale) }) },
        pricing > 0 && { text: t("com.tender.pricing_n", { n: num(pricing, locale) }) },
        awaiting > 0 && { text: t("com.tender.awaiting_n", { n: num(awaiting, locale) }) },
      ]} quiet={q.data ? t("com.tender.nothing_waiting") : null} />

      {canWrite && (
        <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center">
          <NativeSelect className="sm:w-72" aria-label={t("com.tender.customer")} value={companyId} onChange={setCompanyId}
            placeholder={t("com.tender.customer_optional")} options={customers} />
          <FileUpload className="flex-1" accept=".xlsx,.csv" onFiles={upload} busy={busy} label={t("com.tender.upload")} hint={t("com.tender.upload_hint")} />
        </div>
      )}

      <ComLoadable q={q} perm="quotes.write" isEmpty={() => rows.length === 0}
        empty={<EmptyState icon={FileSpreadsheet} title={t("com.tender.empty")} body={t("com.tender.empty_body")} />}>
        {() => (narrow ? (
          <ul className="divide-y divide-[color:var(--border-subtle)] border-y">
            {rows.map((r) => (
              <li key={r.id}>
                <Link to={`/ops/commercial/tenders/${r.id}`} className="flex flex-col gap-1 px-1 py-3 hover:bg-hover">
                  <span className="flex items-start justify-between gap-3">
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-medium">{r.name}</span>
                      <span className="block truncate text-xs text-ink-tertiary">{r.customer || t("com.tender.no_customer")}</span>
                    </span>
                    <ComBadge group="tender" value={r.status} />
                  </span>
                  <span className="flex flex-wrap gap-x-3 text-xs text-ink-secondary">
                    <span>{t("com.tender.lanes_priced", { p: num(r.priced_count, locale), n: num(r.lane_count, locale) })}</span>
                    <Eur cents={r.total_annual_cents} />
                    {r.due_at && <span>{t("com.tender.due")}: {date(r.due_at, locale)}</span>}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <div className="overflow-x-auto"><DataTable columns={columns} rows={rows} dense onRowClick={(r) => navigate(`/ops/commercial/tenders/${r.id}`)} /></div>
        ))}
      </ComLoadable>
    </div>
  );
}
