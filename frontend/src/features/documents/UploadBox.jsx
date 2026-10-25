// Upload: accepted types and the 20 MB limit are stated before the file is picked; type and shipment are optional.
// useUploader() is shared by the page strip and the per-row "Upload" buttons of the required-documents checklist.
import React, { useRef, useState } from "react";
import { CheckCircle2, Loader2, Upload, X, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { FileUpload } from "@/components/app/FileUpload";
import { apiGet, apiUpload, useApi } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { ACCEPT, MAX_BYTES, fileSize } from "@/features/documents/fields";

let specsCache = null;
/** GET /api/documents/specs once per page load. */
export function useSpecs() {
  const [specs, setSpecs] = useState(specsCache);
  React.useEffect(() => {
    if (specsCache) return;
    let live = true;
    apiGet("/documents/specs").then((s) => { specsCache = s; if (live) setSpecs(s); }).catch(() => {});
    return () => { live = false; };
  }, []);
  return specs;
}

let seq = 0;
export function useUploader({ onUploaded } = {}) {
  const { t, locale } = useT();
  const [results, setResults] = useState([]);
  const [busy, setBusy] = useState(false);
  const patch = (id, p) => setResults((rs) => rs.map((r) => (r.id === id ? { ...r, ...p } : r)));
  const upload = async (files, { type, shipmentId, origin = "box" } = {}) => {
    setBusy(true);
    for (const file of files) {
      const id = ++seq;
      if (file.size > MAX_BYTES) {
        setResults((rs) => [{ id, origin, name: file.name, state: "failed", message: t("doc.upload.too_big", { name: file.name, size: fileSize(file.size, locale) }) }, ...rs]);
        continue;
      }
      setResults((rs) => [{ id, origin, name: file.name, state: "uploading" }, ...rs]);
      const fd = new FormData();
      fd.append("file", file);
      if (type) fd.append("type", type);
      if (shipmentId) fd.append("shipment_id", shipmentId);
      try {
        const doc = await apiUpload("/documents", fd);
        patch(id, { state: "done", doc });
        onUploaded?.(doc);
      } catch (e) {
        patch(id, { state: "failed", message: t("doc.upload.failed", { name: file.name, reason: e.message }) });
      }
    }
    setBusy(false);
  };
  const dismiss = (id) => setResults((rs) => rs.filter((r) => r.id !== id));
  return { upload, busy, results, dismiss };
}

/** Per-file outcome lines: uploading, uploaded (with Open), failed with the server's reason (415 / 400 / 413). */
export function UploadResults({ results: all, origin, onDismiss, onOpen, className }) {
  const { t } = useT();
  const results = origin ? all.filter((r) => r.origin === origin) : all;
  if (!results.length) return null;
  return (
    <ul className={cn("flex flex-col gap-1", className)} aria-live="polite">
      {results.slice(0, 6).map((r) => (
        <li key={r.id} className={cn("flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm",
          r.state === "failed" ? "status-failed" : r.state === "done" ? "bg-subtle" : "bg-subtle text-ink-secondary")}>
          {r.state === "uploading" && <Loader2 className="size-4 shrink-0 animate-spin" aria-hidden />}
          {r.state === "done" && <CheckCircle2 className="size-4 shrink-0 text-success-fg" aria-hidden />}
          {r.state === "failed" && <XCircle className="size-4 shrink-0" aria-hidden />}
          <span className="min-w-0 flex-1 break-words">
            {r.state === "uploading" ? t("doc.upload.uploading", { name: r.name }) : r.state === "done" ? t(r.doc?.extraction?.status === "not_applicable" ? "doc.upload.done_stored" : "doc.upload.done", { name: r.name }) : r.message}
          </span>
          {r.state === "done" && onOpen && (
            <Button variant="outline" onClick={() => onOpen(r.doc)}>{t("doc.panel.view")}</Button>
          )}
          {r.state !== "uploading" && (
            <button type="button" onClick={() => onDismiss(r.id)} aria-label={t("doc.upload.dismiss")} className="rounded-full p-1 hover:bg-hover">
              <X className="size-3.5" />
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

/** Compact button + hidden file input with type and shipment pre-selected (checklist rows). */
export function UploadButton({ uploader, type, shipmentId, origin, label, size = "default", variant = "outline" }) {
  const { t } = useT();
  const ref = useRef(null);
  return (
    <>
      <input ref={ref} type="file" accept={ACCEPT} multiple className="sr-only" tabIndex={-1} aria-hidden
        onChange={(e) => { const f = Array.from(e.target.files || []); e.target.value = ""; if (f.length) uploader.upload(f, { type, shipmentId, origin }); }} />
      <Button size={size} variant={variant} disabled={uploader.busy} onClick={() => ref.current?.click()}>
        <Upload /> {label || t("action.upload")}
      </Button>
    </>
  );
}

/**
 * Upload strip: optional type + shipment, then the drop zone. When shipmentId is given the shipment is fixed.
 * Shipments come from GET /api/shipments; if that fails the select is hidden and the file is uploaded unlinked.
 */
export default function UploadBox({ uploader, shipmentId, types, onOpen, className }) {
  const { t } = useT();
  const [type, setType] = useState("auto");
  const [ship, setShip] = useState("none");
  const shipments = useApi(shipmentId ? null : "/shipments?limit=200");
  const shipItems = shipments.data?.items || [];
  const pick = (files) => uploader.upload(files, { type: type === "auto" ? null : type, shipmentId: shipmentId || (ship === "none" ? null : ship) });
  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div className="flex flex-col gap-2 rounded-xl border border-dashed p-2 lg:flex-row lg:items-center">
        <FileUpload multiple accept={ACCEPT} busy={uploader.busy} label={t("doc.upload.choose")} hint={t("doc.upload.hint")}
          className="min-w-0 flex-1 border-0 px-1 py-0.5 max-sm:[&>span]:hidden" onFiles={pick} />
        <p className="px-1 text-xs text-ink-tertiary sm:hidden">{t("doc.upload.hint")}</p>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <label className="flex items-center gap-2 text-xs text-ink-tertiary">
            <span className="w-24 shrink-0 sm:w-auto">{t("doc.upload.type")}</span>
            <Select value={type} onValueChange={setType}>
              <SelectTrigger className="flex-1 sm:w-48 sm:flex-none" aria-label={t("doc.upload.type")}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">{t("doc.upload.type_auto")}</SelectItem>
                {(types || []).map((ty) => <SelectItem key={ty} value={ty}>{tEnum(t, "doc", ty)}</SelectItem>)}
              </SelectContent>
            </Select>
          </label>
          {!shipmentId && shipItems.length > 0 && (
            <label className="flex items-center gap-2 text-xs text-ink-tertiary">
              <span className="w-24 shrink-0 sm:w-auto">{t("doc.upload.shipment")}</span>
              <Select value={ship} onValueChange={setShip}>
                <SelectTrigger className="flex-1 sm:w-44 sm:flex-none" aria-label={t("doc.upload.shipment")}><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">{t("doc.upload.no_shipment")}</SelectItem>
                  {shipItems.map((s) => <SelectItem key={s.id} value={s.id}>{s.reference}</SelectItem>)}
                </SelectContent>
              </Select>
            </label>
          )}
        </div>
      </div>
      <UploadResults origin="box" results={uploader.results} onDismiss={uploader.dismiss} onOpen={onOpen} />
    </div>
  );
}
