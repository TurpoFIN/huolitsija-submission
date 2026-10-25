// Add / edit a rate card in a right-hand sheet: every RateCard field incl. container costs, tiers,
// distance bands and surcharge rows. POST /api/rates, PATCH /api/rates/{id}, DELETE (archive) with confirm.
import React, { useEffect, useMemo, useState } from "react";
import { Archive, Plus, X } from "lucide-react";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DateInput } from "@/components/ui/DateInput";
import { Textarea } from "@/components/ui/textarea";
import { ErrorState } from "@/components/app/states";
import { apiDelete, apiPatch, apiPost } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { BASES, SERVICE_BASIS, SURCHARGE_BASES, CONTAINER_TYPES, Field, MODES, MoneyInput, NativeSelect, NumInput, RATE_SOURCES, SERVICES, SourcePill, isBlank } from "./common";
import { basisLabel } from "./RatesTable";

const iso = (d) => d.toISOString().slice(0, 10);
const addDays = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return iso(d); };

function endToForm(e) {
  e = e || {};
  const countries = Array.isArray(e.countries) && e.countries.length ? e.countries.join(", ") : e.country && !e.unlocode ? e.country : "";
  return { unlocode: e.unlocode || "", countries, name: e.name || "", radius_km: e.radius_km ?? null };
}

function endToPayload(f) {
  const out = {};
  const code = (f.unlocode || "").trim().toUpperCase();
  const countries = (f.countries || "").split(/[,\s/]+/).map((c) => c.trim().toUpperCase()).filter(Boolean);
  if (code) { out.unlocode = code; out.country = code.slice(0, 2); }
  else if (countries.length === 1) out.country = countries[0];
  else if (countries.length > 1) out.countries = countries;
  if (f.name && f.name.trim()) out.name = f.name.trim();
  if (!isBlank(f.radius_km)) out.radius_km = f.radius_km;
  return out;
}

function toForm(rate, defaultCarrierId) {
  if (!rate) {
    return {
      carrier_id: defaultCarrierId || "", mode: "sea", service: "FCL", service_name: "",
      origin: endToForm(null), destination: endToForm(null), via: "",
      basis: "PER_CONTAINER", unit_cost_cents: null, container_costs: {}, tiers: [], distance_bands: [],
      min_cost_cents: null, transit_days: null, departure_weekdays: [], surcharges: [],
      valid_from: iso(new Date()), valid_to: addDays(90), source: "indicative", notes: "", distance_km: null,
    };
  }
  return {
    carrier_id: rate.carrier_id || "", mode: rate.mode || "sea", service: rate.service || "", service_name: rate.service_name || "",
    origin: endToForm(rate.origin), destination: endToForm(rate.destination), via: (rate.via || []).join(", "),
    basis: rate.basis || "PER_SHIPMENT", unit_cost_cents: rate.unit_cost_cents ?? null, container_costs: { ...(rate.container_costs || {}) },
    tiers: (rate.tiers || []).map((x) => ({ ...x })), distance_bands: (rate.distance_bands || []).map((x) => ({ ...x })),
    min_cost_cents: rate.min_cost_cents ?? null, transit_days: rate.transit_days ?? null,
    departure_weekdays: [...(rate.departure_weekdays || [])], surcharges: (rate.surcharges || []).map((s) => ({ ...s, by_size: { ...(s.by_size || {}) } })),
    valid_from: (rate.valid_from || "").slice(0, 10), valid_to: (rate.valid_to || "").slice(0, 10), source: rate.source || "indicative",
    notes: rate.notes || "", distance_km: rate.distance_km ?? null,
  };
}

