// Shared invoice document view: lines (services, then pass-through), VAT breakdown with legal basis, totals,
// info lines, facts, bank transfer, related documents and payments. Used by the portal and ops detail pages.
import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Copy } from "lucide-react";
import { KV, Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { eur, isoDate, localeText, PENDING_REVIEW, vatLabel } from "./invoiceFormat";

export const invoiceLink = (mode, id) => (mode === "ops" ? `/ops/billing/${id}` : `/portal/invoices/${id}`);
export const shipmentLink = (mode, id) => (mode === "ops" ? `/ops/shipments/${id}` : `/portal/shipments/${id}`);

/** Small copy-to-clipboard control with an inline "Copied" confirmation. */
export function CopyButton({ value, label, className }) {
  const { t } = useT();
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (!done) return undefined;
    const h = setTimeout(() => setDone(false), 2000);
    return () => clearTimeout(h);
  }, [done]);
  if (!value) return null;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(String(value).replace(/\s+/g, ""));
      setDone(true);
    } catch {
      setDone(false);
    }
  };
  return (
    <button type="button" onClick={copy} aria-label={`${t("action.copy")}: ${label || value}`}
      className={cn("inline-flex h-6 items-center gap-1 rounded-full px-1.5 text-xs text-ink-tertiary hover:bg-hover hover:text-foreground", className)}>
      {done ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
      <span aria-live="polite">{done ? t("billing.copied") : t("action.copy")}</span>
    </button>
  );
}

export function unitLabel(t, unit) {
  if (!unit) return "";
  const key = `billing.unit.${unit}`;
  const s = t(key);
  return s === key ? unit : s;
}

function qtyText(l, t, locale) {
  const u = unitLabel(t, l.unit);
  return `${num(l.qty, locale, 2)}${u ? ` ${u}` : ""}`;
}

function rateText(line, locale) {
  if (line.is_pass_through || line.vat_code === "OUT_OF_SCOPE_DISBURSEMENT") return "—";
  if (line.vat_rate_pct === null || line.vat_rate_pct === undefined) return "—";
  return `${num(line.vat_rate_pct, locale, 1)} %`;
}

function LineRow({ l, mode, locale, t }) {
  return (
    <tr className="border-b border-[color:var(--border-subtle)] last:border-0 align-top">
      <td className="py-2 pr-3">
        <div className="text-sm">{l.description || "—"}</div>
        <div className="text-xs text-ink-tertiary tabular md:hidden">
          {qtyText(l, t, locale)} × {eur(l.unit_price_cents, locale)} · {t("billing.col.vat")} {rateText(l, locale)}
        </div>
        {mode === "ops" && (
          <div className="text-xs text-ink-tertiary">
            {[l.source ? t(`billing.source.${l.source}`) : null, l.is_pass_through ? t("review.invoice.pass_through") : t(`review.tax.${l.vat_code}`), l.vat_reason_i18n?.[locale] || l.vat_reason]
              .filter(Boolean).join(" · ")}
          </div>
        )}
      </td>
      <td className="hidden py-2 px-3 text-right text-sm tabular whitespace-nowrap md:table-cell">{qtyText(l, t, locale)}</td>
      <td className="hidden py-2 px-3 text-right text-sm tabular whitespace-nowrap md:table-cell">{eur(l.unit_price_cents, locale)}</td>
      <td className="hidden py-2 px-3 text-right text-sm tabular whitespace-nowrap text-ink-secondary md:table-cell">{rateText(l, locale)}</td>
      <td className="py-2 pl-3 text-right text-sm tabular whitespace-nowrap">{eur(l.amount_cents, locale)}</td>
    </tr>
  );
}

function SubtotalRow({ label, cents, locale }) {
  return (
    <tr>
      <td className="pt-2 pb-1 pr-3 text-sm text-ink-secondary" colSpan={1}>{label}</td>
      <td className="hidden md:table-cell" colSpan={3} />
      <td className="pt-2 pb-1 pl-3 text-right text-sm tabular whitespace-nowrap text-ink-secondary">{eur(cents, locale)}</td>
    </tr>
  );
}

