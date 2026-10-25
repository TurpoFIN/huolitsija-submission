// One claim: status, facts, cap calculation, decision, attachments (add until decided), history.
// mode "customer" (portal) or "ops" (adds the review/decision actions).
import React, { useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Paperclip, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { KV } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { FileUpload } from "@/components/app/FileUpload";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { Timeline } from "@/components/app/Timeline";
import { apiPatch, apiPost, apiUpload, fileUrl, useApi } from "@/lib/api";
import { date, dateTime, kg, money, num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { SavedNote, useFlag } from "@/features/exceptions/common";
import CapCalculation from "./CapCalculation";
import ClaimOpsActions from "./ClaimOpsActions";
import RecoursePanel from "./RecoursePanel";
import { capFromClaim, historyEvents, isDecided, typeLabel } from "./text";

function Block({ title, right, children, className }) {
  return (
    <section className={cn("border-t border-[color:var(--border-subtle)] px-4 py-3", className)}>
      {(title || right) && (
        <div className="mb-2 flex items-center justify-between gap-2">
          {title && <h3 className="text-sm font-medium">{title}</h3>}
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

function Documents({ claim, onChanged }) {
  const { t } = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [saved, flagSaved] = useFlag();
  const decided = isDecided(claim.status);
  const add = async (files) => {
    setBusy(true);
    setError(null);
    try {
      const ids = [...(claim.document_ids || [])];
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        fd.append("type", "other");
        if (claim.shipment_id) fd.append("shipment_id", claim.shipment_id);
        const d = await apiUpload("/documents", fd);
        ids.push(d.id);
      }
      await apiPatch(`/claims/${claim.id}`, { document_ids: ids });
      flagSaved();
      onChanged();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Block title={t("claims.d.documents")} right={<SavedNote show={saved} label={t("claims.d.docs_saved")} />}>
      {claim.documents?.length ? (
        <ul className="mb-2 flex flex-col gap-1">
          {claim.documents.map((d) => (
            <li key={d.id} className="flex items-center gap-2 text-sm">
              <Paperclip className="size-3.5 shrink-0 text-ink-tertiary" aria-hidden />
              <a href={fileUrl(`/documents/${d.id}/file`)} target="_blank" rel="noreferrer" className="min-w-0 truncate hover:underline">{d.filename || d.id}</a>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mb-2 text-sm text-ink-tertiary">{t("claims.d.no_documents")}</p>
      )}
      {decided ? (
        <p className="text-xs text-ink-tertiary">{t("claims.d.docs_locked")}</p>
      ) : (
        <FileUpload multiple onFiles={add} busy={busy} label={t("claims.d.add_documents")} hint={t("claims.form.documents_hint")} />
      )}
      {error && <ErrorState className="mt-2" compact error={error} />}
    </Block>
  );
}

function stateLine(c, t, locale, ops) {
  if (ops) {
    switch (c.status) {
      case "draft": return t("claims.d.ops_state.draft");
      case "submitted": return t("claims.d.ops_state.submitted", { date: date(c.submitted_at || c.notified_at, locale) });
      case "in_review": return t("claims.d.ops_state.in_review", { name: c.reviewer?.name || "—" });
      case "approved": return t("claims.d.ops_state.approved", { amount: money(c.decision?.amount_cents, locale, { decimals: 2 }) });
      default: break;
    }
  }
  switch (c.status) {
    case "draft": return t("claims.d.draft_note");
    case "submitted": return t("claims.d.state.submitted", { date: date(c.submitted_at || c.notified_at, locale) });
    case "in_review": return t("claims.d.state.in_review");
    case "approved": return t("claims.d.state.approved", { amount: money(c.decision?.amount_cents, locale, { decimals: 2 }) });
    case "rejected": return t("claims.d.state.rejected");
    case "paid": return t("claims.d.state.paid", { date: date(c.payment?.paid_at, locale) });
    default: return null;
  }
}

export default function ClaimDetail({ id, mode = "customer", onChanged, onClose, onEdit, savedLabel }) {
  const { t, locale } = useT();
  const q = useApi(id ? `/claims/${id}` : null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  const [localSaved, setLocalSaved] = useState(null);
  const ops = mode === "ops";
  const refresh = () => { q.reload(); onChanged?.(); };

  if (q.loading && !q.data) return <div className="p-4"><LoadingRows rows={8} /></div>;
  if (q.error && !q.data) {
    return (
      <div className="flex flex-col gap-3 p-4">
        <ErrorState error={q.error.status === 404 ? { message: t("claims.d.not_found") } : q.error} onRetry={q.error.status === 404 ? null : q.reload} />
        {onClose && <Button variant="ghost" size="sm" onClick={onClose}>{t("claims.d.close")}</Button>}
      </div>
    );
  }
  const c = q.data;
  if (!c) return null;
  const decided = isDecided(c.status);
  const submit = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await apiPost(`/claims/${c.id}/submit`, {});
      toast.success(t("claims.form.submitted"));
      setLocalSaved(t("claims.form.submitted"));
      refresh();
    } catch (e) {
      setSubmitError(e);
    } finally {
      setSubmitting(false);
    }
  };
  const shipmentTo = ops ? `/ops/shipments/${c.shipment_id}` : `/portal/shipments/${c.shipment_id}`;

  return (
    <div className="flex flex-col">
      {q.error && <ErrorState className="m-3" compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
      <header className="flex flex-col gap-2 px-4 pb-3 pt-4">
        <div className="flex items-start justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <StatusBadge size="sm" group="claim" value={c.status} />
            <span className="text-xs text-ink-secondary">{typeLabel(t, c.type)}</span>
            {c.notice_late && <StatusBadge size="sm" state="pending" label={t("claims.late_notice")} />}
          </div>
          {onClose && <Button variant="ghost" size="icon-xs" onClick={onClose} aria-label={t("claims.d.close")}><X /></Button>}
        </div>
        <h2 className="text-md font-medium tabular">{c.number}</h2>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
          <Link to={shipmentTo} className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-medium shadow-[inset_0_0_0_0.5px_var(--border-default)] hover:bg-hover">
            {c.shipment_reference} <ArrowRight className="size-3 text-ink-tertiary" />
          </Link>
          {ops && (c.company_id
            ? <Link to={`/ops/customers/${c.company_id}`} className="text-ink-secondary hover:text-foreground hover:underline">{c.company_name}</Link>
            : <span className="text-ink-secondary">{c.company_name}</span>)}
        </div>
        <p className="text-sm text-ink-secondary">{stateLine(c, t, locale, ops)} <SavedNote show={!!(localSaved || savedLabel)} label={localSaved || savedLabel} /></p>
        {!ops && c.status === "draft" && (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={submit} disabled={submitting}>{submitting ? t("state.saving") : t("claims.d.submit")}</Button>
            {onEdit && <Button size="sm" variant="outline" onClick={() => onEdit(c)} disabled={submitting}>{t("claims.d.edit")}</Button>}
          </div>
        )}
        {submitError && <ErrorState compact error={submitError} />}
      </header>

      {ops && (["submitted", "in_review", "approved"].includes(c.status)) && (
        <Block title={c.status === "approved" ? t("claims.ops.mark_paid") : t("claims.ops.propose")}>
          <ClaimOpsActions claim={c} onDone={(msg) => { if (msg) setLocalSaved(msg); refresh(); }} />
        </Block>
      )}

      {(c.decision || c.payment) && (
        <Block title={t("claims.d.decision")}>
          <KV cols={2} items={[
            c.decision && { label: t("claims.d.decided"), value: <span className="font-medium tabular">{money(c.decision.amount_cents, locale, { decimals: 2 })}</span> },
            c.decision && { label: t("claims.d.decided_by"), value: t("claims.d.decision_by", { name: c.decision.decided_by?.name || "—", time: dateTime(c.decision.decided_at, locale) }) },
            c.payment && { label: t("claims.d.paid_at"), value: date(c.payment.paid_at, locale) },
            c.payment && { label: t("claims.d.reference"), value: c.payment.reference },
          ]} />
          {c.decision?.note && (
            <div className="mt-3">
              <p className="text-xs text-ink-tertiary">{t("claims.ops.note")}</p>
              <p className="whitespace-pre-wrap text-sm">{c.decision.note}</p>
            </div>
          )}
          {ops && c.decision?.proposal_note && c.decision.proposal_note !== c.decision.note && (
            <div className="mt-3">
              <p className="text-xs text-ink-tertiary">{t("claims.ops.propose")}</p>
              <p className="whitespace-pre-wrap text-sm text-ink-secondary">{c.decision.proposal_note}</p>
            </div>
          )}
        </Block>
      )}

      {ops && c.status === "paid" && (
        <Block title={t("recourse.title")}>
          <RecoursePanel claim={c} onDone={(msg) => { if (msg) setLocalSaved(msg); refresh(); }} />
        </Block>
      )}

      <Block>
        <KV cols={2} items={[
          { label: t("claims.d.claimed"), value: <span className="font-medium tabular">{money(c.amount_claimed_cents, locale, { decimals: 2 })}</span> },
          { label: t("claims.d.kg"), value: c.gross_kg_affected != null ? kg(c.gross_kg_affected, locale) : null },
          { label: t("claims.d.packages"), value: c.packages_affected != null ? num(c.packages_affected, locale) : null },
          { label: t("claims.d.date_of_loss"), value: c.date_of_loss ? date(c.date_of_loss, locale) : null },
          { label: t("claims.d.notified"), value: (c.submitted_at || c.notified_at) ? date(c.submitted_at || c.notified_at, locale) : null },
          ...(c.notice_given_on ? [{ label: t("claims.d.notice_on"), value: date(c.notice_given_on, locale) }] : []),
          { label: t("claims.d.created"), value: `${date(c.created_at, locale)}${c.created_by?.name ? ` · ${c.created_by.name}` : ""}` },
        ]} />
        {c.description && (
          <div className="mt-3">
            <p className="text-xs text-ink-tertiary">{t("claims.d.description")}</p>
            <p className="whitespace-pre-wrap text-sm">{c.description}</p>
          </div>
        )}
      </Block>

      <Block>
        <CapCalculation est={capFromClaim(c)} claimedCents={c.amount_claimed_cents} decided={decided} />
      </Block>

      <Documents claim={c} onChanged={refresh} />

      <Block title={t("claims.d.history")}>
        <Timeline events={historyEvents(c.history, t)} />
      </Block>
    </div>
  );
}
