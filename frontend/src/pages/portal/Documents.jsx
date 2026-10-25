// Portal: documents. Decision: "which documents need my check?" -> needs-check count first, dense list, review sheet (?doc=).
// ?shipment=<id> switches to the shipment's checklist view (ShipmentDocumentsPanel).
import React, { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ArrowUpRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { PageHeader } from "@/components/app/layout";
import { FilterBar } from "@/components/app/FilterBar";
import { EmptyState, Freshness, Loadable } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import DocumentReview from "@/features/documents/DocumentReview";
import DocumentTable from "@/features/documents/DocumentTable";
import ShipmentDocumentsPanel from "@/features/documents/ShipmentDocumentsPanel";
import UploadBox, { useSpecs, useUploader } from "@/features/documents/UploadBox";
import { READING, isReading, tn } from "@/features/documents/fields";

function useParam(params, setParams) {
  return (key, value, replace = true) => {
    const next = new URLSearchParams(params);
    if (value === null || value === undefined || value === "") next.delete(key);
    else next.set(key, value);
    setParams(next, { replace });
  };
}

function ShipmentView({ shipmentId }) {
  const { t } = useT();
  const s = useApi(`/shipments/${shipmentId}`);
  const ref = s.data?.reference || s.data?.shipment?.reference || shipmentId.slice(0, 8);
  return (
    <div>
      <PageHeader title={ref} crumbs={[{ label: t("doc.title"), to: "/portal/documents" }, { label: ref }]}
        actions={<Button asChild variant="outline"><Link to={`/portal/shipments/${shipmentId}`}>{t("doc.panel.open_shipment")} <ArrowUpRight /></Link></Button>} />
      <ShipmentDocumentsPanel shipment={{ id: shipmentId, reference: ref }} mode="portal" />
    </div>
  );
}

function ListView({ params, setParams }) {
  const { t } = useT();
  const set = useParam(params, setParams);
  const status = params.get("status") || null;
  const type = params.get("type") || null;
  const docId = params.get("doc");
  const [search, setSearch] = useState(params.get("q") || "");
  const q = params.get("q") || "";
  const specs = useSpecs();

  useEffect(() => {
    const tm = setTimeout(() => { if (search.trim() !== q) set("q", search.trim()); }, 300);
    return () => clearTimeout(tm);
  }, [search]); // eslint-disable-line react-hooks/exhaustive-deps

  const qs = new URLSearchParams();
  if (q) qs.set("q", q);
  if (type) qs.set("type", type);
  if (status) qs.set("status", status === "reading" ? READING.join(",") : status);
  const [poll, setPoll] = useState(false);
  const list = useApi(`/documents?${qs.toString()}`, { pollMs: poll ? 3000 : undefined });
  const filtered = !!(q || type);
  const overall = useApi(filtered ? "/documents?limit=1" : null, { pollMs: poll ? 3000 : undefined });
  const counts = list.data?.counts || {};
  const metaCounts = (filtered ? overall.data?.counts : counts) || {};
  const anyReading = READING.some((s) => (metaCounts[s] || 0) > 0) || (list.data?.items || []).some((d) => isReading(d.extraction?.status));
  useEffect(() => setPoll(anyReading), [anyReading]);

  const rows = list.data?.items;

  const reload = () => { list.reload(); if (filtered) overall.reload(); };
  const uploader = useUploader({ onUploaded: reload });
  const shipments = useApi("/shipments?limit=200");

  const sum = (o, keys) => keys.reduce((a, k) => a + (o[k] || 0), 0);
  const allCount = Object.values(counts).reduce((a, n) => a + n, 0);
  const needCheck = sum(metaCounts, ["needs_review", "failed"]);
  const readingN = sum(metaCounts, READING);
  const totalAll = Object.values(metaCounts).reduce((a, n) => a + n, 0);
  const segs = [
    { value: null, label: t("doc.filter.all"), count: allCount },
    { value: "needs_review", label: t("doc.filter.needs_check"), count: counts.needs_review || 0 },
    { value: "reading", label: t("doc.filter.reading"), count: sum(counts, READING) },
    { value: "done", label: t("doc.filter.done"), count: counts.done || 0 },
    { value: "failed", label: t("doc.filter.failed"), count: counts.failed || 0 },
    counts.not_applicable ? { value: "not_applicable", label: tEnum(t, "extraction", "not_applicable"), count: counts.not_applicable } : null,
  ].filter(Boolean);

  const open = (doc) => set("doc", doc.id, false);
  const anyFilter = !!(q || type || status);

  return (
    <div>
      <PageHeader title={t("doc.title")}
        meta={list.data && (
          <>
            {needCheck > 0 ? (
              <button type="button" onClick={() => set("status", "needs_review")} className="text-warning-fg hover:underline underline-offset-4">
                {tn(t, "doc.meta.need_check", needCheck)}
              </button>
            ) : <span>{t("doc.meta.all_clear")}</span>}
            {readingN > 0 && <span className="text-ink-tertiary">· {t("doc.meta.reading", { n: readingN })}</span>}
            <span className="text-ink-tertiary">· {tn(t, "doc.meta.total", totalAll)}</span>
          </>
        )}
        actions={<Freshness at={list.updatedAt} onRefresh={reload} refreshing={list.refreshing} />} />

      <div id="doc-upload" className="mb-5">
        <UploadBox uploader={uploader} types={specs?.types} onOpen={open} />
      </div>

      <FilterBar search={search} onSearch={setSearch} placeholder={t("doc.search")}
        filters={[{ key: "status", value: status, onChange: (v) => set("status", v), options: segs, label: t("doc.filter.status") }]}
        right={
          <>
            <Select value={type || "all"} onValueChange={(v) => set("type", v === "all" ? null : v)}>
              <SelectTrigger className="w-full sm:w-40" aria-label={t("doc.filter.type")}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t("doc.filter.any_type")}</SelectItem>
                {(specs?.types || []).map((ty) => <SelectItem key={ty} value={ty}>{tEnum(t, "doc", ty)}</SelectItem>)}
              </SelectContent>
            </Select>
            {(shipments.data?.items || []).length > 0 && (
              <Select value="all" onValueChange={(v) => set("shipment", v === "all" ? null : v, false)}>
                <SelectTrigger className="w-full sm:w-40" aria-label={t("doc.filter.shipment")}><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t("doc.filter.any_shipment")}</SelectItem>
                  {shipments.data.items.map((s) => <SelectItem key={s.id} value={s.id}>{s.reference}</SelectItem>)}
                </SelectContent>
              </Select>
            )}
          </>
        } />

      <Loadable q={list}>
        {() => (
          <DocumentTable rows={rows} onOpen={open} selectedId={docId}
            empty={anyFilter ? (
              <EmptyState title={t("doc.empty.filtered")}
                action={<Button variant="outline" onClick={() => { setSearch(""); setParams(new URLSearchParams(), { replace: true }); }}>{t("doc.clear_filters")}</Button>} />
            ) : (
              <EmptyState title={t("doc.empty.title")} body={t("doc.empty.body")}
                action={<Button variant="outline" onClick={() => document.querySelector("#doc-upload [data-slot=button]")?.click()}>{t("doc.upload.choose")}</Button>} />
            )} />
        )}
      </Loadable>

      <DocumentReview docId={docId} onClose={() => set("doc", null)} onChanged={reload} />
    </div>
  );
}

export default function Documents() {
  const [params, setParams] = useSearchParams();
  const shipment = params.get("shipment");
  if (shipment) return <ShipmentView key={shipment} shipmentId={shipment} />;
  return <ListView params={params} setParams={setParams} />;
}
