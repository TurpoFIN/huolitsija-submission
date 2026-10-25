// Audited broker override for RULE E-1 when EU EOS does not confirm a customer's EORI (ops only).
import React, { useState } from "react";
import { FileText, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { apiPost, fileUrl, useApi } from "@/lib/api";
import { dateShort, dateTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { CHECK_STATE, Field, actorName, checkSummary } from "./common";

const NONE = "__none__";

function NoteForm({ label, hint, consequence, submitLabel, onSubmit, onCancel, children, destructive }) {
  const { t } = useT();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const ok = note.trim().length >= 5;
  const submit = async (e) => {
    e.preventDefault();
    if (!ok) return;
    setBusy(true);
    setErr(null);
    try {
      await onSubmit(note.trim());
    } catch (ex) {
      setErr(ex);
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="mt-2 flex flex-col gap-2 rounded-lg border px-3 py-2.5" noValidate>
      {consequence && <p className="text-xs text-ink-secondary">{consequence}</p>}
      <Field label={label} htmlFor="eori-note" hint={hint}>
        <Textarea id="eori-note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} className="min-h-[56px]" disabled={busy} autoFocus />
      </Field>
      {children}
      {err && <ErrorState compact error={err} />}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" variant={destructive ? "destructive" : "default"} disabled={!ok || busy}>{busy ? t("state.saving") : submitLabel}</Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>{t("action.cancel")}</Button>
      </div>
    </form>
  );
}

function EvidencePicker({ companyId, value, onChange }) {
  const { t, locale } = useT();
  const q = useApi(`/documents?company_id=${companyId}&limit=100`);
  const docs = q.data?.items || [];
  return (
    <Field label={t("customer.eori.evidence")} htmlFor="eori-doc" error={q.error ? q.error.message : null}>
      <Select value={value || NONE} onValueChange={(v) => onChange(v === NONE ? null : v)} disabled={q.loading && !q.data}>
        <SelectTrigger id="eori-doc"><SelectValue placeholder={q.loading ? t("state.loading") : t("customer.eori.no_evidence")} /></SelectTrigger>
        <SelectContent>
          <SelectItem value={NONE}>{t("customer.eori.no_evidence")}</SelectItem>
          {docs.map((d) => (
            <SelectItem key={d.id} value={d.id}>{`${d.filename} · ${tEnum(t, "doc", d.type)} · ${dateShort(d.uploaded_at, locale)}`}</SelectItem>
          ))}
        </SelectContent>
      </Select>
    </Field>
  );
}

/** Shown inside the EORI checklist row on the ops customer page. */
export function EoriVerify({ company, row, onDone }) {
  const { t, locale } = useT();
  const [mode, setMode] = useState(null); // "verify" | "revoke"
  const [docId, setDocId] = useState(null);
  const canDecide = !!company.can?.decide;
  const eori = company.eori;

  const post = async (status, note) => {
    const res = await apiPost(`/ops/customers/${company.id}/eori-verification`, { status, note, document_id: status === "verified_manually" ? docId : null });
    toast.success(status === "revoked" ? t("customer.eori.revoked") : t("customer.eori.done"));
    setMode(null);
    setDocId(null);
    onDone(res);
  };

  if (row.status === "manual" && row.manual) {
    const m = row.manual;
    const reg = row.registry_result;
    return (
      <div className="mt-2 flex flex-col gap-1.5 rounded-lg bg-subtle px-3 py-2 text-sm">
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <ShieldCheck className="size-4 text-ink-secondary" aria-hidden />
          <span className="font-medium">{t("customer.eori.by", { name: actorName(m.by) || "—", time: dateTime(m.at, locale) })}</span>
          {m.eori && <span className="font-mono text-xs text-ink-tertiary">{m.eori}</span>}
        </p>
        {m.note && <p className="text-ink-secondary">“{m.note}”</p>}
        {m.document_id && (
          <a href={fileUrl(`/documents/${m.document_id}/file`)} target="_blank" rel="noreferrer"
            className="inline-flex items-center gap-1.5 self-start text-xs text-ink-accent hover:underline">
            <FileText className="size-3.5" aria-hidden /> {t("customer.eori.evidence_link")}
          </a>
        )}
        {reg && (
          <p className="flex flex-wrap items-baseline gap-x-2 text-xs text-ink-tertiary">
            <span>{t("customer.eori.registry")}:</span>
            {reg.status && <StatusBadge size="sm" state={CHECK_STATE[reg.status] || "expired"} label={t(`company.cs.${reg.status}`)} />}
            <span className="text-ink-secondary">{checkSummary(reg, locale) || "—"}</span>
            {reg.checked_at && <span>· {dateTime(reg.checked_at, locale)}</span>}
          </p>
        )}
        {canDecide && mode !== "revoke" && (
          <Button size="xs" variant="outline" className="self-start" onClick={() => setMode("revoke")}>{t("customer.eori.revoke")}</Button>
        )}
        {mode === "revoke" && (
          <NoteForm label={t("customer.eori.revoke_note")} hint={t("customer.eori.note_hint")} destructive
            consequence={t("customer.eori.revoke_consequence", { status: reg?.status ? t(`company.cs.${reg.status}`) : "—" })}
            submitLabel={t("customer.eori.revoke_confirm")} onSubmit={(note) => post("revoked", note)} onCancel={() => setMode(null)} />
        )}
      </div>
    );
  }

  if (!eori || !["fail", "error"].includes(row.status) || !canDecide) return null;
  return mode === "verify" ? (
    <NoteForm label={t("customer.eori.note")} hint={t("customer.eori.note_hint")}
      consequence={t("customer.eori.consequence", { eori })} submitLabel={t("customer.eori.confirm")}
      onSubmit={(note) => post("verified_manually", note)} onCancel={() => { setMode(null); setDocId(null); }}>
      <EvidencePicker companyId={company.id} value={docId} onChange={setDocId} />
    </NoteForm>
  ) : (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      <Button size="xs" variant="outline" onClick={() => setMode("verify")}>
        <ShieldCheck className="size-3.5" /> {t("customer.eori.verify")}
      </Button>
      <span className="text-xs text-ink-tertiary">{t("customer.eori.verify_hint")}</span>
    </div>
  );
}
