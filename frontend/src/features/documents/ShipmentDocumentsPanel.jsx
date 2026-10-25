// Shipment documents panel (embedded in shipment detail and the ?shipment= view of the Documents page):
// required-documents checklist (DOC-2) with per-row upload, DOC-3 tie-out, the shipment's documents, review sheet.
import React, { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Loadable, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { num } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import DocumentReview from "@/features/documents/DocumentReview";
import DocumentTable from "@/features/documents/DocumentTable";
import UploadBox, { UploadButton, UploadResults, useSpecs, useUploader } from "@/features/documents/UploadBox";
import { isReading, localized, tn } from "@/features/documents/fields";

// Checklist state -> one of the nine visual workflow states.
const STATE_VISUAL = { missing: "pending", extracting: "in-progress", needs_review: "in-review", uploaded: "success",
  verified: "success", on_file: "success", failed: "failed" };

function order(i) {
  if (!i.present && i.required) return 0;
  if (["needs_review", "failed"].includes(i.state)) return 1;
  if (i.required) return 2;
  return i.present ? 4 : 3;
}

function RequiredRow({ item, mode, uploader, shipmentId, onOpen }) {
  const { t, locale } = useT();
  const reason = localized(item, "reason", locale);
  const [why, setWhy] = React.useState(false);
  const missing = !item.present;
  const state = missing && !item.required ? "expired" : STATE_VISUAL[item.state] || "expired";
  const from = item.expected_from === "customer" && mode === "ops" ? "customer_ops" : item.expected_from;
  return (
    <li className="border-b border-[color:var(--border-subtle)] py-2 last:border-0">
      <div className="flex items-start gap-3 sm:items-center">
        <div className="flex min-w-0 flex-1 flex-col">
          <span className={cn("text-sm", missing && item.required ? "font-medium" : "")}>{tEnum(t, "doc", item.type)}</span>
          <span className="text-xs text-ink-tertiary">
            {t(item.required ? "doc.req.required" : "doc.req.optional")}
            {from && <> · {t(`doc.req.from.${from}`)}</>}
            {item.alternatives?.length > 0 && <> · {t("doc.panel.alternatives", { list: item.alternatives.map((a) => tEnum(t, "doc", a)).join(", ") })}</>}
            {reason && (
              <>
                {" · "}
                <button type="button" onClick={() => setWhy((w) => !w)} aria-expanded={why}
                  className="inline-flex items-center gap-0.5 hover:text-foreground">
                  {t("doc.panel.why")} <ChevronDown className={cn("size-3 transition-transform", why && "rotate-180")} aria-hidden />
                </button>
              </>
            )}
          </span>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1.5 sm:flex-row sm:items-center">
          <StatusBadge size="sm" state={state} label={t(`doc.state.${item.state}`)} />
          {item.document_ids?.length > 0 && (
            <Button variant="ghost" onClick={() => onOpen(item.document_ids[0])}>{t("doc.panel.view")}</Button>
          )}
          {(missing || item.state === "failed") && (
            <UploadButton uploader={uploader} origin="checklist" type={item.type} shipmentId={shipmentId} variant={missing && item.required ? "default" : "outline"} />
          )}
        </div>
      </div>
      {why && reason && <p className="mt-1 max-w-prose text-xs text-ink-secondary">{reason}</p>}
    </li>
  );
}

function TieOut({ q }) {
  const { t, locale } = useT();
  return (
    <Loadable q={q} skeleton={<LoadingRows rows={3} />}>
      {(d) => (d.items || []).length === 0 ? (
        <p className="py-1 text-sm text-ink-tertiary">{t("doc.checks.none")}</p>
      ) : (
        <ul>
          {d.items.map((c) => (
            <li key={c.key} className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-3 border-b border-[color:var(--border-subtle)] py-1.5 last:border-0 sm:grid-cols-[10rem_minmax(0,1fr)_auto]">
              <span className="text-sm">{t(`doc.check.${c.key}`)}</span>
              <span className="col-span-2 row-start-2 text-xs text-ink-tertiary sm:col-span-1 sm:col-start-2 sm:row-start-1">
                {(c.values || []).map((v, i) => (
                  <span key={i} className="mr-3 inline-block whitespace-nowrap">
                    {tEnum(t, "doc", v.type)}{" "}
                    <span className="tabular text-ink-secondary">{Array.isArray(v.value) ? v.value.join(", ") : typeof v.value === "number" ? num(v.value, locale, 3) : v.value ?? "—"}</span>
                  </span>
                ))}
                {c.ok === false && c.message && <span className="block text-danger-fg">{localized(c, "message", locale)}</span>}
              </span>
              <StatusBadge size="sm" className="col-start-2 row-start-1 sm:col-start-3" state={c.ok === true ? "success" : c.ok === false ? "failed" : "expired"}
                label={t(c.ok === true ? "doc.checks.ok" : c.ok === false ? "doc.checks.fail" : "doc.checks.na")} />
            </li>
          ))}
        </ul>
      )}
    </Loadable>
  );
}

export default function ShipmentDocumentsPanel({ shipment, mode = "portal", onChange }) {
  const { t } = useT();
  const id = shipment?.id;
  const base = mode === "ops" ? "/ops" : "/portal";
  const [params, setParams] = useSearchParams();
  const docId = params.get("doc");
  const specs = useSpecs();

  const [poll, setPoll] = React.useState(false);
  const docs = useApi(id ? `/documents?shipment_id=${id}` : null, { pollMs: poll ? 3000 : undefined });
  const anyReading = (docs.data?.items || []).some((d) => isReading(d.extraction?.status));
  const req = useApi(id ? `/documents/required?shipment_id=${id}` : null);
  const tie = useApi(id ? `/documents/tieout?shipment_id=${id}` : null);

  // Poll the list while something is being read; when reading ends, refresh the checklist and tie-out once.
  const wasReading = React.useRef(false);
  const refreshAll = () => {
    docs.reload();
    req.reload();
    tie.reload();
    onChange?.();
  };
  React.useEffect(() => {
    setPoll(anyReading);
    if (wasReading.current && !anyReading) refreshAll();
    wasReading.current = anyReading;
  }, [anyReading]); // eslint-disable-line react-hooks/exhaustive-deps

  const uploader = useUploader({ onUploaded: refreshAll });
  const open = useCallback((doc) => {
    const next = new URLSearchParams(params);
    next.set("doc", typeof doc === "string" ? doc : doc.id);
    setParams(next, { replace: false });
  }, [params, setParams]);
  const close = () => {
    const next = new URLSearchParams(params);
    next.delete("doc");
    setParams(next, { replace: true });
  };

  const reqItems = useMemo(() => [...(req.data?.items || [])].sort((a, b) => order(a) - order(b)), [req.data]);
  const missing = req.data?.missing ?? 0;

  if (!id) return null;
  return (
    <div>
      <Section title={t("doc.panel.required")}
        actions={req.data && (missing > 0
          ? <span className="text-sm text-warning-fg tabular">{t("doc.panel.missing_n", { n: missing })}</span>
          : <span className="text-sm text-success-fg">{t("doc.panel.complete")}</span>)}>
        <Loadable q={req} skeleton={<LoadingRows rows={4} />}>
          {() => (
            <ul>
              {reqItems.map((it) => (
                <RequiredRow key={it.type} item={it} mode={mode} uploader={uploader} shipmentId={id} onOpen={open} />
              ))}
            </ul>
          )}
        </Loadable>
        <UploadResults className="mt-2" origin="checklist" results={uploader.results} onDismiss={uploader.dismiss} onOpen={open} />
      </Section>

      <Section title={t("doc.checks.title")}
        actions={tie.data?.failed > 0 && <span className="text-sm text-danger-fg tabular">{tn(t, "doc.checks.failed_n", tie.data.failed)}</span>}>
        <p className="mb-1 text-xs text-ink-tertiary">{t("doc.checks.rule")}</p>
        <TieOut q={tie} />
      </Section>

      <Section title={t("doc.panel.documents")}
        actions={docs.data && <span className="text-xs text-ink-tertiary tabular">{docs.data.total}</span>}>
        {docs.error && docs.data && <ErrorState compact className="mb-2" error={{ message: t("state.failed_refresh") }} onRetry={docs.reload} />}
        {docs.error && !docs.data ? (
          <ErrorState error={docs.error} onRetry={docs.reload} />
        ) : (
          <DocumentTable rows={docs.data?.items} loading={docs.loading} onOpen={open} showShipment={false} base={base} selectedId={docId}
            empty={<EmptyState title={t("doc.panel.none")} body={t("doc.panel.none_body")} />} />
        )}
        <UploadBox className="mt-3" uploader={uploader} shipmentId={id} types={specs?.types} onOpen={open} />
      </Section>

      <DocumentReview docId={docId} onClose={close} onChanged={refreshAll} base={base} />
    </div>
  );
}
