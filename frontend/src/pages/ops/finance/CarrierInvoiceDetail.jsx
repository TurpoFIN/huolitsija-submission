// Ops › Finance › Carrier invoice. Decision: "approve, dispute or reject this purchase invoice?" The header carries
// supplier, number, status, amount and due date with the actions; then the checks (each rule with its Finnish/English
// message) and the match against cost accruals per shipment (accrued vs invoiced vs variance); then the extracted fields,
// lines and the original file.
// APIs: GET /finance/carrier-invoices/{id}, POST /finance/carrier-invoices/{id}/{approve|dispute|reject|rematch},
// GET /finance/carrier-invoices/{id}/file.
import React, { useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { CheckCircle2, ExternalLink, RefreshCw, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { KV, PageHeader, Section } from "@/components/app/layout";
import { AgentMark } from "@/components/app/StatusBadge";
import { Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiPost, fileUrl, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { isoDate } from "@/features/billing/invoiceFormat";
import { ActionNote, ConfirmAction, Dot, FinStatus, MoneyCell } from "@/features/finance/kit";
import { FinTable } from "@/features/finance/Table";
import { amount, eur, pctText, signedEur } from "@/features/finance/format";

const CAN = {
  approve: ["needs_review", "matched", "disputed"],
  dispute: ["needs_review", "matched"],
  reject: ["received", "extracting", "needs_review", "matched", "disputed", "duplicate", "approved"],
  rematch: ["received", "needs_review", "matched", "disputed"],
};

function Checks({ audit }) {
  const { t, locale } = useT();
  const checks = audit?.checks || [];
  if (!checks.length) return <p className="py-1 text-sm text-ink-tertiary">{t("fin.ci.no_checks")}</p>;
  const sorted = [...checks].sort((a, b) => Number(a.ok) - Number(b.ok));
  return (
    <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)]">
      {sorted.map((c, i) => {
        const msg = (locale === "fi" ? c.message_fi : c.message_en) || c.message_fi || c.message_en || c.rule;
        return (
          <li key={`${c.rule}-${i}`} className="flex items-start gap-2 py-1.5 text-sm">
            {c.ok ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success-fg" aria-label={t("fin.ci.check_ok")} />
              : <XCircle className="mt-0.5 size-4 shrink-0 text-danger-fg" aria-label={t("fin.ci.check_failed")} />}
            <span className={cn("min-w-0 flex-1", c.ok && "text-ink-secondary")}>{msg}</span>
          </li>
        );
      })}
    </ul>
  );
}

export default function CarrierInvoiceDetail() {
  const { id } = useParams();
  const loc = useLocation();
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("billing.write");
  const q = useApi(`/finance/carrier-invoices/${id}`, { pollMs: 3000 });
  const [dialog, setDialog] = useState(null);
  const [note, setNote] = useState(loc.state?.note ? { text: loc.state.note } : null);
  const [busy, setBusy] = useState(null);

  const act = async (action, reason) => {
    setBusy(action);
    try {
      const body = reason ? { reason, note: reason } : {};
      const r = await apiPost(`/finance/carrier-invoices/${id}/${action}`, body);
      setNote({ text: t(`fin.ci.done.${action}`) });
      if (r && r.id === id) q.setData(r); else q.reload();
      return true;
    } finally {
      setBusy(null);
    }
  };
  const rematch = async () => {
    setNote(null);
    try {
      await act("rematch");
    } catch (e) {
      setNote({ tone: "error", text: e.message });
    }
  };

  return (
    <div className="max-w-[1152px]">
      <Loadable q={q} skeleton={<LoadingRows rows={10} />}>
        {(ci) => {
          const allowed = (a) => canWrite && CAN[a].includes(ci.status);
          const audit = ci.audit || {};
          const ex = ci.extraction || {};
          const sup = ci.supplier || {};
          const pay = ci.payment || {};
          const matches = ci.matches || [];
          const accrued = matches.reduce((a, m) => a + (m.accrued_eur_cents || 0), 0);
          const invoiced = matches.reduce((a, m) => a + (m.invoiced_eur_cents || 0), 0);
          return (
            <>
              <PageHeader
                crumbs={[{ label: t("fin.tab.carrier_invoices"), to: "/ops/finance/carrier-invoices" }, { label: ci.number || t("fin.ci.no_number") }]}
                title={`${sup.name || t("fin.ci.unknown_supplier")}${ci.number ? ` · ${ci.number}` : ""}`}
                meta={<>
                  <FinStatus group="carrier_invoice" value={ci.status} size="default" />
                  <span className="tabular font-medium text-foreground">{amount(ci.total_cents, ci.currency, locale)}</span>
                  {ci.currency && ci.currency !== "EUR" && <span className="tabular">{eur(ci.eur?.total_cents, locale)}</span>}
                  <Dot />
                  <span>{t("fin.ci.due_on", { date: isoDate(ci.due_date, locale) })}</span>
                  <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
                </>}
                actions={<>
                  {ci.file_id !== null && ci.file_id !== undefined && (
                    <Button variant="outline" size="sm" asChild>
                      <a href={fileUrl(`/finance/carrier-invoices/${id}/file`)} target="_blank" rel="noreferrer"><ExternalLink /> {t("fin.ci.open_file")}</a>
                    </Button>
                  )}
                  {CAN.rematch.includes(ci.status) && (
                    <Button variant="outline" size="sm" disabled={!canWrite || !!busy} onClick={rematch}>
                      <RefreshCw className={cn(busy === "rematch" && "animate-spin")} /> {t("fin.ci.act.rematch")}
                    </Button>
                  )}
                  {CAN.reject.includes(ci.status) && <Button variant="destructive" size="sm" disabled={!allowed("reject") || !!busy} onClick={() => setDialog("reject")}>{t("fin.ci.act.reject")}</Button>}
                  {CAN.dispute.includes(ci.status) && <Button variant="outline" size="sm" disabled={!allowed("dispute") || !!busy} onClick={() => setDialog("dispute")}>{t("fin.ci.act.dispute")}</Button>}
                  {CAN.approve.includes(ci.status) && <Button size="sm" disabled={!allowed("approve") || !!busy} onClick={() => setDialog("approve")}>{t("fin.ci.act.approve")}</Button>}
                </>}
              />
              <ActionNote note={note} onClose={() => setNote(null)} className="mb-4" />
              {!canWrite && <p className="mb-3 text-xs text-ink-tertiary">{t("fin.no_permission")}</p>}

              <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
                <div className="min-w-0">
                  <Section title={t("fin.ci.match")}>
                    {matches.length === 0 ? <p className="py-1 text-sm text-ink-tertiary">{t("fin.ci.no_match")}</p> : (
                      <>
                        <FinTable rows={matches} rowKey={(m) => m.accrual_id || m.shipment_ref}
                          totals={matches.length > 1 ? { accrued, invoiced, variance: audit.variance_eur_cents ?? invoiced - accrued } : null} totalsLabel={t("fin.total")}
                          columns={[
                            { key: "shipment_ref", header: t("fin.col.shipment"), cell: (m) => m.shipment_id ? <Link className="font-medium hover:underline" to={`/ops/shipments/${m.shipment_id}`}>{m.shipment_ref || "—"}</Link> : (m.shipment_ref || "—") },
                            { key: "accrued_eur_cents", header: t("fin.col.accrued"), align: "right", cell: (m) => eur(m.accrued_eur_cents, locale), total: (x) => eur(x.accrued, locale) },
                            { key: "invoiced_eur_cents", header: t("fin.col.invoiced"), align: "right", cell: (m) => eur(m.invoiced_eur_cents, locale), total: (x) => eur(x.invoiced, locale) },
                            { key: "variance_eur_cents", header: t("fin.col.variance"), align: "right",
                              cell: (m) => <span className={cn(m.variance_eur_cents > 0 && "text-danger-fg")}>{signedEur(m.variance_eur_cents, locale)}</span>,
                              total: (x) => <span className={cn(x.variance > 0 && "text-danger-fg")}>{signedEur(x.variance, locale)}</span> },
                          ]} />
                        <p className="mt-2 text-sm">
                          {audit.within_tolerance === true && <span className="text-ink-secondary">{t("fin.ci.within_tolerance", { pct: pctText(audit.variance_pct, locale) })}</span>}
                          {audit.within_tolerance === false && <span className="text-danger-fg">{t("fin.ci.over_tolerance", { amount: signedEur(audit.variance_eur_cents, locale), pct: pctText(audit.variance_pct, locale) })}</span>}
                        </p>
                      </>
                    )}
                  </Section>
                  <Section title={t("fin.ci.checks")}>
                    <Checks audit={audit} />
                    {ci.decision?.outcome && (
                      <div className="mt-3 rounded-lg bg-[var(--bg-subtle)] px-3 py-2 text-sm">
                        <p className="flex items-center gap-2"><AgentMark /> <span className="font-medium">{tEnum(t, "fin.decision", ci.decision.outcome)}</span>
                          {ci.decision.at && <span className="text-xs text-ink-tertiary">{dateTime(ci.decision.at, locale)}</span>}</p>
                        {(ci.decision.reasons || []).length > 0 && (
                          <ul className="mt-1 list-disc pl-5 text-ink-secondary">
                            {ci.decision.reasons.map((r, i) => <li key={i}>{typeof r === "string" ? r : (locale === "fi" ? r.fi : r.en) || r.fi || r.en || r.code}</li>)}
                          </ul>
                        )}
                      </div>
                    )}
                  </Section>
                  {ci.dispute && (
                    <Section title={t("fin.ci.dispute")}>
                      <KV items={[
                        { label: t("fin.ci.dispute_reason"), value: ci.dispute.reason ? tEnum(t, "fin.dispute_reason", ci.dispute.reason) : null, hint: ci.dispute.note || null },
                        { label: t("fin.ci.dispute_amount"), value: ci.dispute.claimed_eur_cents !== undefined ? eur(ci.dispute.claimed_eur_cents, locale) : null, className: "tabular" },
                        { label: t("fin.ci.dispute_opened"), value: ci.dispute.opened_at ? dateTime(ci.dispute.opened_at, locale) : null, hint: ci.dispute.by || null },
                        { label: t("fin.ci.dispute_sent"), value: ci.dispute.sent_at ? dateTime(ci.dispute.sent_at, locale) : (ci.dispute.email_status ? tEnum(t, "fin.dispute_email", ci.dispute.email_status) : t("fin.ci.not_sent")), hint: ci.dispute.to || null },
                      ]} />
                      {ci.dispute.approval_id && <p className="mt-2 text-sm"><Link className="text-ink-accent hover:underline" to={`/ops/approvals?focus=${ci.dispute.approval_id}`}>{t("fin.col.open_approval")}</Link></p>}
                    </Section>
                  )}
                  <Section title={t("fin.ci.lines")}>
                    {(ci.lines || []).length === 0 ? <p className="py-1 text-sm text-ink-tertiary">{t("fin.ci.no_lines")}</p> : (
                      <FinTable rows={ci.lines.map((l, i) => ({ ...l, _i: i }))} rowKey="_i" columns={[
                        { key: "description", header: t("fin.col.description"), sortable: false, cell: (l) => <span className="block min-w-[12rem]">{l.description || "—"}</span> },
                        { key: "shipment_ref", header: t("fin.col.shipment"), sortable: false, hideBelow: "md", cell: (l) => <span className="whitespace-nowrap">{l.shipment_ref || l.container_no || l.bl_no || "—"}</span> },
                        { key: "vat_rate_pct", header: t("fin.col.vat_pct"), align: "right", sortable: false, hideBelow: "md", cell: (l) => (l.vat_rate_pct === null || l.vat_rate_pct === undefined ? "—" : pctText(l.vat_rate_pct, locale, 1)) },
                        { key: "amount_cents", header: t("fin.col.amount"), align: "right", sortable: false, cell: (l) => amount(l.amount_cents, ci.currency, locale) },
                      ]} />
                    )}
                    <div className="mt-3 flex flex-col items-end gap-0.5 text-sm tabular">
                      <span className="text-ink-secondary">{t("fin.ci.net")} {amount(ci.net_cents, ci.currency, locale)}</span>
                      <span className="text-ink-secondary">{t("fin.ci.vat")} {amount(ci.vat_cents, ci.currency, locale)}</span>
                      <span className="font-medium">{t("fin.col.total")} {amount(ci.total_cents, ci.currency, locale)}</span>
                    </div>
                  </Section>
                </div>
                <div className="min-w-0">
                  <Section title={t("fin.ci.supplier")}>
                    <KV cols={1} items={[
                      { label: t("fin.col.supplier"), value: sup.carrier_id ? <Link className="hover:underline" to={`/ops/carriers/${sup.carrier_id}`}>{sup.name}</Link> : sup.name },
                      { label: t("fin.ci.business_id"), value: sup.business_id, mono: true },
                      { label: t("fin.ci.vat_id"), value: sup.vat_id, mono: true },
                      { label: t("fin.ci.country"), value: sup.country },
                    ]} />
                  </Section>
                  <Section title={t("fin.ci.payment")}>
                    <KV cols={1} items={[
                      { label: "IBAN", value: pay.iban || sup.iban, mono: true },
                      { label: "BIC", value: pay.bic || sup.bic, mono: true },
                      { label: t("fin.ci.reference"), value: pay.reference, mono: true },
                      pay.message ? { label: t("fin.ci.message"), value: pay.message } : null,
                      { label: t("fin.col.invoice_date"), value: isoDate(ci.invoice_date, locale) },
                      { label: t("fin.col.due"), value: isoDate(ci.due_date, locale) },
                      { label: t("fin.ci.vat_treatment"), value: ci.vat_treatment ? tEnum(t, "fin.vat_treatment", ci.vat_treatment) : null },
                      ci.currency && ci.currency !== "EUR" ? { label: t("fin.ci.eur_total"), value: <MoneyCell cents={ci.eur?.total_cents} currency="EUR" className="items-start" />,
                        hint: ci.fx?.rate ? t("fin.fx.rate_hint", { rate: ci.fx.rate, cur: ci.currency, date: isoDate(ci.fx.rate_date, locale) }) : null } : null,
                      ci.payable_id ? { label: t("fin.ci.payable"), value: <Link className="text-ink-accent hover:underline" to={`/ops/finance/payables?focus=${ci.payable_id}`}>{t("fin.ci.open_payable")}</Link> } : null,
                    ]} />
                  </Section>
                  <Section title={t("fin.ci.extraction")}>
                    <KV cols={1} items={[
                      { label: t("fin.col.source"), value: tEnum(t, "fin.ci_source", ci.source), hint: ci.filename || null },
                      { label: t("field.status"), value: ex.status ? <span className="inline-flex items-center gap-1.5"><FinStatus group="extraction" value={ex.status} />{ex.model && <AgentMark title={ex.model} />}</span> : null },
                      { label: t("fin.ci.confidence"), value: ex.confidence === null || ex.confidence === undefined ? null : pctText(ex.confidence <= 1 ? ex.confidence * 100 : ex.confidence, locale, 0) },
                      { label: t("fin.ci.received"), value: ci.created_at ? dateTime(ci.created_at, locale) : null },
                    ]} />
                    {(ex.warnings || []).length > 0 && (
                      <ul className="mt-3 flex flex-col gap-1">
                        {ex.warnings.map((w, i) => <li key={i} className="rounded-lg px-3 py-1.5 text-xs status-pending">{typeof w === "string" ? w : (locale === "fi" ? w.fi || w.message_fi : w.en || w.message_en) || JSON.stringify(w)}</li>)}
                      </ul>
                    )}
                  </Section>
                  {ci.references && (
                    <Section title={t("fin.ci.references")}>
                      <KV cols={1} items={[
                        { label: t("fin.col.shipments"), value: (ci.references.shipment_refs || []).join(", ") },
                        { label: t("fin.ci.containers"), value: (ci.references.container_nos || []).join(", "), mono: true },
                        { label: t("fin.ci.bls"), value: (ci.references.bl_nos || []).join(", "), mono: true },
                        { label: t("fin.ci.bookings"), value: (ci.references.booking_nos || []).join(", "), mono: true },
                      ]} />
                    </Section>
                  )}
                </div>
              </div>

              <ConfirmAction open={dialog === "approve"} onOpenChange={(o) => !o && setDialog(null)} title={t("fin.ci.confirm.approve")}
                description={t("fin.ci.confirm.approve_body", { amount: amount(ci.total_cents, ci.currency, locale), date: isoDate(ci.due_date, locale) })}
                submitLabel={t("fin.ci.act.approve")} reason={audit.within_tolerance === false} reasonRequired={audit.within_tolerance === false}
                onSubmit={(r) => act("approve", r)} />
              <ConfirmAction open={dialog === "dispute"} onOpenChange={(o) => !o && setDialog(null)} title={t("fin.ci.confirm.dispute")}
                description={t("fin.ci.confirm.dispute_body")} submitLabel={t("fin.ci.act.dispute")} reason reasonRequired
                onSubmit={(r) => act("dispute", r)} />
              <ConfirmAction open={dialog === "reject"} onOpenChange={(o) => !o && setDialog(null)} title={t("fin.ci.confirm.reject")}
                description={t("fin.ci.confirm.reject_body")} submitLabel={t("fin.ci.act.reject")} reason reasonRequired destructive
                onSubmit={(r) => act("reject", r)} />
            </>
          );
        }}
      </Loadable>
    </div>
  );
}
