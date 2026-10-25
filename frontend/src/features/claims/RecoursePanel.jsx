// Ops: carrier recourse on a paid claim (claim.recourse, services/claims_recourse.py). Draft -> owner queue card
// (carrier_recourse) -> approved -> sent only with the carrier's e-mail and recourse contract -> recoveries.
import React, { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { KV } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime, money } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { Field, centsToInput, eurToCents } from "@/features/exceptions/common";

const STATE = { draft: "expired", pending_approval: "pending", approved: "in-review", sent: "submitted", recovered: "success", closed: "expired" };

function Letter({ r }) {
  const { t, locale } = useT();
  const [lang, setLang] = useState(locale === "en" ? "en" : "fi");
  const letter = lang === "en" ? r.letter_en : r.letter_fi;
  if (!letter) return null;
  return (
    <details className="mt-3">
      <summary className="cursor-pointer text-sm font-medium">{t("recourse.letter")}</summary>
      <div className="mt-2 flex gap-1">
        {["fi", "en"].map((l) => (
          <Button key={l} size="xs" variant={lang === l ? "secondary" : "ghost"} onClick={() => setLang(l)}>{t(`recourse.lang.${l}`)}</Button>
        ))}
      </div>
      <p className="mt-2 text-sm font-medium">{letter.subject}</p>
      <pre className="mt-1 whitespace-pre-wrap font-sans text-sm text-ink-secondary">{letter.body}</pre>
    </details>
  );
}

export default function RecoursePanel({ claim, onDone }) {
  const { t, locale } = useT();
  const { user } = useAuth();
  const perms = user?.permissions || [];
  const r = claim.recourse;
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [amount, setAmount] = useState("");
  const [ref, setRef] = useState("");
  const [close, setClose] = useState(false);
  const act = async (kind, fn, ok) => {
    setBusy(kind);
    setError(null);
    try {
      await fn();
      toast.success(ok);
      onDone?.(ok);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  const create = () => act("create", () => apiPost(`/claims/${claim.id}/recourse`, {}), t("recourse.done.created"));
  const redraft = !r || r.status === "draft" || (r.status === "closed" && r.closed_reason === "declined");
  if (!r || r.status === "draft") {
    return (
      <div className="space-y-2">
        <p className="text-sm text-ink-secondary">{t("recourse.none")}</p>
        <Button size="sm" onClick={create} disabled={!!busy}>{busy ? t("state.saving") : t("recourse.create")}</Button>
        {error && <ErrorState compact error={error} />}
      </div>
    );
  }
  const cents = eurToCents(amount);
  const left = (r.paid_cents || 0) - (r.recovered_cents || 0);
  const canRecover = ["approved", "sent", "recovered"].includes(r.status) && perms.includes("billing.write");
  const awaiting = r.status === "approved" && r.awaiting;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusBadge size="sm" state={STATE[r.status]} value={r.status} label={t(`recourse.status.${r.status}`)} />
        {awaiting && <StatusBadge size="sm" state="pending" label={t(`recourse.awaiting.${r.awaiting}`)} />}
        {r.status === "pending_approval" && r.approval_id && (
          <Link to={`/ops/approvals?focus=${r.approval_id}`} className="text-sm text-ink-secondary hover:underline">{t("recourse.open_card")}</Link>
        )}
      </div>
      <KV cols={2} items={[
        { label: t("recourse.carrier"), value: r.carrier_name || t("recourse.no_carrier") },
        { label: t("recourse.regime"), value: r.regime_label || "—" },
        { label: t("recourse.paid"), value: <span className="tabular">{money(r.paid_cents, locale, { decimals: 2 })}</span> },
        { label: t("recourse.cap"), value: r.cap_eur_cents != null ? <span className="tabular">{`${money(r.cap_eur_cents, locale, { decimals: 2 })} (${r.cap_sdr} SDR)`}</span> : "—" },
        { label: t("recourse.amount"), value: <span className="font-medium tabular">{money(r.amount_cents, locale, { decimals: 2 })}</span> },
        { label: t("recourse.recovered"), value: <span className="tabular">{money(r.recovered_cents || 0, locale, { decimals: 2 })}</span> },
        r.transport_doc_no && { label: r.transport_doc_label, value: r.transport_doc_no },
        r.sent_at && { label: t("recourse.sent"), value: `${dateTime(r.sent_at, locale)} · ${r.sent_to}` },
      ]} />
      {(r.cap_basis || r.cap_basis_fi) && <p className="text-xs text-ink-tertiary">{locale === "en" ? r.cap_basis : (r.cap_basis_fi || r.cap_basis)}</p>}
      {awaiting && (
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm text-ink-secondary">{t(`recourse.awaiting_help.${r.awaiting}`)}</p>
          {r.carrier_id && <Link to={`/ops/carriers/${r.carrier_id}`} className="text-sm hover:underline">{t("recourse.open_carrier")}</Link>}
          {perms.includes("claims.approve") && (
            <Button size="sm" variant="outline" disabled={!!busy}
              onClick={() => act("send", () => apiPost(`/claims/${claim.id}/recourse/send`, {}), t("recourse.done.send"))}>
              {t("recourse.send")}
            </Button>
          )}
        </div>
      )}
      {canRecover && left > 0 && (
        <div className="flex flex-wrap items-end gap-2">
          <Field label={t("recourse.recovery_amount")} htmlFor="rc-amount">
            <Input id="rc-amount" inputMode="decimal" value={amount} placeholder={centsToInput(left)}
              onChange={(e) => setAmount(e.target.value)} className="w-36 text-right tabular" />
          </Field>
          <Field label={t("recourse.recovery_ref")} htmlFor="rc-ref">
            <Input id="rc-ref" value={ref} onChange={(e) => setRef(e.target.value)} className="w-40" />
          </Field>
          <label className="flex items-center gap-1.5 pb-2 text-sm">
            <input type="checkbox" checked={close} onChange={(e) => setClose(e.target.checked)} /> {t("recourse.close")}
          </label>
          <Button size="sm" disabled={!!busy || !(cents > 0) || cents > left}
            onClick={() => act("recover", () => apiPost(`/claims/${claim.id}/recourse/recovery`, {
              amount_cents: cents, reference: ref.trim() || null, close,
            }), t("recourse.done.recovered")).then(() => { setAmount(""); setRef(""); setClose(false); })}>
            {t("recourse.record")}
          </Button>
        </div>
      )}
      {redraft && (
        <Button size="sm" variant="outline" onClick={create} disabled={!!busy}>{t("recourse.redraft")}</Button>
      )}
      {error && <ErrorState compact error={error} />}
      <Letter r={r} />
    </div>
  );
}
