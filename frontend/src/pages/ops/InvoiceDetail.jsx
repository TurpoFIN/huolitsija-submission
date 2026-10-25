// Ops: one invoice. Same document view as the portal, plus why a draft was held, the tax-adviser review note,
// VAT reasons per line, draft editing (PATCH) and issuing, mark paid / credit / void / resend / PDF,
// payments + Stripe transactions, e-mails sent and the audit trail.
import React, { useMemo, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { AlertTriangle, Download, Eye, MoreHorizontal, Pencil } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { PageHeader, Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { Timeline } from "@/components/app/Timeline";
import { ErrorState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiPatch, fileUrl, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import InvoiceDocument, { InvoiceFacts, methodLabel, PaymentsList, RelatedList } from "@/features/billing/InvoiceDocument";
import InvoiceDispute from "@/features/billing/InvoiceDispute";
import { ActionNote, ActionsMenu, availability, useInvoiceActions } from "@/features/billing/InvoiceActions";
import LinesEditor, { linesProblem, newLine, parseLines, PreviewTotals, toApiLines, toEditorLines } from "@/features/billing/LinesEditor";
import { eur, hasPendingLegalBasis, invoiceTitle } from "@/features/billing/invoiceFormat";

const lowerFirst = (s) => (s ? s.charAt(0).toLowerCase() + s.slice(1) : s);
const TX_STATE = { initiated: "in-progress", processing: "in-progress", paid: "success", expired: "expired", failed: "failed", review: "in-review" };

function DraftEditor({ inv, onSaved, onCancel }) {
  const { t, locale } = useT();
  const vat = useApi("/invoices/vat-codes");
  const [lines, setLines] = useState(() => (inv.lines || []).length ? toEditorLines(inv.lines, locale) : [newLine()]);
  const [notes, setNotes] = useState(inv.notes || "");
  const [custRef, setCustRef] = useState(inv.customer_reference || "");
  const [terms, setTerms] = useState(inv.payment_terms_days != null ? String(inv.payment_terms_days) : "");
  const [internal, setInternal] = useState(inv.internal_note || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const parsed = useMemo(() => parseLines(lines), [lines]);
  const termsNum = terms === "" ? null : Number(terms);
  let problem = linesProblem(parsed, t);
  if (!problem && terms !== "" && (!Number.isInteger(termsNum) || termsNum < 0 || termsNum > 120)) problem = t("billing.new.bad_terms");
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const body = { lines: toApiLines(parsed), notes: notes.trim() || null, customer_reference: custRef.trim() || null, internal_note: internal.trim() || null };
      if (termsNum !== null) body.payment_terms_days = termsNum;
      const r = await apiPatch(`/invoices/${inv.id}`, body);
      onSaved(r);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Section title={t("billing.edit.title")}>
      <div className="flex flex-col gap-4">
        {vat.error && !vat.data && <ErrorState error={vat.error} onRetry={vat.reload} compact />}
        <LinesEditor lines={lines} onChange={setLines} vatCodes={vat.data?.items || []} disabled={busy} />
        <PreviewTotals lines={lines} vatCodes={vat.data?.items || []} />
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="ed-ref" className="text-xs text-ink-secondary">{t("billing.fact.customer_reference")}</Label>
            <Input id="ed-ref" value={custRef} onChange={(e) => setCustRef(e.target.value)} maxLength={120} disabled={busy} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="ed-terms" className="text-xs text-ink-secondary">{t("billing.new.terms")}</Label>
            <Input id="ed-terms" value={terms} inputMode="numeric" onChange={(e) => setTerms(e.target.value.replace(/[^0-9]/g, ""))} className="tabular" disabled={busy} />
          </div>
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="ed-notes" className="text-xs text-ink-secondary">{t("billing.new.notes")}</Label>
          <Textarea id="ed-notes" value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={1000} className="min-h-[56px]" disabled={busy} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="ed-internal" className="text-xs text-ink-secondary">{t("billing.edit.internal_note")}</Label>
          <Textarea id="ed-internal" value={internal} onChange={(e) => setInternal(e.target.value)} maxLength={1000} className="min-h-[56px]" disabled={busy} />
        </div>
        {error && <ErrorState error={error} />}
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={save} disabled={busy || !!problem}>{busy ? t("state.saving") : t("action.save")}</Button>
          <Button variant="ghost" onClick={onCancel} disabled={busy}>{t("action.cancel")}</Button>
          {problem && <span className="text-xs text-ink-tertiary">{problem}</span>}
        </div>
      </div>
    </Section>
  );
}

function auditDetail(a, t, locale) {
  const d = a.data || {};
  const parts = [];
  if (d.number) parts.push(d.number);
  if (d.total_cents != null) parts.push(eur(d.total_cents, locale));
  if (d.amount_cents != null) parts.push(eur(d.amount_cents, locale));
  if (d.method) parts.push(methodLabel(t, d.method));
  if (d.ref) parts.push(d.ref);
  if (d.credit_note) parts.push(d.credit_note);
  if (d.reason) parts.push(d.reason);
  if (Array.isArray(d.to) && d.to.length) parts.push(d.to.join(", "));
  if (Array.isArray(d.fields) && d.fields.length) {
    parts.push(d.fields.map((f) => { const k = `billing.audit.field.${f}`; const s = t(k); return s === k ? f : s; }).join(", "));
  }
  if (Array.isArray(d.warnings) && d.warnings.length) parts.push(d.warnings.map((w, i) => (d.warnings_i18n || [])[i]?.[locale] || w).join(" "));
  return parts.join(" · ");
}

function auditTitle(a, t) {
  const key = `billing.audit.${a.action}`;
  const s = t(key);
  return s === key ? t(`review.audit.${a.action}`) : s;
}

function Transactions({ items }) {
  const { t, locale } = useT();
  if (!items || items.length === 0) return <p className="text-sm text-ink-tertiary">{t("billing.tx.none")}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="border-b border-[color:var(--border-subtle)] text-xs text-ink-tertiary">
            <th className="py-1.5 pr-3 text-left font-normal">{t("billing.tx.created")}</th>
            <th className="py-1.5 px-3 text-left font-normal">{t("field.status")}</th>
            <th className="py-1.5 px-3 text-left font-normal">{t("billing.tx.session")}</th>
            <th className="py-1.5 pl-3 text-right font-normal">{t("billing.col.amount")}</th>
          </tr>
        </thead>
        <tbody>
          {items.map((x) => (
            <tr key={x.session_id} className="border-b border-[color:var(--border-subtle)] last:border-0">
              <td className="py-1.5 pr-3 text-sm tabular whitespace-nowrap">{dateTime(x.created_at, locale)}</td>
              <td className="py-1.5 px-3">
                <span className="inline-flex items-center gap-1.5">
                  <StatusBadge size="sm" state={TX_STATE[x.status] || "expired"} value={x.status} label={t(`billing.tx.${x.status}`)} />
                  <span className="text-xs text-ink-tertiary">{x.livemode ? t("billing.tx.live") : t("billing.tx.test")}</span>
                </span>
                {x.review_reason && <p className="text-xs text-ink-tertiary">{x.review_reason}</p>}
              </td>
              <td className="py-1.5 px-3 font-mono text-xs text-ink-secondary"><span className="block max-w-[14rem] truncate" title={x.session_id}>{x.session_id}</span></td>
              <td className="py-1.5 pl-3 text-right text-sm tabular whitespace-nowrap">{eur(x.amount_cents, locale)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Emails({ items }) {
  const { t, locale } = useT();
  if (!items || items.length === 0) return <p className="text-sm text-ink-tertiary">{t("billing.email.none")}</p>;
  return (
    <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)]">
      {[...items].reverse().map((m, i) => (
        <li key={i} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 py-1.5">
          <span className="text-sm">{t(`billing.email.${m.kind}`) === `billing.email.${m.kind}` ? m.kind : t(`billing.email.${m.kind}`)}</span>
          <span className="min-w-0 flex-1 truncate text-xs text-ink-secondary">{(m.to || []).join(", ") || t("billing.email.no_recipient")}</span>
          <span className="text-xs text-ink-tertiary">{(Array.isArray(m.status) ? m.status : [m.status]).filter(Boolean).map(s=>t(`review.email.${s}`)).join(", ")}</span>
          <span className="text-xs text-ink-tertiary tabular">{dateTime(m.at, locale)}</span>
        </li>
      ))}
    </ul>
  );
}

export default function InvoiceDetail() {
  const { id } = useParams();
  const { t, locale } = useT();
  const location = useLocation();
  const { can } = useAuth();
  const canWrite = can("billing.write");
  const q = useApi(`/invoices/${id}`);
  const [editing, setEditing] = useState(false);
  const [note, setNote] = useState(location.state?.note ? { text: location.state.note } : null);
  const acts = useInvoiceActions({
    onDone: (r) => {
      setNote({ text: r.text, link: r.link, tone: r.tone });
      q.reload();
    },
  });
  const inv = q.data;
  const av = inv ? availability(inv, t, canWrite) : {};
  const credit = inv?.kind === "credit_note";

  let primary = null;
  if (inv && !editing) {
    if (av.issue) {
      primary = (
        <Button onClick={() => acts.open("issue", inv)} disabled={!!av.issue.reason} title={av.issue.reason || undefined}>
          {t("billing.act.issue")}
        </Button>
      );
    } else if (av.markPaid) {
      primary = (
        <Button onClick={() => acts.open("markPaid", inv)} disabled={!!av.markPaid.reason} title={av.markPaid.reason || undefined}>
          {credit ? t("billing.act.mark_refunded") : t("billing.act.mark_paid")}
        </Button>
      );
    }
  }

  return (
    <div className="max-w-[1152px]">
      <PageHeader
        crumbs={[{ label: t("nav.billing"), to: "/ops/billing" }, { label: inv ? inv.number || t("billing.draft") : "…" }]}
        title={inv ? invoiceTitle(t, inv) : t("nav.billing")}
        meta={inv ? (
          <>
            <StatusBadge group="invoice" value={inv.status} />
            <Link to={`/ops/customers/${inv.company_id}`} className="hover:underline">{inv.company_name || "—"}</Link>
            {inv.shipment_id && <Link to={`/ops/shipments/${inv.shipment_id}`} className="text-ink-secondary hover:underline">{inv.shipment_reference}</Link>}
            {!credit && ["open", "overdue", "processing"].includes(inv.status) && (
              <span>{t("billing.sum.due")} <span className="tabular text-foreground">{eur(inv.amount_due_cents, locale)}</span></span>
            )}
            {inv.status === "draft" && <span>{t("field.total")} <span className="tabular text-foreground">{eur(inv.total_cents, locale)}</span></span>}
            <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
          </>
        ) : null}
        actions={inv ? (
          <>
            {inv.status === "draft" && !editing && (
              <Button variant="outline" onClick={() => setEditing(true)} disabled={!canWrite} title={!canWrite ? t("billing.act.no_permission") : undefined}>
                <Pencil /> {t("billing.edit.open")}
              </Button>
            )}
            {primary}
            <Button variant="outline" asChild>
              <a href={fileUrl(`/invoices/${inv.id}/pdf`)} target="_blank" rel="noopener noreferrer"><Download /> {t("billing.act.pdf")}</a>
            </Button>
            <ActionsMenu inv={inv} acts={acts} canWrite={canWrite} exclude={["pdf", primary ? (av.issue ? "issue" : "markPaid") : ""]}
              trigger={<Button variant="outline" size="icon" aria-label={t("billing.act.more")}><MoreHorizontal /></Button>} />
          </>
        ) : null}
      />
      {inv && !editing && (av.issue?.reason || av.markPaid?.reason) && (
        <p className="-mt-2 mb-3 text-xs text-ink-tertiary">
          {t("billing.act.unavailable", { action: av.issue ? t("billing.act.issue") : t("billing.act.mark_paid"), reason: lowerFirst(av.issue?.reason || av.markPaid?.reason) })}
        </p>
      )}
      <ActionNote note={note} onClose={() => setNote(null)} className="mb-4" />
      <Loadable q={q} skeleton={<LoadingRows rows={10} />}>
        {(d) => (
          <>
            {(d.warnings || []).length > 0 && (
              <div role="alert" className="status-pending mb-3 flex gap-2 rounded-lg px-3 py-2 text-sm">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
                <div>
                  <p className="font-medium">{d.status === "draft" ? t("billing.warn.held") : t("billing.warn.notes")}</p>
                  <ul className="mt-0.5 list-disc pl-4">
                    {d.warnings.map((w, i) => <li key={i}>{(d.warnings_i18n || [])[i]?.[locale] || w}</li>)}
                  </ul>
                </div>
              </div>
            )}
            {hasPendingLegalBasis(d) && (
              <div className="status-in-review mb-3 flex gap-2 rounded-lg px-3 py-2 text-sm">
                <Eye className="mt-0.5 size-4 shrink-0" aria-hidden />
                <span>{t("billing.legal.pending")}</span>
              </div>
            )}
            {d.status === "void" && d.void_reason && (
              <p className="mb-3 rounded-lg bg-[var(--bg-subtle)] px-3 py-2 text-sm text-ink-secondary">{t("billing.void.reason_shown", { reason: d.void_reason })}</p>
            )}
            <div className="grid gap-x-8 lg:grid-cols-[minmax(0,1fr)_320px]">
              <div className="min-w-0">
                {editing ? (
                  <DraftEditor inv={d} onCancel={() => setEditing(false)}
                    onSaved={(r) => {
                      setEditing(false); q.setData((old) => ({ ...old, ...r })); q.reload(); setNote({ text: t("billing.done.draft_updated") });
                      window.scrollTo({ top: 0, behavior: "smooth" });
                    }} />
                ) : (
                  <InvoiceDocument inv={d} mode="ops" />
                )}
                <InvoiceDispute key={d.id} inv={d} internal onChanged={q.reload} />
                {d.status !== "draft" && (
                  <Section title={t("billing.section.payments")}>
                    <PaymentsList payments={d.payments} mode="ops" />
                    <p className="mb-1 mt-4 text-xs text-ink-tertiary">{t("billing.tx.title")}</p>
                    <Transactions items={d.transactions} />
                  </Section>
                )}
                <Section title={t("billing.section.emails")}>
                  <Emails items={d.emails} />
                </Section>
                <Section title={t("billing.section.audit")}>
                  {(d.audit || []).length === 0 ? <p className="text-sm text-ink-tertiary">—</p> : (
                    <Timeline events={[...d.audit].reverse().map((a) => ({
                      id: a.id, at: a.at, title: auditTitle(a, t), detail: auditDetail(a, t, locale),
                      source: !a.actor || a.actor.type === "system" || !a.actor.id ? t("billing.audit.system") : a.actor.name,
                      tone: a.action === "invoice_overdue" ? "warning" : a.action === "invoice_voided" ? "danger" : undefined,
                    }))} />
                  )}
                </Section>
              </div>
              <aside className="min-w-0">
                <Section title={t("billing.section.details")}>
                  <InvoiceFacts inv={d} mode="ops" />
                </Section>
                {d.internal_note && (
                  <Section title={t("billing.edit.internal_note")}>
                    <p className="text-sm text-ink-secondary whitespace-pre-wrap">{d.internal_note}</p>
                  </Section>
                )}
                {(d.related || []).length > 0 && (
                  <Section title={credit ? t("billing.section.original") : t("billing.section.credit_notes")}>
                    <RelatedList related={d.related} mode="ops" />
                  </Section>
                )}
                {d.status !== "draft" && (
                  <Section title={t("billing.section.customer_view")}>
                    <p className="text-sm text-ink-secondary">{t("billing.customer_view.body")}</p>
                    <p className="mt-1 text-xs text-ink-tertiary">
                      {d.payment_options?.card_enabled ? t("billing.customer_view.card_on", { mode: d.payment_options.card_mode || "—" }) : t("billing.customer_view.card_off")}
                      {" · "}
                      {d.payment_options?.bank_transfer?.iban ? t("billing.customer_view.iban_on") : t("billing.customer_view.iban_off")}
                    </p>
                  </Section>
                )}
              </aside>
            </div>
          </>
        )}
      </Loadable>
      {acts.dialogs}
    </div>
  );
}
