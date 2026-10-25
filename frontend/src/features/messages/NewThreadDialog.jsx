import React, { useCallback, useRef, useState } from "react";
import { Check, Loader2, Paperclip, Send, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { apiPost } from "@/lib/api";
import { useT } from "@/lib/i18n";
import SearchPicker from "./SearchPicker";
import { ACCEPT, fileSize, MAX_BODY, useAttachments } from "./util";

const shipmentsPath = (q) => `/shipments?limit=8${q ? `&q=${encodeURIComponent(q)}` : ""}`;
const customersPath = (q) => `/billing/customers?limit=8${q ? `&q=${encodeURIComponent(q)}` : ""}`;

function ShipmentOption({ s, showCustomer }) {
  return (
    <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2">
      <span className="font-medium tabular">{s.reference}</span>
      <StatusBadge size="sm" group="status" value={s.status} />
      <span className="min-w-0 truncate text-xs text-ink-tertiary">
        {s.origin?.name || "—"} → {s.destination?.name || "—"}{showCustomer && s.customer_name ? ` · ${s.customer_name}` : ""}
      </span>
    </span>
  );
}

/**
 * "Uusi viesti" (customer: subject + optional shipment + body) /
 * "Uusi keskustelu" (ops: customer or shipment + subject + body). onCreated({thread, message}).
 */
export default function NewThreadDialog({ mode = "customer", open, onOpenChange, onCreated, initialShipment = null }) {
  const { t, locale } = useT();
  const [subject, setSubject] = useState("");
  const [shipment, setShipment] = useState(initialShipment);
  const [customer, setCustomer] = useState(null);
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [tried, setTried] = useState(false);
  const att = useAttachments();
  const fileRef = useRef(null);

  const companyId = shipment?.company_id || customer?.id || null;
  const customerFilter = mode === "ops" ? customer?.id : null;
  const shipmentPathFor = useCallback(
    (q) => (customerFilter ? `${shipmentsPath(q)}&company_id=${customerFilter}` : shipmentsPath(q)), [customerFilter]);
  const problems = [
    mode === "ops" && !companyId ? t("messages.new.need_customer") : null,
    !shipment && !subject.trim() ? t("messages.new.need_subject") : null,
    !body.trim() && att.files.length === 0 ? t("messages.new.need_body") : null,
  ].filter(Boolean);

  const reset = () => {
    setSubject(""); setShipment(initialShipment); setCustomer(null); setBody(""); setError(null); setTried(false); att.clear();
  };

  const submit = async (e) => {
    e?.preventDefault();
    setTried(true);
    if (problems.length || busy) return;
    setBusy(true);
    setError(null);
    try {
      const ids = await att.uploadAll(shipment ? { shipment_id: shipment.id } : { company_id: companyId });
      const r = await apiPost("/messages/threads", {
        subject: subject.trim() || null,
        shipment_id: shipment?.id || null,
        ...(mode === "ops" ? { company_id: companyId } : {}),
        body: body.trim(),
        attachments: ids,
      });
      reset();
      onCreated?.(r);
    } catch (err) {
      setError(err?.status === 0 ? t("state.offline") : err?.message || t("state.error"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!busy) onOpenChange(o); }}>
      <DialogContent className="max-h-[calc(100dvh-24px)] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{mode === "ops" ? t("messages.new.ops") : t("messages.new.customer")}</DialogTitle>
          <DialogDescription>{mode === "ops" ? t("messages.new.desc_ops") : t("messages.new.desc_customer")}</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
          {mode === "ops" && (
            <SearchPicker label={t("messages.new.customer_label")} placeholder={t("messages.new.search_customer")}
              value={shipment ? { id: shipment.company_id, name: shipment.customer_name } : customer}
              onChange={setCustomer} disabled={!!shipment} pathFor={customersPath}
              renderItem={(c) => <span className="truncate">{c.name}</span>} hint={!companyId ? t("messages.new.target_hint") : null} />
          )}
          <SearchPicker label={mode === "ops" ? t("messages.new.shipment_ops") : t("messages.new.shipment")}
            placeholder={t("messages.new.search_shipment")} value={shipment} onChange={setShipment}
            pathFor={shipmentPathFor}
            renderItem={(s) => <ShipmentOption s={s} showCustomer={mode === "ops"} />} />
          <div className="flex flex-col gap-1">
            <label htmlFor="msg-new-subject" className="text-xs text-ink-secondary">{shipment ? t("messages.new.subject_optional") : t("messages.new.subject")}</label>
            <Input id="msg-new-subject" value={subject} onChange={(e) => setSubject(e.target.value)} maxLength={200}
              placeholder={shipment ? shipment.reference : t("messages.new.subject_ph")} aria-invalid={tried && !shipment && !subject.trim()} />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="msg-new-body" className="text-xs text-ink-secondary">{t("messages.new.body")}</label>
            <Textarea id="msg-new-body" value={body} onChange={(e) => setBody(e.target.value)} rows={5} maxLength={MAX_BODY}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit(e); }}
              aria-invalid={tried && !body.trim() && att.files.length === 0} />
          </div>
          {att.files.length > 0 && (
            <ul className="flex flex-wrap gap-1.5">
              {att.files.map((f) => (
                <li key={f.key} className="inline-flex h-6 max-w-full items-center gap-1 rounded-full bg-secondary pl-2 pr-1 text-xs">
                  {f.docId ? <Check className="size-3 shrink-0 text-success-fg" aria-hidden /> : <Paperclip className="size-3 shrink-0 text-ink-tertiary" aria-hidden />}
                  <span className="max-w-[200px] truncate">{f.file.name}</span>
                  <span className="tabular text-ink-tertiary">{fileSize(f.file.size, locale)}</span>
                  <button type="button" disabled={busy} onClick={() => att.remove(f.key)} aria-label={t("messages.composer.remove_file", { name: f.file.name })}
                    className="rounded-full p-0.5 hover:bg-hover"><X className="size-3" /></button>
                </li>
              ))}
            </ul>
          )}
          {tried && problems.length > 0 && <p className="text-xs text-danger-fg" role="alert">{problems.join(" · ")}</p>}
          {error && <ErrorState compact error={{ message: t("messages.composer.failed", { error }) }} onRetry={submit} />}
          <div className="flex flex-wrap items-center gap-2">
            <input ref={fileRef} type="file" multiple accept={ACCEPT} className="sr-only" tabIndex={-1} aria-hidden
              onChange={(e) => { att.add(e.target.files); e.target.value = ""; }} />
            <Button variant="ghost" size="sm" onClick={() => fileRef.current?.click()} disabled={busy}>
              <Paperclip /> {t("messages.composer.attach")}
            </Button>
            <span className="ml-auto text-xs text-ink-tertiary" aria-live="polite">
              {att.progress ? t("messages.composer.uploading", att.progress) : busy ? t("messages.composer.sending") : null}
            </span>
            <Button type="button" variant="ghost" size="sm" onClick={() => onOpenChange(false)} disabled={busy}>{t("action.cancel")}</Button>
            <Button type="submit" size="sm" disabled={busy}>
              {busy ? <Loader2 className="animate-spin" /> : <Send />} {t("messages.new.create")}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
