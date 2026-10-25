// Luotto for one customer: position (limit, open AR, overdue, available), hold, latest check with its sources.
// Used on the ops customer page and in the Commercial > Luotto sheet.
import React, { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { KV, Section } from "@/components/app/layout";
import { apiPatch, apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime, num, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { Field, MoneyInput } from "@/features/quotes/ops/common";
import { ComBadge, ComLoadable, Eur, Reasons, SourceLink, mutationError, useLabel } from "./common";
import { creditCheck, creditPosition, listOf } from "./shapes";

export function useCreditRun(companyId, onDone) {
  const { t, locale } = useT();
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      const r = await apiPost(`/credit/${companyId}/check`, {});
      toast.success(t("com.credit.checked"));
      onDone?.(r);
    } catch (e) {
      toast.error(mutationError(e, t, locale));
    } finally {
      setBusy(false);
    }
  };
  return { run, busy };
}

/** Customer page panel: one import + one line in pages/ops/CustomerDetail.jsx. */
export default function CreditPanel({ companyId }) {
  const { t } = useT();
  const { can } = useAuth();
  const q = useApi(companyId ? `/credit/${companyId}` : null);
  const { run, busy } = useCreditRun(companyId, () => q.reload());
  return (
    <Section title={t("com.credit.panel")} actions={can("customers.write") && q.data && (
      <Button size="xs" variant="outline" onClick={run} disabled={busy}><RefreshCw className={busy ? "animate-spin" : undefined} />{busy ? t("com.credit.checking") : t("com.credit.run")}</Button>
    )}>
      <ComLoadable q={q} perm="customers.write">{(d) => <CreditBody data={d} onChanged={() => q.reload()} />}</ComLoadable>
    </Section>
  );
}

export function CreditBody({ data, onChanged, compact }) {
  const { t, locale } = useT();
  const label = useLabel();
  const { can } = useAuth();
  const [editing, setEditing] = useState(false);
  const p = creditPosition(data);
  const check = creditCheck(data.check || listOf(data.checks)[0] || null, locale);
  const short = p.available_cents !== null && p.available_cents <= 0;
  const card = data.pending_approval;
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <ComBadge group="credit" value={p.status} />
        {p.hold && p.status !== "hold" && <ComBadge group="credit" value="hold" />}
        {p.hold_reason && <span className="text-xs text-ink-secondary">{p.hold_reason}{p.hold_auto ? ` · ${t("com.credit.auto_hold")}` : ""}</span>}
        {can("customers.write") && <Button size="xs" variant="ghost" className="ml-auto" onClick={() => setEditing(true)}>{t("com.credit.edit")}</Button>}
      </div>
      {card && (
        <p className="mb-3 rounded-lg px-3 py-2 text-sm status-in-review">
          {t("com.credit.pending_card")} <Link className="font-medium underline" to={`/ops/approvals?focus=${card.id}`}>{t("com.credit.open_card")}</Link>
        </p>
      )}
      <KV cols={compact ? 2 : 2} className="mb-4" items={[
        { label: t("com.credit.limit"), value: <Eur cents={p.limit_cents} />, hint: p.limit_basis && p.limit_basis !== "set" ? label("basis_l", p.limit_basis) : null },
        { label: t("com.credit.available"), value: <Eur cents={p.available_cents} className={short ? "text-danger-fg" : undefined} /> },
        { label: t("com.credit.open_ar"), value: <Eur cents={p.open_ar_cents} /> },
        { label: t("com.credit.overdue"), value: <Eur cents={p.overdue_cents} className={p.overdue_cents > 0 ? "text-warning-fg" : undefined} />,
          hint: p.max_days_overdue > 0 ? t("com.credit.days_overdue", { n: num(p.max_days_overdue, locale) }) : null },
        { label: t("com.credit.booked"), value: <Eur cents={p.booked_uninvoiced_cents} />, hint: t("com.credit.booked_hint") },
      ]} />
      <h3 className="mb-1.5 text-xs font-medium text-ink-secondary">{t("com.credit.latest")}</h3>
      {!check ? <p className="text-sm text-ink-tertiary">{t("com.credit.never")}</p> : <CheckView c={check} />}
      {editing && <EditDialog p={p} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); onChanged?.(); }} />}
    </div>
  );
}

