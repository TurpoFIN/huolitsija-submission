// Ops actions on a claim: start review, propose a decision (goes to the approval queue), decide inline
// when allowed (claims.approve), mark paid (billing.write).
import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { DateInput } from "@/components/ui/DateInput";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { apiPost } from "@/lib/api";
import { fourEyesState } from "@/lib/fourEyes";
import { FourEyesNotice } from "@/components/app/FourEyes";
import { useAuth } from "@/lib/auth";
import { dateTime, money } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { Field, centsToInput, eurToCents } from "@/features/exceptions/common";

function useAction(onDone) {
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const run = async (kind, fn, okMsg) => {
    setBusy(kind);
    setError(null);
    try {
      await fn();
      if (okMsg) toast.success(okMsg);
      onDone?.(okMsg);
      return true;
    } catch (e) {
      setError(e);
      return false;
    } finally {
      setBusy(null);
    }
  };
  return { busy, error, run };
}

function ProposalForm({ claim, onDone }) {
  const { t, locale } = useT();
  const cap = claim.cap_eur_cents;
  const suggested = cap != null ? Math.min(cap, claim.amount_claimed_cents) : claim.amount_claimed_cents;
  const [decision, setDecision] = useState("approve");
  const [amount, setAmount] = useState(centsToInput(suggested));
  const [note, setNote] = useState("");
  const [touched, setTouched] = useState(false);
  const { busy, error, run } = useAction(onDone);
  useEffect(() => { setAmount(centsToInput(suggested)); }, [suggested]);
  const cents = eurToCents(amount);
  const aboveClaimed = decision === "approve" && cents > claim.amount_claimed_cents;
  const aboveCap = decision === "approve" && cap != null && cents > cap;
  const amountBad = decision === "approve" && !(cents > 0);
  const noteBad = note.trim().length < 3;
  const submit = () => {
    setTouched(true);
    if (amountBad || aboveClaimed || noteBad) return;
    run("propose", () => apiPost(`/claims/${claim.id}/propose`, {
      decision, amount_cents: decision === "approve" ? cents : null, note: note.trim(),
    }), t("claims.ops.done.propose"));
  };
  return (
    <div className="flex flex-col gap-3">
      <Segmented value={decision} onChange={setDecision} label={t("claims.ops.propose")} className="self-start"
        options={[{ value: "approve", label: t("claims.ops.approve") }, { value: "reject", label: t("claims.ops.reject") }]} />
      {decision === "approve" && (
        <Field label={t("claims.ops.amount")} htmlFor="cl-prop-amount"
          error={touched && amountBad ? t("claims.ops.amount_needed") : aboveClaimed ? t("claims.ops.above_claimed", { claimed: money(claim.amount_claimed_cents, locale, { decimals: 2 }) }) : null}>
          <Input id="cl-prop-amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} className="w-40 text-right tabular"
            aria-invalid={(touched && amountBad) || aboveClaimed ? true : undefined} />
        </Field>
      )}
      {aboveCap && !aboveClaimed && (
        <p className="flex items-start gap-2 rounded-lg px-2.5 py-2 text-sm status-pending">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          {t("claims.ops.above_cap", { cap: money(cap, locale, { decimals: 2 }) })}
        </p>
      )}
      <Field label={t("claims.ops.note")} htmlFor="cl-prop-note" error={touched && noteBad ? t("claims.ops.note_short") : null}>
        <Textarea id="cl-prop-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("claims.ops.note_ph")} maxLength={2000}
          aria-invalid={touched && noteBad ? true : undefined} />
      </Field>
      <div>
        <Button onClick={submit} disabled={!!busy}>{busy ? t("state.saving") : t("claims.ops.submit")}</Button>
      </div>
      {error && <ErrorState compact error={error} />}
    </div>
  );
}

