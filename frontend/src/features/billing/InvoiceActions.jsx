// Ops invoice actions shared by the billing list (row menu) and the ops invoice page:
// issue, mark paid, credit (full), void, resend e-mail, PDF. Each dialog keeps input on failure and shows the
// server's sentence inline; success is reported back to the page for an inline confirmation.
import React, { useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, MoreHorizontal, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DateInput } from "@/components/ui/DateInput";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { ErrorState } from "@/components/app/states";
import { apiPost, fileUrl } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { centsToInput, eur, parseEuro, settleAmount, todayIso } from "./invoiceFormat";

/** Which actions apply to an invoice, and why a visible one is disabled. null = not shown. */
export function availability(inv, t, canWrite = true) {
  if (!inv) return {};
  const s = inv.status;
  const credit = inv.kind === "credit_note";
  const noWrite = canWrite ? null : t("billing.act.no_permission");
  const lines = inv.lines;
  const out = {};
  if (s === "draft") {
    let reason = noWrite;
    if (!reason && Array.isArray(lines) && lines.length === 0) reason = t("billing.act.issue_no_lines");
    if (!reason && !credit && (inv.total_cents || 0) <= 0) reason = t("billing.act.issue_zero");
    out.issue = { reason };
  }
  if (credit ? s === "open" && settleAmount(inv) > 0 : ["open", "overdue", "processing"].includes(s)) {
    out.markPaid = { reason: noWrite };
  }
  if (!credit && ["open", "overdue", "paid", "processing"].includes(s)) {
    const left = (inv.total_cents || 0) - (inv.credited_cents || 0);
    out.credit = { reason: noWrite || (left <= 0 ? t("billing.act.credit_none_left") : null) };
  }
  if (["draft", "open", "overdue"].includes(s)) {
    out.void = { reason: noWrite || ((inv.paid_cents || 0) > 0 ? t("billing.act.void_has_payments") : null) };
  } else if (["paid", "processing"].includes(s)) {
    out.void = { reason: t("billing.act.void_paid") };
  }
  if (s !== "draft") out.resend = { reason: noWrite };
  out.pdf = { reason: null };
  return out;
}

/** Inline confirmation next to the actions (not only a toast). */
export function ActionNote({ note, onClose, className }) {
  const { t } = useT();
  if (!note) return null;
  const failed = note.tone === "danger";
  return (
    <div role="status" className={cn("flex flex-wrap items-center gap-2 rounded-lg px-3 py-1.5 text-sm", failed ? "status-failed" : "status-success", className)}>
      {!failed && <CheckCircle2 className="size-4 shrink-0" aria-hidden />}
      <span className="min-w-0">{note.text}</span>
      {note.link && <Link to={note.link.to} className="font-medium underline-offset-2 hover:underline">{note.link.label}</Link>}
      {onClose && (
        <button type="button" onClick={onClose} aria-label={t("action.close")} className="ml-auto rounded-full p-0.5 hover:bg-hover">
          <X className="size-3.5" />
        </button>
      )}
    </div>
  );
}

function Field({ label, htmlFor, hint, children }) {
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={htmlFor} className="text-xs text-ink-secondary">{label}</Label>
      {children}
      {hint && <p className="text-xs text-ink-tertiary">{hint}</p>}
    </div>
  );
}

