// Customer files (or edits a draft of) a cargo claim with a live liability-cap calculation.
import React, { useEffect, useMemo, useState } from "react";
import { Paperclip, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { DateInput } from "@/components/ui/DateInput";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Segmented } from "@/components/app/FilterBar";
import { FileUpload } from "@/components/app/FileUpload";
import { ErrorState } from "@/components/app/states";
import { apiGet, apiPatch, apiPost, apiUpload, useApi } from "@/lib/api";
import { kg as fmtKg } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { Field, ShipmentPicker, centsToInput, eurToCents, useDebounced } from "@/features/exceptions/common";
import CapCalculation from "./CapCalculation";
import { CLAIM_TYPES, typeLabel } from "./text";

const toNum = (s) => {
  if (s === null || s === undefined || String(s).trim() === "") return null;
  const n = Number(String(s).replace(/\s/g, "").replace(",", "."));
  return Number.isFinite(n) ? n : NaN;
};

/**
 * initial: a draft claim to edit, or null for a new one. presetShipmentId: open with this shipment chosen.
 * onSaved(claim, {submitted}) after save; onCancel closes the form.
 */
export default function ClaimForm({ initial, presetShipmentId, onSaved, onCancel, canFile = true }) {
  const { t, locale } = useT();
  const [shipment, setShipment] = useState(initial ? { id: initial.shipment_id, reference: initial.shipment_reference, customer_name: initial.company_name } : null);
  const [type, setType] = useState(initial?.type || "damage");
  const [grossKg, setGrossKg] = useState(initial?.gross_kg_affected != null ? String(initial.gross_kg_affected).replace(".", ",") : "");
  const [packages, setPackages] = useState(initial?.packages_affected != null ? String(initial.packages_affected) : "");
  const [amount, setAmount] = useState(initial ? centsToInput(initial.amount_claimed_cents) : "");
  const [dateOfLoss, setDateOfLoss] = useState(initial?.date_of_loss?.slice(0, 10) || "");
  const [noticeOn, setNoticeOn] = useState(initial?.notice_given_on?.slice(0, 10) || "");
  const [description, setDescription] = useState(initial?.description || "");
  const [docs, setDocs] = useState(initial?.documents || []);
  const [uploading, setUploading] = useState(false);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    if (!presetShipmentId || initial) return undefined;
    let alive = true;
    apiGet(`/shipments/${presetShipmentId}`).then((s) => { if (alive && s?.id) setShipment(s); }).catch(() => {});
    return () => { alive = false; };
  }, [presetShipmentId, initial]);

  const kgNum = toNum(grossKg);
  const pkgNum = toNum(packages);
  const cents = eurToCents(amount);
  const estParams = useDebounced(
    shipment ? `shipment_id=${shipment.id}&type=${type}${kgNum > 0 ? `&gross_kg=${kgNum}` : ""}${pkgNum > 0 ? `&packages=${Math.round(pkgNum)}` : ""}` : null,
    400,
  );
  const est = useApi(estParams ? `/claims/estimate?${estParams}` : null);
  const shipKg = shipment?.weight_kg ?? est.data?.shipment?.weight_kg ?? null;

  const needKg = type !== "delay";
  const errors = {
    shipment: !shipment,
    kg: needKg ? !(kgNum > 0) || (shipKg && kgNum > shipKg * 1.001) : kgNum !== null && !(kgNum >= 0),
    amount: !(cents > 0),
    description: description.trim().length < 10,
  };
  const missing = [
    errors.shipment && t("claims.form.need_shipment"),
    errors.kg && t("claims.form.need_kg"),
    errors.amount && t("claims.form.need_amount"),
    errors.description && t("claims.form.need_description"),
  ].filter(Boolean);

  const kgError = !touched && !grossKg ? null
    : needKg && !(kgNum > 0) ? t("claims.form.gross_kg_needed")
      : shipKg && kgNum > shipKg * 1.001 ? t("claims.form.gross_kg_too_much", { kg: fmtKg(shipKg, locale) }) : null;

  const upload = async (files) => {
    setUploading(true);
    setError(null);
    try {
      const added = [];
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        fd.append("type", "other");
        if (shipment?.id) fd.append("shipment_id", shipment.id);
        const d = await apiUpload("/documents", fd);
        added.push({ id: d.id, filename: d.filename || f.name, type: d.type });
      }
      setDocs((list) => [...list, ...added]);
    } catch (e) {
      setError(e);
    } finally {
      setUploading(false);
    }
  };

  const save = async (submit) => {
    setTouched(true);
    if (missing.length) return;
    setBusy(submit ? "submit" : "draft");
    setError(null);
    const body = {
      type, description: description.trim(), amount_claimed_cents: cents,
      gross_kg_affected: kgNum > 0 ? kgNum : null, packages_affected: pkgNum > 0 ? Math.round(pkgNum) : null,
      date_of_loss: dateOfLoss || null, notice_given_on: noticeOn || null, document_ids: docs.map((d) => d.id),
    };
    try {
      let claim;
      if (initial) {
        claim = await apiPatch(`/claims/${initial.id}`, body);
        if (submit) claim = await apiPost(`/claims/${initial.id}/submit`, {});
      } else {
        claim = await apiPost("/claims", { ...body, shipment_id: shipment.id, submit });
      }
      toast.success(submit ? t("claims.form.submitted") : t("claims.form.saved"));
      onSaved?.(claim, { submitted: submit });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  const typeOptions = useMemo(() => CLAIM_TYPES.map((k) => ({ value: k, label: typeLabel(t, k) })), [t]);

  return (
    <div className="flex flex-col gap-4">
      <Field label={t("claims.form.shipment")} htmlFor="cl-shipment" error={touched && errors.shipment ? t("claims.form.need_shipment") : null}
        hint={type === "loss" && !shipment ? t("claims.form.shipment_loss") : null}>
        {initial ? (
          <p className="text-sm font-medium">{initial.shipment_reference}</p>
        ) : (
          <ShipmentPicker id="cl-shipment" value={shipment} onChange={setShipment}
            query={type === "loss" ? "" : "status=delivered"}
            filter={(s) => s.status !== "cancelled" && s.status !== "draft"}
            placeholder={t("claims.form.shipment_ph")} emptyText={t("claims.form.shipment_none")} />
        )}
      </Field>

      <Field label={t("claims.form.type")} hint={t(`claims.type_hint.${type}`)}>
        <Segmented value={type} onChange={setType} options={typeOptions} label={t("claims.form.type")} className="self-start" />
      </Field>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("claims.form.gross_kg")} htmlFor="cl-kg" error={kgError}
          hint={shipKg ? t("claims.form.gross_kg_hint", { kg: fmtKg(shipKg, locale) }) : null}>
          <Input id="cl-kg" inputMode="decimal" value={grossKg} onChange={(e) => setGrossKg(e.target.value)} aria-invalid={kgError ? true : undefined} />
        </Field>
        <Field label={t("claims.form.packages")} htmlFor="cl-pkg" hint={t("claims.form.packages_hint")}>
          <Input id="cl-pkg" inputMode="numeric" value={packages} onChange={(e) => setPackages(e.target.value)} />
        </Field>
        <Field label={t("claims.form.amount")} htmlFor="cl-amount"
          error={(touched || amount) && !(cents > 0) ? t("claims.form.amount_invalid") : null}>
          <Input id="cl-amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} className="text-right tabular"
            aria-invalid={(touched || amount) && !(cents > 0) ? true : undefined} placeholder="0,00" />
        </Field>
        <Field label={t("claims.form.date_of_loss")} htmlFor="cl-date">
          <DateInput id="cl-date" value={dateOfLoss} max={todayIso()} onChange={(iso) => setDateOfLoss(iso)} clearable />
        </Field>
        <Field label={t("claims.form.notice_on")} htmlFor="cl-notice" hint={t("claims.form.notice_on_hint")} className="sm:col-span-2">
          <DateInput id="cl-notice" value={noticeOn} max={todayIso()} onChange={(iso) => setNoticeOn(iso)} clearable
            aria-describedby="cl-notice-hint" />
        </Field>
      </div>

      <CapCalculation est={est.data} loading={est.loading || est.refreshing || (!!shipment && !estParams)} error={est.error}
        claimedCents={cents > 0 ? cents : null} />

      <Field label={t("claims.form.description")} htmlFor="cl-desc" error={touched && errors.description ? t("claims.form.description_short") : null}>
        <Textarea id="cl-desc" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={5000} className="min-h-[96px]"
          placeholder={t("claims.form.description_ph")} aria-invalid={touched && errors.description ? true : undefined} />
      </Field>

      <Field label={t("claims.form.documents")}>
        {docs.length > 0 && (
          <ul className="mb-1 flex flex-col gap-1">
            {docs.map((d) => (
              <li key={d.id} className="flex items-center gap-2 text-sm">
                <Paperclip className="size-3.5 text-ink-tertiary" aria-hidden />
                <span className="min-w-0 flex-1 truncate">{d.filename}</span>
                <Button size="icon-xs" variant="ghost" onClick={() => setDocs((l) => l.filter((x) => x.id !== d.id))} aria-label={`${t("claims.form.remove")} ${d.filename}`}>
                  <X />
                </Button>
              </li>
            ))}
          </ul>
        )}
        <FileUpload multiple onFiles={upload} busy={uploading} label={t("claims.form.upload")} hint={t("claims.form.documents_hint")} />
      </Field>

      {error && <ErrorState compact error={error} />}
      <div className="flex flex-col gap-2 border-t border-[color:var(--border-subtle)] pt-3">
        {!canFile ? (
          <p className="text-xs text-ink-tertiary">{t("claims.form.no_perm")}</p>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Button onClick={() => save(true)} disabled={!!busy || uploading}>{busy === "submit" ? t("state.saving") : t("claims.form.submit")}</Button>
              <Button variant="outline" onClick={() => save(false)} disabled={!!busy || uploading}>{busy === "draft" ? t("state.saving") : t("claims.form.save_draft")}</Button>
              {onCancel && <Button variant="ghost" onClick={onCancel} disabled={!!busy}>{t("action.cancel")}</Button>}
            </div>
            <p className="text-xs text-ink-tertiary">
              {touched && missing.length ? <span className="text-danger-fg">{t("claims.form.missing", { list: missing.join(", ") })}</span> : t("claims.form.submit_hint")}
            </p>
          </>
        )}
      </div>
    </div>
  );
}


function todayIso() {
  // Helsinki calendar day (sv-SE formats as yyyy-mm-dd)
  return new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Helsinki" });
}