function toPayload(f) {
  const cc = Object.fromEntries(Object.entries(f.container_costs || {}).filter(([, v]) => !isBlank(v)));
  return {
    carrier_id: f.carrier_id, mode: f.mode, service: f.service || null, service_name: f.service_name || null,
    origin: endToPayload(f.origin), destination: endToPayload(f.destination),
    via: f.via.split(/[,\s]+/).map((v) => v.trim().toUpperCase()).filter(Boolean),
    basis: f.basis, unit_cost_cents: f.unit_cost_cents, container_costs: cc,
    tiers: f.tiers.filter((x) => !isBlank(x.unit_cost_cents)), // blank max = "and above"
    distance_bands: f.distance_bands.filter((x) => !isBlank(x.max_km) && !isBlank(x.factor)),
    min_cost_cents: f.min_cost_cents, transit_days: f.transit_days, departure_weekdays: [...f.departure_weekdays].sort(),
    surcharges: f.surcharges.filter((s) => s.code || s.label).map((s) => {
      const by = Object.fromEntries(Object.entries(s.by_size || {}).filter(([, v]) => !isBlank(v)));
      // Keep fields this form does not edit (e.g. min_cents) as they were.
      const { by_size: _b, pct: _p, unit_cost_cents: _u, ...rest } = s;
      const o = { ...rest, code: (s.code || "").trim().toUpperCase(), label: s.label || "", basis: s.basis || "PER_SHIPMENT" };
      if (!isBlank(s.pct)) o.pct = s.pct;
      else { o.unit_cost_cents = s.unit_cost_cents ?? null; if (Object.keys(by).length) o.by_size = by; }
      return o;
    }),
    valid_from: f.valid_from, valid_to: f.valid_to, source: f.source, currency: "EUR", notes: f.notes || null, distance_km: f.distance_km,
  };
}

function validate(f, t) {
  const e = {};
  if (!f.carrier_id) e.carrier_id = t("oq.v.required");
  const end = (x) => (x.unlocode || "").trim() || (x.countries || "").trim();
  if (!end(f.origin)) e.origin = t("oq.v.end");
  if (!end(f.destination)) e.destination = t("oq.v.end");
  const anyCc = Object.values(f.container_costs || {}).some((v) => !isBlank(v));
  if (isBlank(f.unit_cost_cents) && !anyCc && f.tiers.length === 0) e.price = t("oq.v.price");
  if (!f.valid_from) e.valid_from = t("oq.v.required");
  if (!f.valid_to) e.valid_to = t("oq.v.required");
  if (f.valid_from && f.valid_to && f.valid_to < f.valid_from) e.valid_to = t("oq.v.to_after_from");
  return e;
}

function GroupTitle({ children }) {
  return <h3 className="mb-2 mt-1 border-b border-[color:var(--border-subtle)] pb-1.5 text-xs font-medium text-ink-tertiary">{children}</h3>;
}

function EndFields({ label, value, onChange, error, idp }) {
  const { t } = useT();
  return (
    <fieldset className="grid grid-cols-2 gap-2 sm:grid-cols-[110px_110px_minmax(0,1fr)_90px]">
      <legend className="mb-1 text-xs font-medium text-ink-secondary">{label}</legend>
      <Field label={t("oq.rate.locode")} htmlFor={`${idp}-code`}>
        <Input id={`${idp}-code`} value={value.unlocode} maxLength={5} className="font-mono uppercase" placeholder="FIHEL"
          onChange={(e) => onChange({ ...value, unlocode: e.target.value.toUpperCase() })} />
      </Field>
      <Field label={t("oq.rate.countries")} htmlFor={`${idp}-cty`}>
        <Input id={`${idp}-cty`} value={value.countries} className="uppercase" placeholder="SE, NO" disabled={!!value.unlocode}
          onChange={(e) => onChange({ ...value, countries: e.target.value.toUpperCase() })} />
      </Field>
      <Field label={t("oq.rate.place_name")} htmlFor={`${idp}-name`}>
        <Input id={`${idp}-name`} value={value.name} onChange={(e) => onChange({ ...value, name: e.target.value })} />
      </Field>
      <Field label={t("oq.rate.radius")} htmlFor={`${idp}-rad`}>
        <NumInput id={`${idp}-rad`} value={value.radius_km} onChange={(v) => onChange({ ...value, radius_km: v })} />
      </Field>
      {error && <span className="col-span-full text-xs text-danger-fg">{error}</span>}
    </fieldset>
  );
}

function RemoveBtn({ onClick, label }) {
  return (
    <Button size="icon-sm" variant="ghost" aria-label={label} onClick={onClick} className="self-end">
      <X />
    </Button>
  );
}

/**
 * props: open, onOpenChange, rate (null = new), carriers [{id, name, modes}], defaultCarrierId, canWrite,
 * onSaved(rate) after create/update, onArchived(id).
 */
