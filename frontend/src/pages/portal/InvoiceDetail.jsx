// Portal: one invoice, Stripe hosted-invoice logic. First: amount due, due date, status and ONE primary action
// (card when enabled and allowed; otherwise bank transfer with viitenumero). Then lines, VAT, totals, facts.
// Return from Stripe (?paid=1&session_id=...) is confirmed by polling /payments/status/{session_id} (no webhook needed).
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { CheckCircle2, CircleDashed, CreditCard, Download, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader, Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState, Loadable, LoadingRows } from "@/components/app/states";
import { apiGet, apiPost, fileUrl, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import InvoiceDocument, { BankTransfer, InvoiceFacts, PaymentsList, RelatedList } from "@/features/billing/InvoiceDocument";
import InvoiceDispute from "@/features/billing/InvoiceDispute";
import { dueInfo, eur, invoiceTitle, isoDate, PAYABLE_STATES } from "@/features/billing/invoiceFormat";

const POLL_MS = 2000;
const POLL_LIMIT_MS = 60000;

/** Poll the Checkout Session until the invoice is paid (or 60 s pass). */
function usePaymentConfirmation(sessionId, onPaid) {
  const [state, setState] = useState({ phase: sessionId ? "confirming" : null, result: null, error: null });
  const [round, setRound] = useState(0);
  const paidRef = useRef(onPaid);
  paidRef.current = onPaid;
  useEffect(() => {
    if (!sessionId) return undefined;
    let stopped = false;
    let timer = null;
    const started = Date.now();
    setState({ phase: "confirming", result: null, error: null });
    const tick = async () => {
      let res = null;
      let err = null;
      try {
        res = await apiGet(`/payments/status/${encodeURIComponent(sessionId)}`);
      } catch (e) {
        err = e;
      }
      if (stopped) return;
      if (res?.invoice_status === "paid") {
        setState({ phase: "paid", result: res, error: null });
        paidRef.current?.();
        return;
      }
      if (res && ["failed", "expired"].includes(res.transaction_status)) {
        setState({ phase: "failed", result: res, error: null });
        paidRef.current?.();
        return;
      }
      if (err && err.status === 404) {
        setState({ phase: "failed", result: null, error: err });
        return;
      }
      if (Date.now() - started + POLL_MS > POLL_LIMIT_MS) {
        setState({ phase: "timeout", result: res, error: err });
        paidRef.current?.();
        return;
      }
      setState((s) => ({ ...s, result: res || s.result, error: err }));
      timer = setTimeout(tick, POLL_MS);
    };
    tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [sessionId, round]);
  const retry = useCallback(() => setRound((r) => r + 1), []);
  return { ...state, retry };
}

function ReturnNote({ tone, icon: Icon, children, onClose, action }) {
  const { t } = useT();
  return (
    <div role="status" aria-live="polite" className={cn("mb-4 flex flex-wrap items-center gap-2 rounded-lg px-3 py-2 text-sm",
      tone === "success" ? "status-success" : tone === "progress" ? "status-in-progress" : tone === "danger" ? "status-failed" : "bg-[var(--bg-subtle)] text-ink-secondary")}>
      {Icon && <Icon className={cn("size-4 shrink-0", tone === "progress" && "animate-spin motion-reduce:animate-none")} aria-hidden />}
      <span className="min-w-0 flex-1">{children}</span>
      {action}
      {onClose && (
        <button type="button" onClick={onClose} aria-label={t("action.close")} className="rounded-full p-0.5 hover:bg-hover">
          <X className="size-3.5" />
        </button>
      )}
    </div>
  );
}

function PaymentReturn({ confirm, inv, locale, t }) {
  if (!confirm.phase) return null;
  if (confirm.phase === "confirming") {
    return <ReturnNote tone="progress" icon={CircleDashed}>{t("billing.pay.confirming")}</ReturnNote>;
  }
  if (confirm.phase === "paid") {
    const when = inv?.paid_at ? isoDate(String(inv.paid_at).slice(0, 10), locale) : null;
    const amount = confirm.result?.amount_total != null ? eur(confirm.result.amount_total, locale) : null;
    return (
      <ReturnNote tone="success" icon={CheckCircle2}>
        {t("billing.pay.success", { amount: amount || "", date: when || "" }).replace(/\s+/g, " ").trim()}
      </ReturnNote>
    );
  }
  if (confirm.phase === "failed") {
    return <ReturnNote tone="danger">{t("billing.pay.failed")}</ReturnNote>;
  }
  return (
    <ReturnNote tone="neutral" icon={CircleDashed}
      action={<Button size="xs" variant="outline" onClick={confirm.retry}>{t("action.retry")}</Button>}>
      {t("billing.pay.processing")}
    </ReturnNote>
  );
}

function Summary({ inv, payState, onPay, canPay, cardEnabled, cardMode, payerAllowed, confirming }) {
  const { t, locale } = useT();
  const credit = inv.kind === "credit_note";
  const due = dueInfo(t, inv);
  const payable = !credit && PAYABLE_STATES.includes(inv.status) && inv.amount_due_cents > 0;
  let label = t("billing.sum.due");
  let amount = inv.amount_due_cents;
  let line = null;
  if (credit) {
    label = t("billing.sum.credit");
    amount = inv.total_cents;
    line = inv.credit_note_for_number ? t("billing.sum.credit_for", { n: inv.credit_note_for_number }) : null;
  } else if (inv.status === "paid") {
    label = t("billing.sum.paid");
    amount = inv.paid_cents || inv.total_cents;
    line = inv.paid_at ? t("billing.sum.paid_on", { date: isoDate(String(inv.paid_at).slice(0, 10), locale) }) : null;
  } else if (inv.status === "void") {
    label = t("billing.sum.void");
    amount = inv.total_cents;
    line = inv.void_reason || null;
  } else if (inv.status === "credited") {
    label = t("billing.sum.credited");
    amount = inv.total_cents;
    line = t("billing.sum.credited_line");
  } else if (inv.status === "processing") {
    line = t("billing.sum.processing");
  }
  const dueLine = !credit && inv.due_date && ["open", "overdue", "processing"].includes(inv.status)
    ? `${t("billing.fact.due_date")} ${isoDate(inv.due_date, locale)}${due ? ` · ${due.text}` : ""}` : null;

  let payReason = null;
  if (payable && !cardEnabled) payReason = t("billing.pay.card_off");
  else if (payable && !payerAllowed) payReason = t("billing.pay.no_permission");

  return (
    <section aria-label={t("billing.sum.aria")} className="mb-6 rounded-xl px-4 py-4 shadow-[inset_0_0_0_0.5px_var(--border-default)] sm:px-5">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <p className="text-xs text-ink-tertiary">{label}</p>
          <p className={cn("text-2xl tabular", inv.status === "void" && "text-ink-tertiary line-through")}>{eur(amount, locale)}</p>
          {dueLine && <p className={cn("text-sm", due?.tone === "warning" ? "text-warning-fg" : "text-ink-secondary")}>{dueLine}</p>}
          {line && <p className="text-sm text-ink-secondary">{line}</p>}
          {inv.status === "overdue" && inv.late_interest_pct != null && (
            <p className="text-xs text-ink-tertiary">{t("billing.sum.late_interest", { pct: String(inv.late_interest_pct).replace(".", locale === "fi" ? "," : ".") })}</p>
          )}
        </div>
        <div className="flex flex-col items-start gap-2 sm:items-end">
          <StatusBadge group="invoice" value={inv.status} />
          <div className="flex flex-wrap gap-2">
            {canPay && (
              <Button onClick={onPay} disabled={payState.busy}>
                <CreditCard /> {payState.busy ? t("billing.pay.redirecting") : t("billing.pay.card", { amount: eur(inv.amount_due_cents, locale) })}
              </Button>
            )}
            <Button variant="outline" asChild>
              <a href={fileUrl(`/invoices/${inv.id}/pdf`)} target="_blank" rel="noopener noreferrer"><Download /> {t("billing.act.pdf")}</a>
            </Button>
          </div>
          {canPay && cardMode === "test" && <span className="text-xs text-ink-tertiary">{t("billing.pay.test_mode")}</span>}
        </div>
      </div>
      {payable && !canPay && !confirming && (
        <p className="mt-3 text-sm text-ink-secondary">{payReason ? `${payReason} ` : ""}{t("billing.pay.use_bank", { ref: inv.reference || "—" })}</p>
      )}
      {payState.error && (
        <ErrorState className="mt-3" error={payState.error} />
      )}
      {payState.error && <p className="mt-1 text-xs text-ink-tertiary">{t("billing.pay.bank_still")}</p>}
    </section>
  );
}

function Body({ inv, confirm, locale, t, cancelled, onDismissCancel, onChanged }) {
  const { can } = useAuth();
  const [payState, setPayState] = useState({ busy: false, error: null });
  const opts = inv.payment_options || {};
  const credit = inv.kind === "credit_note";
  const payable = !credit && PAYABLE_STATES.includes(inv.status) && inv.amount_due_cents > 0;
  const payerAllowed = can("company.pay");
  const confirming = confirm.phase === "confirming";
  const canPay = payable && !!opts.card_enabled && payerAllowed && !confirming;

  const pay = async () => {
    setPayState({ busy: true, error: null });
    try {
      const r = await apiPost(`/invoices/${inv.id}/checkout`, {});
      if (!r?.url) throw new Error(t("billing.pay.no_url"));
      window.location.assign(r.url);
    } catch (e) {
      setPayState({ busy: false, error: e });
    }
  };

  const showBank = payable || inv.status === "processing";
  return (
    <>
      <PaymentReturn confirm={confirm} inv={inv} locale={locale} t={t} />
      {cancelled && !confirm.phase && (
        <ReturnNote tone="neutral" onClose={onDismissCancel}>{t("billing.pay.cancelled")}</ReturnNote>
      )}
      <Summary inv={inv} payState={payState} onPay={pay} canPay={canPay} cardEnabled={!!opts.card_enabled}
        cardMode={opts.card_mode} payerAllowed={payerAllowed} confirming={confirming} />
      <div className="grid gap-x-8 lg:grid-cols-[minmax(0,1fr)_340px] lg:grid-rows-[auto_1fr]">
        <aside className="min-w-0 lg:col-start-2 lg:row-start-1">
          {showBank && (
            <Section title={t("billing.bank.title")}>
              <BankTransfer bt={opts.bank_transfer} inv={inv} />
            </Section>
          )}
          {!showBank && (inv.payments || []).length > 0 && (
            <Section title={t("billing.section.payments")}>
              <PaymentsList payments={inv.payments} />
            </Section>
          )}
        </aside>
        <div className="min-w-0 lg:col-start-1 lg:row-span-2 lg:row-start-1">
          <InvoiceDocument inv={inv} mode="customer" />
          <InvoiceDispute key={inv.id} inv={inv} onChanged={onChanged} />
        </div>
        <aside className="min-w-0 self-start lg:col-start-2 lg:row-start-2">
          <Section title={t("billing.section.details")}>
            <InvoiceFacts inv={inv} mode="customer" hideReference={showBank} />
          </Section>
          {(inv.related || []).length > 0 && (
            <Section title={credit ? t("billing.section.original") : t("billing.section.credit_notes")}>
              <RelatedList related={inv.related} mode="customer" />
            </Section>
          )}
          {showBank && (inv.payments || []).length > 0 && (
            <Section title={t("billing.section.payments")}>
              <PaymentsList payments={inv.payments} />
            </Section>
          )}
        </aside>
      </div>
    </>
  );
}

export default function InvoiceDetail() {
  const { id } = useParams();
  const { t, locale } = useT();
  const [params, setParams] = useSearchParams();
  const q = useApi(`/invoices/${id}`);
  const sessionId = params.get("paid") === "1" ? params.get("session_id") : null;
  const cancelled = params.get("cancelled") === "1";
  const reload = q.reload;
  const confirm = usePaymentConfirmation(sessionId, reload);
  const dismissCancel = () => {
    const next = new URLSearchParams(params);
    next.delete("cancelled");
    setParams(next, { replace: true });
  };
  const inv = q.data;
  return (
    <div className="max-w-[1152px]">
      <PageHeader
        crumbs={[{ label: t("nav.invoices"), to: "/portal/invoices" }, { label: inv?.number || "…" }]}
        title={inv ? invoiceTitle(t, inv) : t("nav.invoices")}
        meta={inv ? (
          <>
            {inv.shipment_reference && <span>{t("billing.fact.shipment")} {inv.shipment_reference}</span>}
            {inv.issue_date && <span className="text-ink-tertiary">{t("billing.fact.issue_date")} {isoDate(inv.issue_date, locale)}</span>}
          </>
        ) : null}
      />
      <Loadable q={q} skeleton={<LoadingRows rows={8} />}>
        {(d) => <Body inv={d} confirm={confirm} locale={locale} t={t} cancelled={cancelled} onDismissCancel={dismissCancel} onChanged={q.reload} />}
      </Loadable>
    </div>
  );
}