/** Line items: services first, then pass-through disbursements with a one-line explanation. */
export function InvoiceLines({ inv, mode = "customer" }) {
  const { t, locale } = useT();
  const lines = inv.lines || [];
  const services = lines.filter((l) => !l.is_pass_through);
  const pass = lines.filter((l) => l.is_pass_through);
  if (lines.length === 0) {
    return <p className="text-sm text-ink-tertiary">{mode === "ops" && inv.status === "draft" ? t("billing.lines.none_draft") : t("billing.lines.none")}</p>;
  }
  const head = (
    <thead>
      <tr className="border-b border-[color:var(--border-subtle)] text-xs text-ink-tertiary">
        <th className="py-1.5 pr-3 text-left font-normal">{t("billing.col.description")}</th>
        <th className="hidden py-1.5 px-3 text-right font-normal md:table-cell">{t("billing.col.qty")}</th>
        <th className="hidden py-1.5 px-3 text-right font-normal md:table-cell">{t("billing.col.unit_price")}</th>
        <th className="hidden py-1.5 px-3 text-right font-normal md:table-cell">{t("billing.col.vat")}</th>
        <th className="py-1.5 pl-3 text-right font-normal">{t("billing.col.net")}</th>
      </tr>
    </thead>
  );
  return (
    <div className="overflow-x-auto">
      {services.length > 0 && (
        <table className="w-full">
          {head}
          <tbody>
            {services.map((l) => <LineRow key={l.id || l.description} l={l} mode={mode} locale={locale} t={t} />)}
            {pass.length > 0 && <SubtotalRow label={t("billing.total.services")} cents={inv.services_net_cents} locale={locale} />}
          </tbody>
        </table>
      )}
      {pass.length > 0 && (
        <div className={cn(services.length > 0 && "mt-4")}>
          <p className="text-sm font-medium">{t("billing.pass.title")}</p>
          <p className="mb-1 text-xs text-ink-tertiary">{t("billing.pass.explain")}</p>
          <table className="w-full">
            {services.length === 0 && head}
            <tbody>
              {pass.map((l) => <LineRow key={l.id || l.description} l={l} mode={mode} locale={locale} t={t} />)}
              <SubtotalRow label={t("billing.total.pass_through")} cents={inv.pass_through_cents} locale={locale} />
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** VAT breakdown per rate, with the legal basis under zero-rated / reverse-charge / disbursement rows. */
export function VatBreakdown({ inv, mode = "customer", rows }) {
  const { t, locale } = useT();
  const list = rows || inv?.vat_breakdown || [];
  if (list.length === 0) return <p className="text-sm text-ink-tertiary">—</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="border-b border-[color:var(--border-subtle)] text-xs text-ink-tertiary">
            <th className="py-1.5 pr-3 text-left font-normal">{t("billing.vat.class")}</th>
            <th className="py-1.5 px-3 text-right font-normal">{t("billing.vat.base")}</th>
            <th className="py-1.5 pl-3 text-right font-normal">{t("billing.vat.amount")}</th>
          </tr>
        </thead>
        <tbody>
          {list.map((b) => {
            const basis = localeText(b.legal_basis, locale);
            return (
              <tr key={`${b.vat_code}-${b.rate_pct}`} className="border-b border-[color:var(--border-subtle)] last:border-0 align-top">
                <td className="py-2 pr-3">
                  <div className="flex flex-wrap items-center gap-1.5 text-sm">
                    {vatLabel(b, locale)}
                    {mode === "ops" && b.legal_basis?.status === PENDING_REVIEW && (
                      <span className="status-pending inline-flex h-5 items-center rounded-full px-2 text-[11px] font-medium">{t("billing.legal.pending_short")}</span>
                    )}
                  </div>
                  {basis && <p className="mt-0.5 max-w-prose text-xs text-ink-tertiary">{basis}</p>}
                </td>
                <td className="py-2 px-3 text-right text-sm tabular whitespace-nowrap">{eur(b.base_cents, locale)}</td>
                <td className="py-2 pl-3 text-right text-sm tabular whitespace-nowrap">{b.kind === "disbursement" ? "—" : eur(b.vat_cents, locale)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TotalRow({ label, value, strong, tone }) {
  return (
    <div className={cn("flex items-baseline justify-between gap-6 py-1", strong && "border-t border-[color:var(--border-subtle)] pt-2 mt-1")}>
      <dt className={cn("text-sm", strong ? "font-medium" : "text-ink-secondary")}>{label}</dt>
      <dd className={cn("text-sm tabular whitespace-nowrap", strong && "font-medium", tone === "warning" && "text-warning-fg")}>{value}</dd>
    </div>
  );
}

/** Net, VAT, disbursements, total, then paid / credited / amount due. */
export function InvoiceTotals({ inv, className }) {
  const { t, locale } = useT();
  const credit = inv.kind === "credit_note";
  const showDue = !credit && inv.status !== "draft" && inv.status !== "void";
  return (
    <dl className={cn("ml-auto w-full max-w-sm", className)}>
      <TotalRow label={t("billing.total.services")} value={eur(inv.services_net_cents, locale)} />
      <TotalRow label={t("billing.total.vat")} value={eur(inv.vat_cents, locale)} />
      {(inv.pass_through_cents || 0) !== 0 && <TotalRow label={t("billing.total.pass_through")} value={eur(inv.pass_through_cents, locale)} />}
      <TotalRow strong label={credit ? t("billing.total.credit_total") : t("billing.total.total")} value={eur(inv.total_cents, locale)} />
      {(inv.paid_cents || 0) !== 0 && !credit && <TotalRow label={t("billing.total.paid")} value={eur(-inv.paid_cents, locale)} />}
      {(inv.credited_cents || 0) !== 0 && <TotalRow label={t("billing.total.credited")} value={eur(-inv.credited_cents, locale)} />}
      {showDue && <TotalRow strong label={t("billing.total.due")} value={eur(inv.amount_due_cents, locale)} />}
      {credit && (inv.refund_due_cents || 0) > 0 && (
        <TotalRow label={t("billing.total.refund_due")} value={eur(inv.refund_due_cents, locale)} />
      )}
    </dl>
  );
}

/** Customs info lines printed on the invoice. */
export function InfoLines({ inv }) {
  const { t } = useT();
  const items = inv.info || [];
  if (items.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1">
      {items.map((it, i) => (
        <li key={i} className="text-sm text-ink-secondary">
          {it.code === "IMPORT_VAT_POSTPONED" ? t("billing.info.import_vat_postponed", { mrn: it.mrn || "—" })
            : it.code === "CUSTOMS_PAID_BY_CUSTOMER" ? t("billing.info.customs_paid_by_customer", { mrn: it.mrn || "—" })
            : `${it.code}${it.mrn ? ` (MRN ${it.mrn})` : ""}`}
        </li>
      ))}
    </ul>
  );
}

/** The whole document body: lines, VAT, totals, info and notes. */
export default function InvoiceDocument({ inv, mode = "customer" }) {
  const { t } = useT();
  return (
    <>
      <Section title={t("billing.section.lines")}>
        <InvoiceLines inv={inv} mode={mode} />
      </Section>
      <Section title={t("billing.section.vat")}>
        <VatBreakdown inv={inv} mode={mode} />
      </Section>
      <Section title={t("billing.section.totals")}>
        <InvoiceTotals inv={inv} />
        {(inv.info || []).length > 0 && <div className="mt-3"><InfoLines inv={inv} /></div>}
        {inv.notes && (
          <p className="mt-3 text-sm text-ink-secondary whitespace-pre-wrap">
            <span className="text-ink-tertiary">{inv.kind === "credit_note" ? t("billing.credit_reason") : t("billing.notes")}: </span>{inv.notes}
          </p>
        )}
      </Section>
    </>
  );
}

function addressText(a) {
  if (!a) return null;
  if (typeof a === "string") return a;
  return [a.street, [a.postal_code, a.city].filter(Boolean).join(" "), a.country].filter(Boolean).join(", ") || null;
}

function Party({ p, buyer }) {
  const { t } = useT();
  if (!p) return null;
  return (
    <span className="flex flex-col">
      <span>{p.name || "—"}</span>
      <span className="text-xs text-ink-tertiary">
        {t("billing.fact.business_id")} {p.business_id || t("billing.fact.registration_pending")}
        {" · "}
        {t("billing.fact.vat_id")} {p.vat_id || (buyer ? "—" : t("billing.fact.registration_pending"))}
      </span>
      {addressText(p.address) && <span className="text-xs text-ink-tertiary">{addressText(p.address)}</span>}
    </span>
  );
}

/** Key facts of the document (seller, buyer, references, dates, terms). */
export function InvoiceFacts({ inv, mode = "customer", hideReference = false }) {
  const { t, locale } = useT();
  const credit = inv.kind === "credit_note";
  return (
    <div className="flex flex-col gap-3">
    <KV cols={1} items={[
      { label: t("billing.fact.seller"), value: <Party p={inv.seller} /> },
      { label: t("billing.fact.buyer"), value: <Party p={inv.buyer} buyer /> },
    ]} />
    <KV cols={2} className="grid-cols-2" items={[
      { label: t("billing.fact.customer_reference"), value: inv.customer_reference },
      { label: t("billing.fact.shipment"), value: inv.shipment_id
        ? <Link className="text-ink-accent hover:underline" to={shipmentLink(mode, inv.shipment_id)}>{inv.shipment_reference || inv.shipment_id}</Link> : null },
      credit && { label: t("billing.fact.credit_for"), value: inv.credit_note_for
        ? <Link className="font-mono text-[13px] text-ink-accent hover:underline" to={invoiceLink(mode, inv.credit_note_for)}>{inv.credit_note_for_number || inv.credit_note_for}</Link> : null },
      { label: t("billing.fact.issue_date"), value: inv.issue_date ? isoDate(inv.issue_date, locale) : (inv.status === "draft" ? t("billing.fact.on_issue") : null) },
      !credit && { label: t("billing.fact.due_date"), value: inv.due_date ? isoDate(inv.due_date, locale) : (inv.status === "draft" ? t("billing.fact.terms_from_issue", { n: inv.payment_terms_days ?? "—" }) : null) },
      { label: t("billing.fact.supply_date"), value: inv.supply_date ? isoDate(inv.supply_date, locale) : null },
      !hideReference && { label: t("billing.fact.reference"), value: inv.reference, mono: true },
      !credit && { label: t("billing.fact.payment_terms"), value: inv.payment_terms_days !== null && inv.payment_terms_days !== undefined ? t("billing.fact.terms_days", { n: inv.payment_terms_days }) : null },
      !credit && { label: t("billing.fact.late_interest"), value: inv.late_interest_pct !== null && inv.late_interest_pct !== undefined ? `${num(inv.late_interest_pct, locale, 1)} %` : null },
      { label: t("billing.fact.terms_version"), value: inv.terms_version },
    ]} />
    </div>
  );
}

function BtRow({ label, value, mono, copy }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 py-1.5">
      <dt className="text-xs text-ink-tertiary">{label}</dt>
      <dd className="flex min-w-0 items-center gap-1">
        {value === null || value === undefined || value === ""
          ? <span className="text-sm text-ink-tertiary">—</span>
          : <span className={cn("text-sm text-right break-all", mono && "font-mono text-[13px]")}>{value}</span>}
        {copy && value ? <CopyButton value={String(value)} label={label} /> : null}
      </dd>
    </div>
  );
}

/** Bank transfer instructions (Finnish viitenumero first). Says plainly when bank details are not set up yet. */
export function BankTransfer({ bt, inv }) {
  const { t, locale } = useT();
  if (!bt) return null;
  const contact = inv?.seller?.email;
  return (
    <div>
      {!bt.iban && (
        <p className="mb-2 rounded-lg bg-[var(--bg-subtle)] px-3 py-2 text-sm text-ink-secondary">
          {t("billing.bank.not_ready")}{contact ? ` ${t("billing.bank.contact", { email: contact })}` : ""}
        </p>
      )}
      <dl className="divide-y divide-[color:var(--border-subtle)]">
        <BtRow label={t("billing.bank.payee")} value={bt.payee} />
        {bt.iban && <BtRow label="IBAN" value={bt.iban} mono copy />}
        {bt.iban && <BtRow label="BIC" value={bt.bic} mono />}
        <BtRow label={t("billing.bank.reference")} value={bt.reference} mono copy />
        <BtRow label={t("billing.bank.rf_reference")} value={bt.rf_reference} mono copy />
        <BtRow label={t("billing.bank.amount")} value={eur(bt.amount_cents, locale)} />
        <BtRow label={t("billing.bank.due_date")} value={bt.due_date ? isoDate(bt.due_date, locale) : null} />
        {bt.virtual_barcode && <BtRow label={t("billing.bank.barcode")} value={bt.virtual_barcode} mono copy />}
      </dl>
    </div>
  );
}

/** Credit notes for this invoice, or the original invoice of a credit note. */
export function RelatedList({ related, mode = "customer" }) {
  const { t, locale } = useT();
  if (!related || related.length === 0) return null;
  return (
    <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)]">
      {related.map((r) => (
        <li key={r.id}>
          <Link to={invoiceLink(mode, r.id)} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-1 py-2 hover:bg-hover">
            <span className="text-xs text-ink-tertiary">{r.kind === "credit_note" ? t("billing.kind.credit_note") : t("billing.kind.invoice")}</span>
            <span className="font-mono text-[13px]">{r.number || t("billing.draft")}</span>
            <StatusBadge group="invoice" size="sm" value={r.status} />
            <span className="ml-auto text-sm tabular">{eur(r.total_cents, locale)}</span>
            <span className="text-xs text-ink-tertiary tabular">{isoDate(r.issue_date, locale)}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

export function methodLabel(t, m) {
  if (!m) return "—";
  const key = `billing.method.${m}`;
  const s = t(key);
  return s === key ? m : s;
}

/** Payments received (bank transfers and card payments). */
export function PaymentsList({ payments, mode = "customer" }) {
  const { t, locale } = useT();
  if (!payments || payments.length === 0) return <p className="text-sm text-ink-tertiary">{t("billing.payments.none")}</p>;
  return (
    <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)]">
      {payments.map((p) => (
        <li key={p.id || p.ref} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 py-2">
          <span className="text-sm tabular">{isoDate(String(p.paid_at || "").slice(0, 10), locale)}</span>
          <span className="text-sm text-ink-secondary">{methodLabel(t, p.method)}</span>
          <span className="min-w-0 truncate font-mono text-xs text-ink-tertiary">{p.ref}</span>
          {mode === "ops" && p.recorded_by?.name && <span className="text-xs text-ink-tertiary">{p.recorded_by.name}</span>}
          <span className="ml-auto text-sm tabular">{eur(p.amount_cents, locale)}</span>
        </li>
      ))}
    </ul>
  );
}