function ActionDialog({ open, onOpenChange, title, description, children, submitLabel, busyLabel, onSubmit, busy, error, disabledReason, destructive }) {
  const { t } = useT();
  return (
    <Dialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent className="max-w-md">
        <form onSubmit={(e) => { e.preventDefault(); if (!disabledReason && !busy) onSubmit(); }} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            {description && <DialogDescription>{description}</DialogDescription>}
          </DialogHeader>
          {children}
          {error && <ErrorState error={error} compact />}
          <DialogFooter className="flex-col gap-2 sm:flex-row sm:items-center">
            {disabledReason && <p className="text-xs text-ink-tertiary sm:mr-auto">{disabledReason}</p>}
            <Button type="button" variant="ghost" disabled={busy} onClick={() => onOpenChange(false)}>{t("action.cancel")}</Button>
            <Button type="submit" variant={destructive ? "destructive" : "default"} disabled={busy || !!disabledReason}>
              {busy ? busyLabel || t("state.saving") : submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function useSubmit(onDone, onOpenChange) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      const res = await fn();
      onOpenChange(false);
      onDone?.(res);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run, setError };
}

function IssueDialog({ inv, open, onOpenChange, onDone }) {
  const { t, locale } = useT();
  const [send, setSend] = useState(true);
  const s = useSubmit(onDone, onOpenChange);
  const email = inv.buyer?.billing_email;
  return (
    <ActionDialog open={open} onOpenChange={onOpenChange} title={t("billing.issue.title")} busy={s.busy} error={s.error}
      description={t("billing.issue.body", { total: eur(inv.total_cents, locale), n: inv.payment_terms_days ?? "—" })}
      submitLabel={t("billing.act.issue")} busyLabel={t("billing.issue.busy")}
      onSubmit={() => s.run(async () => {
        const r = await apiPost(`/invoices/${inv.id}/issue`, { send });
        return { kind: "issue", inv: r, text: t("billing.done.issued", { n: r.number || "" }) + (send ? ` ${t("billing.done.emailed")}` : "") };
      })}>
      <label className="flex items-start gap-2 text-sm">
        <Checkbox checked={send} onCheckedChange={(c) => setSend(!!c)} className="mt-0.5" />
        <span>
          {t("billing.issue.send")}
          <span className="block text-xs text-ink-tertiary">{email || t("billing.issue.send_admins")}</span>
        </span>
      </label>
    </ActionDialog>
  );
}

function MarkPaidDialog({ inv, open, onOpenChange, onDone }) {
  const { t, locale } = useT();
  const today = todayIso();
  const [paidAt, setPaidAt] = useState(today);
  const [ref, setRef] = useState("");
  const [amount, setAmount] = useState(centsToInput(settleAmount(inv), locale));
  const [note, setNote] = useState("");
  const s = useSubmit(onDone, onOpenChange);
  const cents = parseEuro(amount);
  const credit = inv.kind === "credit_note";
  let reason = null;
  if (!paidAt) reason = t("billing.paid.need_date");
  else if (paidAt > today) reason = t("billing.paid.future");
  else if (!ref.trim()) reason = t("billing.paid.need_ref");
  else if (cents === null || cents <= 0) reason = t("billing.paid.need_amount");
  return (
    <ActionDialog open={open} onOpenChange={onOpenChange} busy={s.busy} error={s.error} disabledReason={reason}
      title={credit ? t("billing.paid.title_refund") : t("billing.paid.title")}
      description={`${inv.number || ""} · ${inv.company_name || ""} · ${t(credit ? "billing.paid.refund_due" : "billing.paid.due", { amount: eur(settleAmount(inv), locale) })}`}
      submitLabel={t("billing.paid.submit")}
      onSubmit={() => s.run(async () => {
        const r = await apiPost(`/invoices/${inv.id}/mark-paid`, { paid_at: paidAt, reference: ref.trim(), amount_cents: cents, note: note.trim() || null });
        const key = credit ? "billing.done.refunded" : r.status === "paid" ? "billing.done.paid" : "billing.done.payment_recorded";
        return { kind: "markPaid", inv: r, text: t(key, { n: r.number || "", amount: eur(cents, locale) }) };
      })}>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("billing.paid.value_date")} htmlFor="mp-date">
          <DateInput id="mp-date" value={paidAt} max={today} onChange={(iso) => setPaidAt(iso)} required />
        </Field>
        <Field label={t("billing.paid.amount")} htmlFor="mp-amount">
          <Input id="mp-amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} className="text-right tabular" />
        </Field>
      </div>
      <Field label={t("billing.paid.reference")} htmlFor="mp-ref" hint={t("billing.paid.reference_hint")}>
        <Input id="mp-ref" value={ref} onChange={(e) => setRef(e.target.value)} maxLength={120} className="font-mono" autoFocus />
      </Field>
      <Field label={t("billing.paid.note")} htmlFor="mp-note">
        <Input id="mp-note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} />
      </Field>
    </ActionDialog>
  );
}

function CreditDialog({ inv, open, onOpenChange, onDone }) {
  const { t, locale } = useT();
  const [reason, setReason] = useState("");
  const s = useSubmit(onDone, onOpenChange);
  const left = (inv.total_cents || 0) - (inv.credited_cents || 0);
  const disabled = reason.trim().length < 3 ? t("billing.credit.need_reason") : null;
  return (
    <ActionDialog open={open} onOpenChange={onOpenChange} busy={s.busy} error={s.error} disabledReason={disabled}
      title={t("billing.credit.title")}
      description={t("billing.credit.body", { n: inv.number || "", amount: eur(left, locale) })}
      submitLabel={t("billing.credit.submit")}
      onSubmit={() => s.run(async () => {
        const cn = await apiPost(`/invoices/${inv.id}/credit`, { reason: reason.trim() });
        return { kind: "credit", inv: cn, text: t("billing.done.credited", { n: cn.number || "", amount: eur(cn.total_cents, locale) }),
          link: { to: `/ops/billing/${cn.id}`, label: t("billing.done.open_credit_note") } };
      })}>
      {(inv.paid_cents || 0) > 0 && <p className="text-sm text-ink-secondary">{t("billing.credit.paid_note", { amount: eur(inv.paid_cents, locale) })}</p>}
      <Field label={t("billing.credit.reason")} htmlFor="cr-reason" hint={t("billing.credit.reason_hint")}>
        <Textarea id="cr-reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} className="min-h-[72px]" autoFocus />
      </Field>
    </ActionDialog>
  );
}