export function RateSheet({ open, onOpenChange, rate, carriers, defaultCarrierId, canWrite = true, onSaved, onArchived }) {
  const { t, locale } = useT();
  const [current, setCurrent] = useState(rate || null);
  const [form, setForm] = useState(() => toForm(rate, defaultCarrierId));
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [savedAt, setSavedAt] = useState(null);
  const [confirmArchive, setConfirmArchive] = useState(false);

  useEffect(() => {
    if (open) {
      setCurrent(rate || null);
      setForm(toForm(rate, defaultCarrierId));
      setErrors({}); setError(null); setSavedAt(null);
    }
  }, [open, rate, defaultCarrierId]);

  const set = (patch) => { setForm((f) => ({ ...f, ...patch })); setSavedAt(null); };
  const carrierOpts = useMemo(() => (carriers || []).map((c) => ({ value: c.id, label: `${c.name}${c.code ? ` (${c.code})` : ""}` })), [carriers]);
  const serviceOpts = (SERVICES[form.mode] || []).map((s) => ({ value: s, label: t(`oq.service.${s}`) }));
  const disabled = !canWrite || busy;

  const save = async () => {
    const e = validate(form, t);
    setErrors(e);
    if (Object.keys(e).length) return;
    setBusy(true); setError(null);
    try {
      const body = toPayload(form);
      const r = current?.id ? await apiPatch(`/rates/${current.id}`, body) : await apiPost("/rates", body);
      const saved = r.rate || r;
      setCurrent(saved);
      setForm(toForm(saved, defaultCarrierId));
      setSavedAt(new Date());
      onSaved?.(saved);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const archive = async () => {
    setBusy(true); setError(null);
    try {
      await apiDelete(`/rates/${current.id}`);
      setConfirmArchive(false);
      onArchived?.(current.id);
      onOpenChange(false);
    } catch (err) {
      setConfirmArchive(false);
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const rowsSet = (key, i, patch) => set({ [key]: form[key].map((x, j) => (j === i ? { ...x, ...patch } : x)) });
  const rowsDel = (key, i) => set({ [key]: form[key].filter((_, j) => j !== i) });
  const weekday = (d) => set({ departure_weekdays: form.departure_weekdays.includes(d) ? form.departure_weekdays.filter((x) => x !== d) : [...form.departure_weekdays, d] });
  const days = t("oq.weekdays_short").split(",");

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-[680px]">
        <SheetHeader className="border-b border-[color:var(--border-subtle)] px-5 py-4 text-left">
          <SheetTitle className="text-md font-medium">{current?.id ? t("oq.rate.edit_title") : t("oq.rate.new_title")}</SheetTitle>
          <SheetDescription className="flex flex-wrap items-center gap-2 text-xs text-ink-tertiary">
            {current?.id ? (
              <>
                <span>{current.carrier_name}</span>
                {current.source && <SourcePill source={current.source} />}
                {current.updated_at && <span>{t("oq.rate.updated_by", { when: new Date(current.updated_at).toLocaleString(locale === "fi" ? "fi-FI" : "en-GB", { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" }), who: current.updated_by?.name || "—" })}</span>}
              </>
            ) : t("oq.rate.new_hint")}
          </SheetDescription>
        </SheetHeader>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {!canWrite && <p className="mb-3 text-xs text-ink-tertiary">{t("oq.no_perm.rates")}</p>}
          <fieldset disabled={disabled} className="flex flex-col gap-5">
            <div>
              <GroupTitle>{t("oq.rate.g.service")}</GroupTitle>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Field label={t("field.carrier")} className="col-span-2" error={errors.carrier_id}>
                  <NativeSelect value={form.carrier_id} onChange={(v) => set({ carrier_id: v })} options={carrierOpts} placeholder={t("oq.choose")} aria-label={t("field.carrier")} />
                </Field>
                <Field label={t("field.mode")}>
                  <NativeSelect value={form.mode} aria-label={t("field.mode")}
                    onChange={(v) => { const sv = (SERVICES[v] || [])[0] || ""; set({ mode: v, service: sv, ...(current?.id ? {} : { basis: SERVICE_BASIS[sv] || form.basis }) }); }}
                    options={MODES.map((m) => ({ value: m, label: tEnum(t, "mode", m) }))} />
                </Field>
                <Field label={t("oq.rates.col.service")}>
                  <NativeSelect value={form.service} onChange={(v) => set({ service: v, ...(current?.id ? {} : { basis: SERVICE_BASIS[v] || form.basis }) })} options={serviceOpts} aria-label={t("oq.rates.col.service")} />
                </Field>
                <Field label={t("oq.rate.service_name")} className="col-span-2 sm:col-span-4" htmlFor="rs-sname">
                  <Input id="rs-sname" value={form.service_name} placeholder={t("oq.rate.service_name_ph")} onChange={(e) => set({ service_name: e.target.value })} />
                </Field>
              </div>
            </div>

            <div>
              <GroupTitle>{t("oq.rate.g.lane")}</GroupTitle>
              <div className="flex flex-col gap-3">
                <EndFields idp="rs-o" label={t("field.origin")} value={form.origin} error={errors.origin} onChange={(v) => set({ origin: v })} />
                <EndFields idp="rs-d" label={t("field.destination")} value={form.destination} error={errors.destination} onChange={(v) => set({ destination: v })} />
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <Field label={t("oq.rate.via")} htmlFor="rs-via" className="col-span-2" hint={t("oq.rate.via_hint")}>
                    <Input id="rs-via" value={form.via} className="font-mono uppercase" placeholder="DEHAM" onChange={(e) => set({ via: e.target.value.toUpperCase() })} />
                  </Field>
                  <Field label={t("oq.rate.distance")} htmlFor="rs-dist">
                    <NumInput id="rs-dist" value={form.distance_km} onChange={(v) => set({ distance_km: v })} />
                  </Field>
                  <Field label={t("oq.rates.col.transit")} htmlFor="rs-tt">
                    <NumInput id="rs-tt" value={form.transit_days} onChange={(v) => set({ transit_days: v })} />
                  </Field>
                </div>
                <div>
                  <span className="mb-1 block text-xs font-medium text-ink-secondary">{t("oq.rate.departures")}</span>
                  <div className="flex flex-wrap gap-1" role="group" aria-label={t("oq.rate.departures")}>
                    {days.map((d, i) => {
                      const on = form.departure_weekdays.includes(i);
                      return (
                        <button key={i} type="button" aria-pressed={on} onClick={() => weekday(i)}
                          className={cn("h-7 min-w-9 rounded-full px-2 text-xs font-medium shadow-[inset_0_0_0_0.5px_var(--border-default)]",
                            on ? "bg-primary text-primary-foreground" : "text-ink-secondary hover:bg-hover")}>
                          {d}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            </div>

            <div>
              <GroupTitle>{t("oq.rate.g.price")}</GroupTitle>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Field label={t("oq.rates.col.basis")}>
                  <NativeSelect value={form.basis} onChange={(v) => set({ basis: v })} aria-label={t("oq.rates.col.basis")}
                    options={BASES.map((b) => ({ value: b, label: basisLabel(t, b) }))} />
                </Field>
                <Field label={t("oq.rate.unit_cost")} htmlFor="rs-uc">
                  <MoneyInput id="rs-uc" cents={form.unit_cost_cents} onChange={(v) => set({ unit_cost_cents: v })} />
                </Field>
                <Field label={t("oq.rates.col.min")} htmlFor="rs-min">
                  <MoneyInput id="rs-min" cents={form.min_cost_cents} onChange={(v) => set({ min_cost_cents: v })} />
                </Field>
                <Field label={t("oq.rate.source")}>
                  <NativeSelect value={form.source} onChange={(v) => set({ source: v })} aria-label={t("oq.rate.source")}
                    options={[...RATE_SOURCES, ...(form.source && !RATE_SOURCES.includes(form.source) ? [form.source] : [])].map((s) => ({ value: s, label: t(`oq.source.${s}`) }))} />
                </Field>
              </div>
              {errors.price && <p className="mt-1 text-xs text-danger-fg">{errors.price}</p>}

              {(form.basis === "PER_CONTAINER" || Object.values(form.container_costs || {}).some((v) => !isBlank(v))) && (
              <div className="mt-3">
                <span className="mb-1 block text-xs font-medium text-ink-secondary">{t("oq.rate.container_costs")}</span>
                <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
                  {CONTAINER_TYPES.map((ct) => (
                    <Field key={ct} label={ct} htmlFor={`rs-cc-${ct}`}>
                      <MoneyInput id={`rs-cc-${ct}`} cents={form.container_costs[ct] ?? null}
                        onChange={(v) => set({ container_costs: { ...form.container_costs, [ct]: v } })} />
                    </Field>
                  ))}
                </div>
              </div>
              )}

              <div className="mt-3">
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-xs font-medium text-ink-secondary">{t("oq.rate.tiers")}</span>
                  <Button size="xs" variant="ghost" onClick={() => set({ tiers: [...form.tiers, { max_qty: null, unit_cost_cents: null }] })}><Plus />{t("oq.add_row")}</Button>
                </div>
                {form.tiers.length === 0 ? <p className="text-xs text-ink-tertiary">{t("oq.rate.tiers_none")}</p> : (<>
                  <div className="flex flex-col gap-2">
                    {form.tiers.map((x, i) => (
                      <div key={i} className="grid grid-cols-[1fr_1fr_auto] gap-2">
                        <Field label={i === 0 ? t("oq.rate.max_qty") : null}><NumInput aria-label={t("oq.rate.max_qty")} value={x.max_qty} onChange={(v) => rowsSet("tiers", i, { max_qty: v })} /></Field>
                        <Field label={i === 0 ? t("oq.rate.unit_cost") : null}><MoneyInput fractionCents aria-label={t("oq.rate.unit_cost")} cents={x.unit_cost_cents} onChange={(v) => rowsSet("tiers", i, { unit_cost_cents: v })} /></Field>
                        <RemoveBtn label={t("oq.remove_row")} onClick={() => rowsDel("tiers", i)} />
                      </div>
                    ))}
                  </div>
                  <p className="mt-1 text-xs text-ink-tertiary">{t("oq.rate.tiers_hint")}</p>
                </>)}
              </div>

              {(form.basis === "PER_KM" || form.distance_bands.length > 0) && (
                <div className="mt-3">
                  <div className="mb-1 flex items-center justify-between">
                    <span className="text-xs font-medium text-ink-secondary">{t("oq.rate.bands")}</span>
                    <Button size="xs" variant="ghost" onClick={() => set({ distance_bands: [...form.distance_bands, { max_km: null, factor: 1 }] })}><Plus />{t("oq.add_row")}</Button>
                  </div>
                  <div className="flex flex-col gap-2">
                    {form.distance_bands.map((x, i) => (
                      <div key={i} className="grid grid-cols-[1fr_1fr_auto] gap-2">
                        <Field label={i === 0 ? t("oq.rate.max_km") : null}><NumInput aria-label={t("oq.rate.max_km")} value={x.max_km} onChange={(v) => rowsSet("distance_bands", i, { max_km: v })} /></Field>
                        <Field label={i === 0 ? t("oq.rate.factor") : null}><NumInput aria-label={t("oq.rate.factor")} value={x.factor} onChange={(v) => rowsSet("distance_bands", i, { factor: v })} /></Field>
                        <RemoveBtn label={t("oq.remove_row")} onClick={() => rowsDel("distance_bands", i)} />
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <div>
              <GroupTitle>{t("oq.rates.col.surcharges")}</GroupTitle>
              {form.surcharges.length === 0 && <p className="mb-2 text-xs text-ink-tertiary">{t("oq.rate.surcharges_none")}</p>}
              <div className="flex flex-col gap-3">
                {form.surcharges.map((s, i) => (
                  <div key={i} className="grid grid-cols-2 gap-2 border-b border-[color:var(--border-subtle)] pb-3 sm:grid-cols-[90px_minmax(0,1fr)_150px_auto]">
                    <Field label={t("oq.line.code")}><Input aria-label={t("oq.line.code")} value={s.code || ""} className="font-mono uppercase" onChange={(e) => rowsSet("surcharges", i, { code: e.target.value.toUpperCase() })} /></Field>
                    <Field label={t("oq.line.description")}><Input aria-label={t("oq.line.description")} value={s.label || ""} onChange={(e) => rowsSet("surcharges", i, { label: e.target.value })} /></Field>
                    <Field label={t("oq.rates.col.basis")}>
                      <NativeSelect aria-label={t("oq.rates.col.basis")} value={s.basis || "PER_SHIPMENT"} onChange={(v) => rowsSet("surcharges", i, { basis: v })}
                        options={SURCHARGE_BASES.map((b) => ({ value: b, label: basisLabel(t, b) }))} />
                    </Field>
                    <RemoveBtn label={t("oq.remove_row")} onClick={() => rowsDel("surcharges", i)} />
                    <div className="col-span-full grid grid-cols-2 gap-2 sm:grid-cols-4">
                      <Field label={t("oq.rate.unit_cost")}><MoneyInput aria-label={t("oq.rate.unit_cost")} cents={s.unit_cost_cents ?? null} disabled={!isBlank(s.pct)} onChange={(v) => rowsSet("surcharges", i, { unit_cost_cents: v })} /></Field>
                      <Field label="20'"><MoneyInput aria-label="20'" cents={s.by_size?.["20"] ?? null} disabled={!isBlank(s.pct)} onChange={(v) => rowsSet("surcharges", i, { by_size: { ...(s.by_size || {}), 20: v } })} /></Field>
                      <Field label="40'"><MoneyInput aria-label="40'" cents={s.by_size?.["40"] ?? null} disabled={!isBlank(s.pct)} onChange={(v) => rowsSet("surcharges", i, { by_size: { ...(s.by_size || {}), 40: v } })} /></Field>
                      <Field label={t("oq.rate.pct")}><NumInput aria-label={t("oq.rate.pct")} value={s.pct ?? null} onChange={(v) => rowsSet("surcharges", i, { pct: v })} /></Field>
                    </div>
                  </div>
                ))}
              </div>
              <Button size="xs" variant="ghost" className="mt-2" onClick={() => set({ surcharges: [...form.surcharges, { code: "", label: "", basis: "PER_CONTAINER", unit_cost_cents: null, by_size: {}, pct: null }] })}>
                <Plus />{t("oq.rate.add_surcharge")}
              </Button>
            </div>

            <div>
              <GroupTitle>{t("oq.rate.g.validity")}</GroupTitle>
              <div className="grid grid-cols-2 gap-3">
                <Field label={t("oq.rate.valid_from")} htmlFor="rs-vf" error={errors.valid_from}>
                  <DateInput id="rs-vf" value={form.valid_from} onChange={(iso) => set({ valid_from: iso })} />
                </Field>
                <Field label={t("oq.rate.valid_to")} htmlFor="rs-vt" error={errors.valid_to}>
                  <DateInput id="rs-vt" min={form.valid_from || undefined} value={form.valid_to} onChange={(iso) => set({ valid_to: iso })} />
                </Field>
                <Field label={t("oq.rate.notes")} htmlFor="rs-notes" className="col-span-2">
                  <Textarea id="rs-notes" value={form.notes} className="min-h-[56px]" onChange={(e) => set({ notes: e.target.value })} />
                </Field>
              </div>
            </div>
          </fieldset>
        </div>

        <div className="flex flex-col gap-2 border-t border-[color:var(--border-subtle)] px-5 py-3">
          {error && <ErrorState compact error={error} />}
          {Object.keys(errors).length > 0 && <p className="text-xs text-danger-fg">{t("oq.v.fix_fields")}</p>}
          <div className="flex flex-wrap items-center gap-2">
            {current?.id && canWrite && (
              <Button variant="destructive" size="sm" disabled={busy} onClick={() => setConfirmArchive(true)}><Archive />{t("oq.rate.archive")}</Button>
            )}
            <span className="ml-auto text-xs text-ink-tertiary" aria-live="polite">
              {busy ? t("state.saving") : savedAt ? t("oq.saved_at", { time: savedAt.toLocaleTimeString(locale === "fi" ? "fi-FI" : "en-GB", { hour: "2-digit", minute: "2-digit" }) }) : ""}
            </span>
            <Button variant="ghost" onClick={() => onOpenChange(false)}>{t("action.close")}</Button>
            {canWrite && <Button disabled={busy} onClick={save}>{current?.id ? t("action.save") : t("oq.rate.create")}</Button>}
          </div>
        </div>

        <AlertDialog open={confirmArchive} onOpenChange={setConfirmArchive}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{t("oq.rate.archive_q")}</AlertDialogTitle>
              <AlertDialogDescription>{t("oq.rate.archive_body")}</AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>{t("action.cancel")}</AlertDialogCancel>
              <AlertDialogAction onClick={(e) => { e.preventDefault(); archive(); }} disabled={busy}>{t("oq.rate.archive")}</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </SheetContent>
    </Sheet>
  );
}
