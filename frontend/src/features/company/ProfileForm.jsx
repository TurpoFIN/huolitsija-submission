// Company details with Business ID lookup: YTJ prefills name, address and VAT, every field stays editable
// and shows where its value came from ("Source: YTJ 24 Oct 2026").
import React, { useMemo, useState } from "react";
import { Check, Download } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { apiPatch, apiPost } from "@/lib/api";
import { date } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { Field, SourceLabel, normaliseBusinessId, ytunnusValid } from "./common";

const FIELDS = ["business_id", "name", "street", "postal_code", "city", "country", "vat_id", "eori", "billing_email", "cbam_declarant_id"];
const ADDRESS = ["street", "postal_code", "city", "country"];

function fromCompany(c) {
  const a = c?.address || {};
  return {
    business_id: c?.business_id || "", name: c?.name || "", street: a.street || "", postal_code: a.postal_code || "",
    city: a.city || "", country: a.country || "FI", vat_id: c?.vat_id || "", eori: c?.eori || "",
    billing_email: c?.billing_email || "", cbam_declarant_id: c?.cbam_declarant_id || "",
    cbam_under_threshold: c?.cbam_under_threshold ?? false,
  };
}

function ytjValues(y) {
  if (!y) return null;
  const a = y.address || {};
  return { name: y.name, street: a.street, postal_code: a.postal_code, city: a.city, country: a.country || "FI" };
}

// Backend 400 messages -> the field they belong to.
function fieldForError(msg) {
  if (/Business ID/i.test(msg)) return "business_id";
  if (/VAT/i.test(msg)) return "vat_id";
  if (/EORI/i.test(msg)) return "eori";
  if (/postal/i.test(msg)) return "postal_code";
  if (/name is required/i.test(msg)) return "name";
  if (/e-?mail/i.test(msg)) return "billing_email";
  return null;
}