function CheckView({ c }) {
  const { t, locale } = useT();
  const label = useLabel();
  return (
    <div className="flex flex-col gap-2">
      <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
        {c.rating !== null && <span><span className="text-ink-tertiary">{t("com.credit.rating")} </span><span className="font-medium">{c.rating}</span></span>}
        {c.score !== null && <span><span className="text-ink-tertiary">{t("com.credit.score")} </span><span className="tabular">{num(c.score, locale)}</span></span>}
        {c.recommended_limit_cents !== null && <span><span className="text-ink-tertiary">{t("com.credit.recommended")} </span><Eur cents={c.recommended_limit_cents} /></span>}
        {c.at && <time className="text-xs text-ink-tertiary" dateTime={c.at} title={dateTime(c.at, locale)}>{relTime(c.at, locale)}</time>}
      </p>
      {c.check_status === "error" && <p className="text-sm text-danger-fg">{t("com.credit.check_failed")}</p>}
      {c.outcome && !["unchanged", "no_decision"].includes(c.outcome) && <p className="text-xs text-ink-secondary">{label("outcome", c.outcome)}</p>}
      <Reasons items={c.reasons} />
      {c.sources.length > 0 && (
        <p className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
          <span className="text-ink-tertiary">{t("com.credit.sources")}:</span>
          {c.sources.map((s, i) => <SourceLink key={i} href={s.url}>{label("csrc", s.name)}{s.status && s.status !== "ok" ? ` (${label("cstat", s.status)})` : ""}</SourceLink>)}
        </p>
      )}
      {c.provider === "free" && <p className="text-xs text-ink-tertiary">{t("com.credit.no_bureau")}</p>}
      {c.provider === "sandbox" && <p className="text-xs text-warning-fg">{t("com.credit.sandbox")}</p>}
    </div>
  );
}

function EditDialog({ p, onClose, onSaved }) {
  const { t, locale } = useT();
  const [limit, setLimit] = useState(p.limit_cents);
  const [hold, setHold] = useState(p.hold);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const save = async () => {
    const body = {};
    if (limit !== p.limit_cents && limit !== null) body.credit_limit_cents = limit;
    if (hold !== p.hold) body.hold = hold;
    if (!Object.keys(body).length) { onClose(); return; }
    if (hold && !p.hold && !reason.trim()) { setErr(t("com.credit.need_reason")); return; }
    body.reason = reason.trim() || null;
    setBusy(true); setErr(null);
    try {
      await apiPatch(`/credit/${p.company_id}`, body);
      toast.success(t("action.saved"));
      onSaved();
    } catch (e) {
      setErr(mutationError(e, t, locale));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>{t("com.credit.edit_title", { name: p.name || "" })}</DialogTitle><DialogDescription>{t("com.credit.edit_hint")}</DialogDescription></DialogHeader>
        <div className="flex flex-col gap-3">
          <Field label={t("com.credit.limit_eur")} htmlFor="cr-limit"><MoneyInput id="cr-limit" cents={limit} onChange={setLimit} /></Field>
          <label className="flex items-center gap-2 text-sm"><Switch checked={hold} onCheckedChange={setHold} />{t("com.credit.hold")}</label>
          {(hold || limit !== p.limit_cents) && <Field label={hold && !p.hold ? t("com.credit.hold_reason") : t("com.credit.change_note")} htmlFor="cr-why"><Textarea id="cr-why" value={reason} onChange={(e) => setReason(e.target.value)} className="min-h-[56px]" /></Field>}
        </div>
        {err && <p role="alert" className="text-sm text-danger-fg">{err}</p>}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>{t("action.cancel")}</Button>
          <Button onClick={save} disabled={busy}>{busy ? t("state.saving") : t("action.save")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