function VoidDialog({ inv, open, onOpenChange, onDone }) {
  const { t } = useT();
  const [reason, setReason] = useState("");
  const s = useSubmit(onDone, onOpenChange);
  const disabled = reason.trim().length < 3 ? t("billing.void.need_reason") : null;
  return (
    <ActionDialog open={open} onOpenChange={onOpenChange} busy={s.busy} error={s.error} disabledReason={disabled} destructive
      title={t("billing.void.title")}
      description={inv.number ? t("billing.void.body_issued", { n: inv.number }) : t("billing.void.body_draft")}
      submitLabel={t("billing.act.void")}
      onSubmit={() => s.run(async () => {
        const r = await apiPost(`/invoices/${inv.id}/void`, { reason: reason.trim() });
        return { kind: "void", inv: r, text: t("billing.done.voided", { n: r.number || `${t("billing.draft")} (${r.company_name || "—"})` }) };
      })}>
      <Field label={t("billing.void.reason")} htmlFor="vo-reason">
        <Textarea id="vo-reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} className="min-h-[72px]" autoFocus />
      </Field>
    </ActionDialog>
  );
}

/**
 * const acts = useInvoiceActions({ onDone });  acts.open("markPaid", inv);  render {acts.dialogs}
 * onDone({kind, inv, text, link}) after success; resend + pdf run without a dialog.
 */
export function useInvoiceActions({ onDone }) {
  const { t } = useT();
  const [state, setState] = useState({ kind: null, inv: null, n: 0 });
  const [busyKey, setBusyKey] = useState(null);
  const close = () => setState((s) => ({ ...s, kind: null }));
  const open = async (kind, inv) => {
    if (kind === "pdf") {
      window.open(fileUrl(`/invoices/${inv.id}/pdf`), "_blank", "noopener");
      return;
    }
    if (kind === "resend") {
      setBusyKey(`resend:${inv.id}`);
      try {
        const r = await apiPost(`/invoices/${inv.id}/send`, {});
        const to = (r.sent || []).map((x) => x.to).join(", ");
        onDone?.({ kind, inv, text: to ? t(r.kind === "reminder" ? "billing.done.reminder_sent" : "billing.done.sent", { to }) : t("billing.done.sent_none") });
      } catch (e) {
        onDone?.({ kind, inv, text: e.message, tone: "danger" });
      } finally {
        setBusyKey(null);
      }
      return;
    }
    setState((s) => ({ kind, inv, n: s.n + 1 }));
  };
  const props = (k) => ({ inv: state.inv, open: state.kind === k, onOpenChange: (o) => !o && close(), onDone });
  const dialogs = state.inv ? (
    <>
      {state.kind === "issue" && <IssueDialog key={`issue-${state.n}`} {...props("issue")} />}
      {state.kind === "markPaid" && <MarkPaidDialog key={`paid-${state.n}`} {...props("markPaid")} />}
      {state.kind === "credit" && <CreditDialog key={`credit-${state.n}`} {...props("credit")} />}
      {state.kind === "void" && <VoidDialog key={`void-${state.n}`} {...props("void")} />}
    </>
  ) : null;
  return { open, dialogs, busyKey };
}

const ORDER = ["issue", "markPaid", "credit", "resend", "pdf", "void"];

/** "..." menu with every applicable action; disabled ones carry their reason as a second line. */
export function ActionsMenu({ inv, acts, canWrite = true, exclude = [], align = "end", trigger }) {
  const { t } = useT();
  const av = availability(inv, t, canWrite);
  const keys = ORDER.filter((k) => av[k] && !exclude.includes(k));
  if (keys.length === 0) return null;
  const label = { issue: t("billing.act.issue"), markPaid: inv.kind === "credit_note" ? t("billing.act.mark_refunded") : t("billing.act.mark_paid"),
    credit: t("billing.act.credit"), resend: inv.status === "overdue" ? t("billing.act.send_reminder") : t("billing.act.resend"),
    pdf: t("billing.act.pdf"), void: t("billing.act.void") };
  const stop = (e) => e.stopPropagation();
  return (
    <span onClick={stop} onKeyDown={stop} className="inline-flex">
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        {trigger || (
          <Button size="icon-sm" variant="ghost" aria-label={t("billing.act.menu", { n: inv.number || t("billing.draft") })}>
            <MoreHorizontal />
          </Button>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent align={align} className="w-60">
        {keys.map((k) => (
          <React.Fragment key={k}>
            {k === "void" && <DropdownMenuSeparator />}
            <DropdownMenuItem aria-disabled={av[k].reason ? true : undefined}
              disabled={k === "resend" && acts.busyKey === `resend:${inv.id}`}
              onSelect={(e) => { if (av[k].reason) { e.preventDefault(); return; } acts.open(k, inv); }}
              className={cn("flex-col items-start gap-0", av[k].reason && "cursor-default focus:bg-transparent",
                k === "void" && !av[k].reason && "text-danger-fg")}>
              <span className={cn(av[k].reason && "text-ink-tertiary")}>{label[k]}</span>
              {av[k].reason && <span className="text-xs text-ink-tertiary whitespace-normal">{av[k].reason}</span>}
            </DropdownMenuItem>
          </React.Fragment>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
    </span>
  );
}

