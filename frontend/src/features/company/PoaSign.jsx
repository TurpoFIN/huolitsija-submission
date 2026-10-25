// Customs power of attorney (valtakirja): current status and electronic signing in the portal.
import React, { useState } from "react";
import { FileText, PenLine } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import DateInput from "@/components/ui/DateInput";
import { Input } from "@/components/ui/input";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { KV } from "@/components/app/layout";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { apiPost, fileUrl, useApi } from "@/lib/api";
import { date, dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Field, PoaBadge, poaState } from "./common";

const SCOPES = ["import", "export", "transit", "ens", "correspondence"];
const iso = (d) => d.toISOString().slice(0, 10);
function plusYears(s, n) {
  const d = new Date(`${s}T00:00:00Z`);
  d.setUTCFullYear(d.getUTCFullYear() + n);
  return iso(d);
}

/** Read-only facts of the signed POA. audience "ops" adds signature evidence (IP, hash). */
export function PoaSummary({ company, audience = "customer" }) {
  const { t, locale } = useT();
  const poa = company?.poa;
  const state = poaState(poa, company?.poa_valid);
  if (state === "missing") return <p className="text-sm text-ink-tertiary">{t("company.poa.missing")}</p>;
  const s = poa.signature || {};
  const doc = company.poa_document;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <PoaBadge poa={poa} valid={company.poa_valid} size="default" />
        {poa.demo && audience === "ops" && <span className="text-xs text-ink-tertiary">{t("customers.demo_hint")}</span>}
      </div>
      <KV cols={1} items={[
        { label: t("company.poa.type"), value: t(`company.poa.type.${poa.type}`) },
        { label: t("company.poa.scope"), value: (poa.scope || []).map((x) => t(`company.poa.scope.${x}`)).join("; ") },
        { label: t("company.poa.validity"), value: `${date(poa.valid_from, locale)} – ${date(poa.valid_to, locale)}` },
        { label: t("company.poa.signed_by"), value: [s.typed_name, s.title].filter(Boolean).join(", "), hint: s.email },
        { label: t("company.poa.signed_at"), value: dateTime(poa.signed_at, locale) },
        poa.revoked_at && { label: t("company.poa.revoked"), value: dateTime(poa.revoked_at, locale) },
        audience === "ops" && { label: t("company.poa.ip"), value: s.ip, mono: true },
        audience === "ops" && { label: t("company.poa.evidence"), value: poa.evidence_hash ? `${poa.evidence_hash.slice(0, 16)}…` : null, mono: true, hint: poa.text_version },
      ]} />
      {doc && (
        <a href={fileUrl(`/documents/${doc.id}/file`)} target="_blank" rel="noreferrer"
          className="inline-flex items-center gap-1.5 self-start text-sm text-ink-accent hover:underline">
          <FileText className="size-4" aria-hidden /> {doc.filename}
        </a>
      )}
    </div>
  );
}

