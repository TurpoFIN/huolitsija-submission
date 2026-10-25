// Beneficial owners (> 25 %) and the authorised signatory. Customer-declared; ops reads them on the customer page.
import React, { useMemo, useState } from "react";
import { Check, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import DateInput from "@/components/ui/DateInput";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { ErrorState } from "@/components/app/states";
import { apiPatch } from "@/lib/api";
import { date, pct } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { Field } from "./common";

const todayIso = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

const CONTROL = ["ownership", "voting", "board", "other"];
let tmp = 0;
const blank = () => ({ _key: `new-${++tmp}`, name: "", birth_date: "", country: "FI", ownership_pct: "", control: "ownership", pep: false });
const toRows = (ubos) => (ubos || []).map((u) => ({ ...u, _key: u.id || `new-${++tmp}`, birth_date: u.birth_date || "",
  country: u.country || "", ownership_pct: u.ownership_pct ?? "", control: u.control || "ownership", pep: !!u.pep }));
const sig = (rows, none) => JSON.stringify([rows.map(({ _key, ...r }) => r), !!none]);

function UboRow({ r, i, onChange, onRemove, disabled, errors }) {
  const { t } = useT();
  const set = (k) => (e) => onChange({ ...r, [k]: e?.target ? e.target.value : e });
  const id = (k) => `ubo-${i}-${k}`;
  return (
    <li className="grid grid-cols-2 gap-x-3 gap-y-2 py-3 md:grid-cols-[minmax(0,1fr)_140px_64px_84px] md:items-end">
      <Field label={t("company.ubo.name")} htmlFor={id("name")} error={errors?.name} className="col-span-2 md:col-span-1">
        <Input id={id("name")} value={r.name} onChange={set("name")} disabled={disabled} autoComplete="off" />
      </Field>
      <Field label={t("company.ubo.birth_date")} htmlFor={id("bd")} error={errors?.birth_date}>
        <DateInput id={id("bd")} value={r.birth_date || ""} onChange={(iso) => set("birth_date")(iso)} disabled={disabled}
          max={todayIso()} yearNavigation aria-invalid={!!errors?.birth_date} />
      </Field>
      <Field label={t("company.ubo.country")} htmlFor={id("cc")}>
        <Input id={id("cc")} value={r.country} onChange={(e) => set("country")(e.target.value.toUpperCase().slice(0, 2))} disabled={disabled} maxLength={2} className="uppercase" />
      </Field>
      <Field label={t("company.ubo.ownership")} htmlFor={id("pct")} error={errors?.ownership_pct}>
        <Input id={id("pct")} value={r.ownership_pct} onChange={set("ownership_pct")} disabled={disabled} inputMode="decimal" className="tabular text-right" />
      </Field>
      <Field label={t("company.ubo.control")} htmlFor={id("ctl")} className="md:max-w-[240px]">
        <Select value={r.control} onValueChange={set("control")} disabled={disabled}>
          <SelectTrigger id={id("ctl")}><SelectValue /></SelectTrigger>
          <SelectContent>{CONTROL.map((c) => <SelectItem key={c} value={c}>{t(`company.ubo.control.${c}`)}</SelectItem>)}</SelectContent>
        </Select>
      </Field>
      <label className="flex h-8 items-center gap-2 text-sm md:col-span-2 md:self-end" title={t("company.ubo.pep_hint")}>
        <Checkbox aria-label={t("company.ubo.pep")} checked={r.pep} onCheckedChange={(v) => onChange({ ...r, pep: !!v })} disabled={disabled} />
        {t("company.ubo.pep")}
      </label>
      {!disabled && (
        <Button type="button" variant="ghost" size="icon-sm" className="justify-self-end md:self-end" onClick={onRemove}
          aria-label={t("company.ubo.remove", { name: r.name || "" })}>
          <Trash2 className="size-4" />
        </Button>
      )}
    </li>
  );
}

export function UboEditor({ company, canEdit, onSaved }) {
  const { t } = useT();
  const start = useMemo(() => ({ rows: toRows(company?.ubos), none: !!company?.ubo_none_declared }), [company?.ubos, company?.ubo_none_declared]);
  const [rows, setRows] = useState(start.rows);
  const [none, setNone] = useState(start.none);
  const [base, setBase] = useState(sig(start.rows, start.none));
  const [errors, setErrors] = useState({});
  const [err, setErr] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const dirty = sig(rows, none) !== base;

  const save = async () => {
    const errs = {};
    rows.forEach((r, i) => {
      const e = {};
      if (!r.name.trim()) e.name = t("company.profile.required");
      const p = r.ownership_pct === "" ? null : Number(String(r.ownership_pct).replace(",", "."));
      if (p !== null && (Number.isNaN(p) || p < 0 || p > 100)) e.ownership_pct = "0–100";
      if (Object.keys(e).length) errs[i] = e;
    });
    setErrors(errs);
    if (Object.keys(errs).length) return;
    setSaving(true);
    setErr(null);
    try {
      const ubos = rows.map((r) => ({
        ...(r.id ? { id: r.id } : {}), name: r.name.trim(), birth_date: r.birth_date || null, country: r.country || null,
        ownership_pct: r.ownership_pct === "" ? null : Number(String(r.ownership_pct).replace(",", ".")), control: r.control, pep: !!r.pep,
      }));
      const res = await apiPatch("/company", { ubos, ubo_none_declared: none });
      const next = toRows(res.ubos);
      setRows(next);
      setNone(!!res.ubo_none_declared);
      setBase(sig(next, !!res.ubo_none_declared));
      setSaved(true);
      toast.success(t("company.ubo.saved"));
      onSaved?.(res);
    } catch (e) {
      setErr(e);
    } finally {
      setSaving(false);
    }
  };

  if (!canEdit) return <UboList ubos={company?.ubos} noneDeclared={company?.ubo_none_declared} />;

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-ink-tertiary">{t("company.ubo.hint")}</p>
      {rows.length > 0 ? (
        <ul className="divide-y divide-[color:var(--border-subtle)]">
          {rows.map((r, i) => (
            <UboRow key={r._key} r={r} i={i} errors={errors[i]} disabled={saving}
              onChange={(nr) => { setRows((p) => p.map((x, j) => (j === i ? nr : x))); setSaved(false); }}
              onRemove={() => { setRows((p) => p.filter((_, j) => j !== i)); setSaved(false); }} />
          ))}
        </ul>
      ) : (
        !none && <p className="text-sm text-ink-tertiary">{t("company.ubo.empty")}</p>
      )}
      <div>
        <Button type="button" size="sm" variant="outline" onClick={() => { setRows((p) => [...p, blank()]); setSaved(false); }} disabled={saving}>
          <Plus className="size-3.5" /> {t("company.ubo.add")}
        </Button>
      </div>
      <label className="mt-1 flex items-start gap-2 text-sm">
        <Checkbox aria-label={t("company.ubo.none")} className="mt-0.5" checked={none} onCheckedChange={(v) => { setNone(!!v); setSaved(false); }} disabled={saving} />
        <span>
          {t("company.ubo.none")}
          <span className="block text-xs text-ink-tertiary">{t("company.ubo.none_hint")}</span>
        </span>
      </label>
      {err && <ErrorState compact error={err} />}
      <div className="flex items-center gap-3">
        <Button type="button" size="sm" onClick={save} disabled={!dirty || saving}>{saving ? t("state.saving") : t("company.ubo.save")}</Button>
        {dirty && !saving && <Button type="button" size="sm" variant="ghost" onClick={() => { setRows(toRows(company?.ubos)); setNone(!!company?.ubo_none_declared); setErrors({}); }}>{t("action.cancel")}</Button>}
        <span className="text-xs text-ink-tertiary" aria-live="polite">
          {dirty ? t("company.profile.unsaved") : saved ? <span className="inline-flex items-center gap-1 text-success-fg"><Check className="size-3.5" />{t("company.profile.saved")}</span> : null}
        </span>
      </div>
    </div>
  );
}