export function ProfileForm({ company, canEdit, onSaved }) {
  const { t, locale } = useT();
  const initial = useMemo(() => fromCompany(company), [company]);
  const [f, setF] = useState(initial);
  const [baseline, setBaseline] = useState(initial);
  const [errors, setErrors] = useState({});
  const [formError, setFormError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  const [lookup, setLookup] = useState(null); // last YTJ lookup result in this session
  const [looking, setLooking] = useState(false);
  const [lookupError, setLookupError] = useState(null);

  // Re-sync when the company changes underneath (e.g. after KYC run) and the form is untouched.
  const companyKey = company?.updated_at || company?.id;
  const [syncedKey, setSyncedKey] = useState(companyKey);
  if (companyKey !== syncedKey) {
    const dirtyNow = FIELDS.some((k) => f[k] !== baseline[k]) || f.cbam_under_threshold !== baseline.cbam_under_threshold;
    setSyncedKey(companyKey);
    if (!dirtyNow) {
      setF(initial);
      setBaseline(initial);
    }
  }

  const ytj = lookup?.found ? lookup : company?.ytj && company.ytj.name ? company.ytj : null;
  const ytjVals = ytjValues(ytj);
  const dirty = FIELDS.some((k) => f[k] !== baseline[k]) || f.cbam_under_threshold !== baseline.cbam_under_threshold;

  const set = (k) => (e) => {
    const v = e?.target ? e.target.value : e;
    setF((p) => ({ ...p, [k]: v }));
    setErrors((p) => ({ ...p, [k]: undefined }));
    setSavedAt(null);
  };

  const runLookup = async (bidRaw) => {
    const bid = normaliseBusinessId(bidRaw ?? f.business_id);
    if (!bid || !ytunnusValid(bid)) {
      setErrors((p) => ({ ...p, business_id: t("company.profile.lookup_bad_id") }));
      return;
    }
    setLooking(true);
    setLookupError(null);
    try {
      const r = await apiPost("/company/lookup", { business_id: bid });
      setLookup(r);
      if (r.found) {
        const a = r.address || {};
        setF((p) => ({
          ...p, business_id: r.business_id || bid, name: r.name || p.name, street: a.street || p.street,
          postal_code: a.postal_code || p.postal_code, city: a.city || p.city, country: a.country || "FI",
          vat_id: p.vat_id || (r.vat_registered ? `FI${(r.business_id || bid).replace("-", "")}` : p.vat_id),
        }));
        setErrors({});
      }
    } catch (e) {
      if (e.status === 400) setErrors((p) => ({ ...p, business_id: e.message }));
      else setLookupError(e);
    } finally {
      setLooking(false);
    }
  };

  const onBidBlur = () => {
    const bid = normaliseBusinessId(f.business_id);
    if (bid && bid !== f.business_id) setF((p) => ({ ...p, business_id: bid }));
    if (!canEdit || !bid) return;
    if (!ytunnusValid(bid)) {
      setErrors((p) => ({ ...p, business_id: t("company.profile.lookup_bad_id") }));
      return;
    }
    if (bid !== baseline.business_id && lookup?.business_id !== bid) runLookup(bid);
  };

  const save = async (e) => {
    e.preventDefault();
    const patch = {};
    for (const k of ["business_id", "name", "vat_id", "eori", "billing_email", "cbam_declarant_id"]) {
      if (f[k] !== baseline[k]) patch[k] = f[k].trim() === "" ? null : f[k].trim();
    }
    if (patch.business_id) patch.business_id = normaliseBusinessId(patch.business_id) || patch.business_id;
    if (f.cbam_under_threshold !== baseline.cbam_under_threshold) patch.cbam_under_threshold = f.cbam_under_threshold;
    if (ADDRESS.some((k) => f[k] !== baseline[k])) {
      patch.address = Object.fromEntries(ADDRESS.map((k) => [k, f[k].trim() || null]));
    }
    const errs = {};
    if (patch.business_id && !ytunnusValid(patch.business_id)) errs.business_id = t("company.profile.lookup_bad_id");
    if ("name" in patch && !patch.name) errs.name = t("company.profile.required");
    if (Object.keys(errs).length) {
      setErrors(errs);
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      const res = await apiPatch("/company", patch);
      const next = fromCompany(res);
      setF(next);
      setBaseline(next);
      setSyncedKey(res.updated_at || res.id);
      setSavedAt(Date.now());
      toast.success(t("company.profile.saved"));
      onSaved?.(res);
    } catch (ex) {
      const k = fieldForError(ex.message || "");
      if (k) setErrors((p) => ({ ...p, [k]: ex.message }));
      else setFormError(ex);
    } finally {
      setSaving(false);
    }
  };

  // Provenance per field: YTJ value unchanged -> "Source: YTJ <date>", changed -> "Edited".
  const src = (k) => {
    if (!ytjVals || !ytjVals[k] || !f[k]) return null;
    if (String(ytjVals[k]).trim().toLowerCase() === String(f[k]).trim().toLowerCase()) {
      return <SourceLabel label={t("company.profile.source_ytj", { date: date(ytj.fetched_at, locale) })} url={ytj.url} />;
    }
    return <span className="text-xs text-ink-tertiary">{t("company.profile.edited")}</span>;
  };
  const dis = !canEdit || saving;

  return (
    <form onSubmit={save} className="flex flex-col gap-4" noValidate>
      {!canEdit && <p className="text-xs text-ink-tertiary">{t("company.profile.read_only")}</p>}
      <div className="grid gap-x-4 gap-y-3 sm:grid-cols-2">
        <Field label={t("company.profile.business_id")} htmlFor="cp-bid" error={errors.business_id} className="sm:col-span-2">
          <div className="flex gap-2">
            <Input id="cp-bid" value={f.business_id} onChange={set("business_id")} onBlur={onBidBlur} disabled={dis}
              placeholder="1234567-8" inputMode="numeric" autoComplete="off" className="font-mono sm:max-w-[180px]" aria-invalid={!!errors.business_id} />
            {canEdit && (
              <Button type="button" variant="outline" size="sm" className="h-8" disabled={looking || !f.business_id} onClick={() => runLookup()}>
                <Download className="size-3.5" /> {looking ? t("company.profile.looking") : t("company.profile.lookup")}
              </Button>
            )}
          </div>
        </Field>
        {(lookup || lookupError || ytj) && (
          <div className="sm:col-span-2 -mt-1" aria-live="polite">
            {lookupError && <ErrorState compact error={lookupError} onRetry={() => runLookup()} />}
            {lookup && !lookup.found && (
              <p className="text-sm text-warning-fg">{t("company.profile.lookup_not_found", { id: lookup.business_id })}</p>
            )}
            {ytj && !lookupError && !(lookup && !lookup.found) && (
              <div className="flex flex-col gap-1 rounded-lg bg-subtle px-3 py-2 text-sm">
                {lookup?.found && <p className="flex items-center gap-1.5"><Check className="size-3.5 text-success-fg" aria-hidden />{t("company.profile.lookup_found")}</p>}
                <p className="text-ink-secondary">
                  <span className="font-medium text-foreground">{ytj.name}</span>
                  {" · "}
                  {t("company.profile.ytj_summary", { form: ytj.company_form || "—", date: date(ytj.registration_date, locale), line: ytj.main_business_line || "—" })}
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  {typeof ytj.vat_registered === "boolean" && (
                    <span className="text-xs text-ink-secondary">{ytj.vat_registered ? t("company.profile.vat_registered") : t("company.profile.not_vat_registered")}</span>
                  )}
                  {(ytj.situations || []).map((s) => <StatusBadge key={s.type} size="sm" state="pending" label={`${s.label}${s.since ? ` · ${date(s.since, locale)}` : ""}`} />)}
                  <SourceLabel label={t("company.profile.source_ytj", { date: date(ytj.fetched_at, locale) })} url={ytj.url} />
                </div>
              </div>
            )}
          </div>
        )}
        <Field label={t("company.profile.name")} htmlFor="cp-name" error={errors.name} source={src("name")} className="sm:col-span-2">
          <Input id="cp-name" value={f.name} onChange={set("name")} disabled={dis} autoComplete="organization" />
        </Field>
        <Field label={t("company.profile.street")} htmlFor="cp-street" error={errors.street} source={src("street")} className="sm:col-span-2">
          <Input id="cp-street" value={f.street} onChange={set("street")} disabled={dis} autoComplete="street-address" />
        </Field>
        <div className="grid grid-cols-[110px_minmax(0,1fr)_72px] gap-x-3 sm:col-span-2">
          <Field label={t("company.profile.postal")} htmlFor="cp-postal" error={errors.postal_code}>
            <Input id="cp-postal" value={f.postal_code} onChange={set("postal_code")} disabled={dis} inputMode="numeric" autoComplete="postal-code" className="tabular" />
          </Field>
          <Field label={t("company.profile.city")} htmlFor="cp-city" error={errors.city}>
            <Input id="cp-city" value={f.city} onChange={set("city")} disabled={dis} autoComplete="address-level2" />
          </Field>
          <Field label={t("company.profile.country")} htmlFor="cp-country">
            <Input id="cp-country" value={f.country} onChange={(e) => set("country")(e.target.value.toUpperCase().slice(0, 2))} disabled={dis} maxLength={2} className="uppercase" autoComplete="country" />
          </Field>
        </div>
        <Field label={t("company.profile.vat")} htmlFor="cp-vat" error={errors.vat_id} hint={t("company.profile.vat_hint")}>
          <Input id="cp-vat" value={f.vat_id} onChange={set("vat_id")} disabled={dis} placeholder="FI12345678" className="font-mono" autoComplete="off" />
        </Field>
        <Field label={t("company.profile.eori")} htmlFor="cp-eori" error={errors.eori} hint={t("company.profile.eori_hint")}>
          <Input id="cp-eori" value={f.eori} onChange={set("eori")} disabled={dis} placeholder="FI1234567-8" className="font-mono" autoComplete="off" />
        </Field>
        <Field label={t("company.profile.billing_email")} htmlFor="cp-bill" error={errors.billing_email}>
          <Input id="cp-bill" type="email" value={f.billing_email} onChange={set("billing_email")} disabled={dis} autoComplete="email" />
        </Field>
        <div className="hidden sm:block" />
        <fieldset className="flex flex-col gap-2 sm:col-span-2">
          <legend className="mb-1 text-xs text-ink-secondary">{t("company.profile.cbam")}</legend>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox aria-label={t("company.profile.cbam_under")} checked={!!f.cbam_under_threshold} onCheckedChange={(v) => { setF((p) => ({ ...p, cbam_under_threshold: !!v })); setSavedAt(null); }} disabled={dis} />
            {t("company.profile.cbam_under")}
          </label>
          {!f.cbam_under_threshold && (
            <Field label={t("company.profile.cbam_id")} htmlFor="cp-cbam" hint={t("company.profile.cbam_hint")} className="sm:max-w-[50%]">
              <Input id="cp-cbam" value={f.cbam_declarant_id} onChange={set("cbam_declarant_id")} disabled={dis} className="font-mono" autoComplete="off" />
            </Field>
          )}
        </fieldset>
      </div>
      {formError && <ErrorState compact error={formError} />}
      {canEdit && (
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" size="sm" disabled={!dirty || saving}>{saving ? t("state.saving") : t("action.save")}</Button>
          {dirty && !saving && (
            <Button type="button" size="sm" variant="ghost" onClick={() => { setF(baseline); setErrors({}); setFormError(null); }}>{t("action.cancel")}</Button>
          )}
          <span className="text-xs text-ink-tertiary" aria-live="polite">
            {dirty ? t("company.profile.unsaved") : savedAt ? <span className="inline-flex items-center gap-1 text-success-fg"><Check className="size-3.5" />{t("company.profile.saved")}</span> : null}
          </span>
        </div>
      )}
    </form>
  );
}
