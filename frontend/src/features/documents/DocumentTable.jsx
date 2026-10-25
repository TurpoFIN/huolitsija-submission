// Dense documents list: table from md up, card rows on phones. Row click opens the review (?doc=<id>).
import React from "react";
import { Link } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { DataTable } from "@/components/app/DataTable";
import { StatusBadge } from "@/components/app/StatusBadge";
import { LoadingRows } from "@/components/app/states";
import { dateTime, relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { ProvenanceChip } from "@/features/documents/Provenance";
import { fileSize, tn } from "@/features/documents/fields";

/** Extraction status; a confirmed document reads "Confirmed" (a person checked it). */
export function DocStatus({ doc, size = "sm" }) {
  const { t } = useT();
  const ex = doc.extraction || {};
  const confirmed = ex.status === "done" && ex.confirmed_by;
  return (
    <span className="inline-flex items-center gap-1.5">
      {confirmed
        ? <StatusBadge group="extraction" value="done" label={t("doc.state.verified")} size={size} />
        : <StatusBadge group="extraction" value={ex.status} size={size} />}
      {!confirmed && ex.status !== "not_applicable" && <ProvenanceChip source={ex.source} />}
    </span>
  );
}

export function Issues({ doc }) {
  const { t } = useT();
  const w = doc.warnings_count || 0;
  const c = doc.failed_checks || 0;
  if (!w && !c) return <span className="text-ink-tertiary">{t("doc.issues.none")}</span>;
  return (
    <span className="inline-flex flex-col leading-tight">
      {c > 0 && <span className="text-danger-fg whitespace-nowrap">{tn(t, "doc.checks.failed_n", c)}</span>}
      {w > 0 && <span className="text-warning-fg whitespace-nowrap">{tn(t, "doc.issues.n", w)}</span>}
    </span>
  );
}

function ShipmentLink({ doc, base }) {
  const { t } = useT();
  if (!doc.shipment_id) return <span className="text-ink-tertiary">{t("doc.unlinked")}</span>;
  return (
    <Link to={`${base}/shipments/${doc.shipment_id}`} onClick={(e) => e.stopPropagation()} className="tabular whitespace-nowrap hover:underline underline-offset-4">
      {doc.shipment_ref || doc.shipment_id.slice(0, 8)}
    </Link>
  );
}

export default function DocumentTable({ rows, loading, empty, onOpen, showShipment = true, showCompany = false, base = "/portal", selectedId }) {
  const { t, locale } = useT();
  if (loading && !rows) return <LoadingRows />;
  if (rows && rows.length === 0 && empty) return empty;

  const columns = [
    { key: "type", header: t("doc.col.type"), sort: (r) => tEnum(t, "doc", r.type),
      cell: (r) => <span className="whitespace-nowrap">{tEnum(t, "doc", r.type)}</span> },
    { key: "filename", header: t("doc.col.document"), className: "max-w-[320px]",
      cell: (r) => (
        <span className="flex min-w-0 flex-col">
          <span className="truncate font-medium" title={r.filename}>{r.filename}</span>
          {r.number && <span className="truncate text-xs text-ink-tertiary tabular">{r.number}</span>}
        </span>
      ) },
    showCompany && { key: "company_name", header: t("doc.col.company"), hideBelow: "lg" },
    showShipment && { key: "shipment_ref", header: t("doc.col.shipment"), cell: (r) => <ShipmentLink doc={r} base={base} /> },
    { key: "status", header: t("doc.col.status"), sort: (r) => r.extraction?.status, cell: (r) => <DocStatus doc={r} /> },
    { key: "issues", header: t("doc.col.issues"), sort: (r) => (r.failed_checks || 0) * 100 + (r.warnings_count || 0), cell: (r) => <Issues doc={r} /> },
    { key: "uploaded_at", header: t("doc.col.uploaded"), align: "right",
      cell: (r) => <span className="whitespace-nowrap text-ink-secondary" title={dateTime(r.uploaded_at, locale)}>{relTime(r.uploaded_at, locale)}</span> },
    { key: "size", header: t("doc.col.size"), align: "right", hideBelow: "lg",
      cell: (r) => <span className="whitespace-nowrap text-ink-secondary">{fileSize(r.size, locale)}</span> },
  ].filter(Boolean);

  return (
    <>
      <div className="hidden md:block overflow-x-auto">
        <DataTable dense columns={columns} rows={rows} onRowClick={(r) => onOpen(r)}
          rowClassName={(r) => cn(r.id === selectedId && "bg-hover")} />
      </div>
      <ul className="md:hidden divide-y divide-[color:var(--border-subtle)] border-y border-[color:var(--border-subtle)]">
        {(rows || []).map((r) => (
          <li key={r.id}>
            <button type="button" onClick={() => onOpen(r)} className="flex w-full items-start gap-2 px-1 py-2.5 text-left hover:bg-hover">
              <span className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium">{tEnum(t, "doc", r.type)}</span>
                  <DocStatus doc={r} />
                </span>
                <span className="truncate text-sm text-ink-secondary">{r.filename}{r.number ? ` · ${r.number}` : ""}</span>
                <span className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-ink-tertiary">
                  {showShipment && <ShipmentLink doc={r} base={base} />}
                  <span>{relTime(r.uploaded_at, locale)}</span>
                  <Issues doc={r} />
                </span>
              </span>
              <ChevronRight className="mt-0.5 size-4 shrink-0 text-ink-tertiary" aria-hidden />
            </button>
          </li>
        ))}
      </ul>
    </>
  );
}