function SignForm({ company, onSigned, onCancel }) {
  const { t, locale } = useT();
  const today = iso(new Date());
  // Canonical POA texts (same version as the signed PDF) in the UI language.
  const terms = useApi(`/company/poa/terms?lang=${locale}`);
  const tx = terms.data;
  const indirectAllowed = !!tx?.indirect_allowed;
  const [f, setF] = useState({ type: "direct", scope: ["import", "export", "correspondence"], valid_from: today,
    valid_to: plusYears(today, 1), typed_name: "", title: company?.signatory?.title || "", accept: false });
  const [errors, setErrors] = useState({});
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k, v) => { setF((p) => ({ ...p, [k]: v })); setErrors((p) => ({ ...p, [k]: undefined })); };
  const profileReady = !!(company?.name && company?.business_id);

  const submit = async (e) => {
    e.preventDefault();
    const errs = {};
    if (!f.scope.length) errs.scope = t("company.poa.scope");
    if (!f.valid_from || !f.valid_to || f.valid_to <= f.valid_from) errs.valid_to = t("company.poa.valid_hint");
    else if (f.valid_to > plusYears(f.valid_from, 5)) errs.valid_to = t("company.poa.valid_hint");
    if (f.typed_name.trim().split(/\s+/).length < 2) errs.typed_name = t("company.poa.typed_name");
    if (!f.accept) errs.accept = true;
    setErrors(errs);
    if (Object.keys(errs).length) return;
    setBusy(true);
    setErr(null);
    try {
      const res = await apiPost("/company/poa", { ...f, typed_name: f.typed_name.trim(), title: f.title.trim() || null });
      toast.success(t("company.poa.signed"));
      onSigned(res);
    } catch (ex) {
      setErr(ex);
    } finally {
      setBusy(false);
    }
  };

  if (!profileReady) return <p className="text-sm text-warning-fg">{t("company.poa.need_profile")}</p>;
  if (terms.loading && !tx) return <LoadingRows rows={5} />;
  if (terms.error && !tx) return <ErrorState error={terms.error} onRetry={terms.reload} />;

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-xs text-ink-secondary">{t("company.poa.type")}</legend>
        <RadioGroup value={f.type} onValueChange={(v) => set("type", v)} className="gap-2">
          {["direct", "indirect"].map((ty) => {
            const locked = ty === "indirect" && !indirectAllowed;
            return (
              <label key={ty} className={cn("flex items-start gap-2.5 rounded-lg border px-3 py-2.5",
                f.type === ty && "border-[color:var(--border-strong)] bg-subtle", locked ? "cursor-not-allowed opacity-60" : "cursor-pointer")}>
                <RadioGroupItem value={ty} disabled={locked || busy} className="mt-0.5" aria-label={t(`company.poa.type.${ty}`)} />
                <span className="min-w-0">
                  <span className="block text-sm font-medium">{t(`company.poa.type.${ty}`)}</span>
                  <span className="block text-xs text-ink-secondary">{tx.types?.[ty]?.text}</span>
                  {locked && <span className="mt-0.5 block text-xs text-ink-tertiary">{t("company.poa.indirect_locked")}</span>}
                </span>
              </label>
            );
          })}
        </RadioGroup>
      </fieldset>
      <fieldset className="flex flex-col gap-1.5">
        <legend className="mb-1 text-xs text-ink-secondary">{t("company.poa.scope")}</legend>
        {SCOPES.map((s) => (
          <label key={s} className="flex items-start gap-2 text-sm">
            <Checkbox aria-label={tx.scopes?.[s] || t(`company.poa.scope.${s}`)} className="mt-0.5" checked={f.scope.includes(s)} disabled={busy}
              onCheckedChange={(v) => set("scope", v ? [...f.scope, s] : f.scope.filter((x) => x !== s))} />
            {tx.scopes?.[s] || t(`company.poa.scope.${s}`)}
          </label>
        ))}
        {errors.scope && <p className="text-xs text-danger-fg" role="alert">{t("company.poa.scope")}</p>}
      </fieldset>
      <div className="grid grid-cols-2 gap-3">
        <Field label={t("company.poa.valid_from")} htmlFor="poa-from">
          <DateInput id="poa-from" value={f.valid_from} onChange={(iso) => set("valid_from", iso)} disabled={busy} />
        </Field>
        <Field label={t("company.poa.valid_to")} htmlFor="poa-to" error={errors.valid_to}>
          <DateInput id="poa-to" value={f.valid_to} onChange={(iso) => set("valid_to", iso)} disabled={busy}
            min={f.valid_from || undefined} max={f.valid_from ? plusYears(f.valid_from, 5) : undefined}
            aria-invalid={!!errors.valid_to} />
        </Field>
      </div>
      <div className="flex flex-col gap-1 text-xs text-ink-secondary">
        {tx.undertakings?.length > 0 && (
          <>
            <p className="font-medium text-foreground">{t("company.poa.undertakings_title")}</p>
            <ul className="ml-4 list-disc space-y-0.5">{tx.undertakings.map((u, i) => <li key={i}>{u}</li>)}</ul>
          </>
        )}
        {tx.retention && <p><span className="font-medium text-foreground">{t("company.poa.retention_title")}:</span> {tx.retention}.</p>}
        <p className="text-ink-tertiary">{t("company.poa.text_version")} {tx.version}</p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
        <Field label={t("company.poa.typed_name")} htmlFor="poa-name" error={errors.typed_name}>
          <Input id="poa-name" value={f.typed_name} onChange={(e) => set("typed_name", e.target.value)} disabled={busy} autoComplete="name" className="italic" />
        </Field>
        <Field label={t("company.poa.signer_title")} htmlFor="poa-title">
          <Input id="poa-title" value={f.title} onChange={(e) => set("title", e.target.value)} disabled={busy} autoComplete="organization-title" />
        </Field>
      </div>
      <label className={cn("flex items-start gap-2 text-sm", errors.accept && "text-danger-fg")}>
        <Checkbox aria-label={tx.types?.[f.type]?.consent} className="mt-0.5" checked={f.accept} onCheckedChange={(v) => set("accept", !!v)} disabled={busy} aria-invalid={!!errors.accept} />
        <span>{tx.types?.[f.type]?.consent}</span>
      </label>
      {err && <ErrorState compact error={err} />}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          <PenLine className="size-3.5" /> {busy ? t("company.poa.signing") : t("company.poa.sign")}
        </Button>
        {onCancel && <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>{t("action.cancel")}</Button>}
      </div>
    </form>
  );
}

export function PoaSign({ company, canSign, onChange }) {
  const { t } = useT();
  const state = poaState(company?.poa, company?.poa_valid);
  const [signing, setSigning] = useState(false);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [err, setErr] = useState(null);
  const showForm = canSign && (signing || state === "missing");

  const revoke = async () => {
    setRevoking(true);
    setErr(null);
    try {
      const res = await apiPost("/company/poa/revoke", {});
      toast.success(t("company.poa.revoked_done"));
      onChange(res);
      setRevokeOpen(false);
    } catch (ex) {
      setErr(ex);
      setRevokeOpen(false);
    } finally {
      setRevoking(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <p className="text-xs text-ink-tertiary">{t("company.poa.what")}</p>
      {state !== "missing" && !signing && <PoaSummary company={company} />}
      {state === "missing" && !canSign && <p className="text-sm text-ink-tertiary">{t("company.poa.missing")} · {t("company.poa.read_only")}</p>}
      {err && <ErrorState compact error={err} />}
      {showForm ? (
        <SignForm company={company} onSigned={(res) => { setSigning(false); onChange(res); }}
          onCancel={state !== "missing" ? () => setSigning(false) : null} />
      ) : (
        canSign && (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant={state === "valid" ? "outline" : "default"} onClick={() => setSigning(true)}>
              <PenLine className="size-3.5" /> {t("company.poa.renew")}
            </Button>
            {state === "valid" && (
              <Button size="sm" variant="ghost" onClick={() => setRevokeOpen(true)}>{t("company.poa.revoke")}</Button>
            )}
          </div>
        )
      )}
      <AlertDialog open={revokeOpen} onOpenChange={setRevokeOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("company.poa.revoke_title")}</AlertDialogTitle>
            <AlertDialogDescription>{t("company.poa.revoke_body")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revoking}>{t("action.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={(e) => { e.preventDefault(); revoke(); }} disabled={revoking}>
              {revoking ? t("state.saving") : t("company.poa.revoke")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