/** Read-only owners list (ops customer page, non-admin customer users). */
export function UboList({ ubos, noneDeclared }) {
  const { t, locale } = useT();
  return (
    <div className="flex flex-col gap-2">
      {ubos?.length ? (
        <ul className="divide-y divide-[color:var(--border-subtle)]">
          {ubos.map((u, i) => (
            <li key={u.id || i} className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 py-2">
              <span className="flex items-center gap-2 text-sm font-medium">
                {u.name}
                {u.pep && <Badge variant="outline" title={t("company.ubo.pep_hint")}>{t("company.ubo.pep_yes")}</Badge>}
              </span>
              <span className="text-xs tabular text-ink-secondary">
                {[u.ownership_pct !== null && u.ownership_pct !== undefined ? pct(u.ownership_pct, locale, 0) : null,
                  u.control ? t(`company.ubo.control.${u.control}`) : null, u.birth_date ? date(u.birth_date, locale) : null, u.country].filter(Boolean).join(" · ")}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        !noneDeclared && <p className="text-sm text-ink-tertiary">{t("company.ubo.empty")}</p>
      )}
      {noneDeclared && <p className="text-sm text-ink-secondary">{t("customer.ubo.none_declared")}</p>}
    </div>
  );
}

export function SignatoryForm({ company, canEdit, onSaved }) {
  const { t } = useT();
  const init = { name: company?.signatory?.name || "", title: company?.signatory?.title || "", email: company?.signatory?.email || "" };
  const [f, setF] = useState(init);
  const [base, setBase] = useState(init);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);
  const dirty = JSON.stringify(f) !== JSON.stringify(base);
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }));
  const save = async (e) => {
    e.preventDefault();
    setSaving(true);
    setErr(null);
    try {
      const body = { name: f.name.trim() || null, title: f.title.trim() || null, email: f.email.trim() || null };
      const res = await apiPatch("/company", { signatory: body });
      const next = { name: res.signatory?.name || "", title: res.signatory?.title || "", email: res.signatory?.email || "" };
      setF(next);
      setBase(next);
      toast.success(t("company.signatory.saved"));
      onSaved?.(res);
    } catch (ex) {
      setErr(ex);
    } finally {
      setSaving(false);
    }
  };
  return (
    <form onSubmit={save} className="flex flex-col gap-3" noValidate>
      <p className="text-xs text-ink-tertiary">{t("company.signatory.hint")}</p>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label={t("company.signatory.name")} htmlFor="sg-name"><Input id="sg-name" value={f.name} onChange={set("name")} disabled={!canEdit || saving} autoComplete="name" /></Field>
        <Field label={t("company.signatory.role")} htmlFor="sg-title"><Input id="sg-title" value={f.title} onChange={set("title")} disabled={!canEdit || saving} autoComplete="organization-title" /></Field>
        <Field label={t("company.signatory.email")} htmlFor="sg-email"><Input id="sg-email" type="email" value={f.email} onChange={set("email")} disabled={!canEdit || saving} autoComplete="email" /></Field>
      </div>
      {err && <ErrorState compact error={err} />}
      {canEdit && (
        <div className="flex items-center gap-3">
          <Button type="submit" size="sm" disabled={!dirty || saving}>{saving ? t("state.saving") : t("company.signatory.save")}</Button>
          {dirty && !saving && <span className="text-xs text-ink-tertiary">{t("company.profile.unsaved")}</span>}
        </div>
      )}
    </form>
  );
}