function PendingApproval({ claim, onDone }) {
  const { t, locale } = useT();
  const { can } = useAuth();
  const p = claim.proposal || {};
  const [amount, setAmount] = useState(centsToInput(p.amount_cents));
  const [note, setNote] = useState("");
  const { busy, error, run } = useAction(onDone);
  useEffect(() => { setAmount(centsToInput(p.amount_cents)); }, [p.amount_cents]);
  const approveProposal = p.decision === "approve";
  const cents = eurToCents(amount);
  const amountBad = approveProposal && !(cents > 0);
  const aboveClaimed = approveProposal && cents > claim.amount_claimed_cents;
  const aboveCap = approveProposal && claim.cap_eur_cents != null && cents > claim.cap_eur_cents;
  const decide = (decision, override = false) => run(decision, () => apiPost(`/approvals/${claim.approval_id}/decide`, {
    decision, note: note.trim() || null, data: decision === "approved" && approveProposal ? { amount_cents: cents } : null,
    ...(override ? { override: true } : {}),
  }), t("claims.ops.done.decide"));
  const fourEyes = fourEyesState(error);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge size="sm" group="approval" value="pending" label={t("claims.ops.pending")} />
        <span className="text-sm font-medium">
          {approveProposal ? t("claims.ops.proposal.approve", { amount: money(p.amount_cents, locale, { decimals: 2 }) }) : t("claims.ops.proposal.reject")}
        </span>
        {p.above_cap && <StatusBadge size="sm" state="pending" label={t("claims.ops.above_cap_flag")} />}
      </div>
      {p.note && <p className="whitespace-pre-wrap text-sm text-ink-secondary">{p.note}</p>}
      <p className="text-xs text-ink-tertiary">{t("claims.ops.proposed_by", { name: p.by?.name || "—", time: dateTime(p.at, locale) })}</p>
      <div>
        <Button variant="link" size="sm" className="h-auto px-0" asChild><Link to={`/ops/approvals?focus=${claim.approval_id}`}>{t("claims.ops.open_approvals")}</Link></Button>
      </div>
      {can("claims.approve") ? (
        <div className="flex flex-col gap-2 rounded-lg p-3 shadow-[inset_0_0_0_0.5px_var(--border-default)]">
          {approveProposal && (
            <Field label={t("claims.ops.decide_amount")} htmlFor="cl-dec-amount"
              error={amountBad ? t("claims.ops.amount_needed") : aboveClaimed ? t("claims.ops.above_claimed", { claimed: money(claim.amount_claimed_cents, locale, { decimals: 2 }) }) : null}
              hint={aboveCap ? t("claims.ops.above_cap", { cap: money(claim.cap_eur_cents, locale, { decimals: 2 }) }) : null}>
              <Input id="cl-dec-amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} className="w-40 text-right tabular"
                aria-invalid={amountBad || aboveClaimed ? true : undefined} />
            </Field>
          )}
          <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("claims.ops.decide_note")} aria-label={t("claims.ops.decide_note")} />
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => decide("approved")} disabled={!!busy || amountBad || aboveClaimed || !!fourEyes}>
              {busy === "approved" ? t("state.saving") : approveProposal ? t("claims.ops.confirm_pay") : t("claims.ops.confirm_reject")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => decide("rejected")} disabled={!!busy}>
              {busy === "rejected" ? t("state.saving") : t("claims.ops.return")}
            </Button>
          </div>
        </div>
      ) : (
        <p className="text-xs text-ink-tertiary">{t("claims.ops.no_approve_perm")}</p>
      )}
      {fourEyes ? <FourEyesNotice state={fourEyes} note={note} busy={!!busy} onOverride={() => decide("approved", true)} />
        : error && <ErrorState compact error={error} />}
    </div>
  );
}

function MarkPaid({ claim, onDone }) {
  const { t } = useT();
  const { can } = useAuth();
  const [paidAt, setPaidAt] = useState(new Date().toISOString().slice(0, 10));
  const [ref, setRef] = useState("");
  const { busy, error, run } = useAction(onDone);
  if (!can("billing.write")) return <p className="text-xs text-ink-tertiary">{t("claims.ops.no_billing")}</p>;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-end gap-2">
        <Field label={t("claims.ops.paid_date")} htmlFor="cl-paid-at">
          <DateInput id="cl-paid-at" value={paidAt} max={todayIso()} onChange={(iso) => setPaidAt(iso)} className="w-40" />
        </Field>
        <Field label={t("claims.ops.paid_ref")} htmlFor="cl-paid-ref" className="min-w-[160px] flex-1">
          <Input id="cl-paid-ref" value={ref} onChange={(e) => setRef(e.target.value)} maxLength={120} />
        </Field>
        <Button disabled={!!busy || !paidAt}
          onClick={() => run("paid", () => apiPost(`/claims/${claim.id}/mark-paid`, { paid_at: paidAt, reference: ref.trim() || null }), t("claims.ops.done.paid"))}>
          {busy ? t("state.saving") : t("claims.ops.mark_paid")}
        </Button>
      </div>
      {error && <ErrorState compact error={error} />}
    </div>
  );
}

export default function ClaimOpsActions({ claim, onDone }) {
  const { t, locale } = useT();
  const { busy, error, run } = useAction(onDone);
  const pending = claim.approval_id && claim.proposal && (!claim.approval || claim.approval.status === "pending");
  const open = ["submitted", "in_review"].includes(claim.status);
  if (!open && claim.status !== "approved") return null;
  const declined = claim.last_proposal_declined;
  return (
    <div className="flex flex-col gap-3">
      {claim.status === "submitted" && (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" onClick={() => run("review", () => apiPost(`/claims/${claim.id}/review`, {}), t("claims.ops.done.review"))} disabled={!!busy}>
            {busy ? t("state.saving") : t("claims.ops.review")}
          </Button>
          <span className="text-xs text-ink-tertiary">{t("claims.ops.review_hint")}</span>
        </div>
      )}
      {error && <ErrorState compact error={error} />}
      {open && declined && !pending && (
        <p className="text-xs text-warning-fg">
          {t("claims.ops.declined", { name: declined.by || "—", time: dateTime(declined.at, locale) })}{declined.note ? `: ${declined.note}` : ""}
        </p>
      )}
      {open && (pending ? <PendingApproval claim={claim} onDone={onDone} /> : <ProposalForm claim={claim} onDone={onDone} />)}
      {claim.status === "approved" && <MarkPaid claim={claim} onDone={onDone} />}
    </div>
  );
}


function todayIso() {
  // Helsinki calendar day (sv-SE formats as yyyy-mm-dd)
  return new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Helsinki" });
}
