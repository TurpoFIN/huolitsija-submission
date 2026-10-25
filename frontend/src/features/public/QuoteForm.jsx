/**
 * Public quote form, used by the landing widget (variant="hero") and the /quote page (variant="bar").
 *
 * <QuoteForm value={form} onChange={setForm} onSubmit={(form) => …} busy variant="hero"|"bar" />
 * form shape: features/public/quoteQuery.js emptyForm(). Validates route + cargo on submit and keeps
 * every input on error. Desktop: one row; mobile: stacked.
 */
import React, { useId, useRef, useState } from "react";
import { ChevronDown, Package, Plus, X } from "lucide-react";
import LocationField from "@/features/quotes/LocationField";
import { Field, NumberInput } from "@/features/quotes/fields";
import { CONTAINER_TYPES, INCOTERMS, PACKAGE_TYPES, modeIcon } from "@/features/quotes/quoteFormat";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DateInput } from "@/components/ui/DateInput";
import { Checkbox } from "@/components/ui/checkbox";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/lib/i18n";
import { num } from "@/lib/format";
import { cn } from "@/lib/utils";
import { cargoProblem } from "./quoteQuery";

const MODES = ["any", "sea", "road", "air", "rail"];
const INSURANCE = ["NONE", "ICC_A", "ICC_B", "ICC_C"];

/** "2 × EUR pallet · 600 kg · 1,4 m³" | "1 × 40HC · 18 000 kg" | "" */
export function pubCargoSummary(c, t, locale) {
  if (!c) return "";
  const parts = [];
  if (c.load_type === "FCL") {
    const cs = (c.containers || []).filter((x) => x.type && x.count > 0);
    if (cs.length) parts.push(cs.map((x) => `${x.count} × ${x.type}`).join(", "));
  } else if (c.packages) {
    parts.push(`${num(c.packages, locale)} × ${t(`pub.pkg.${c.package_type || "other"}`)}`);
  }
  if (c.weight_kg) parts.push(`${num(c.weight_kg, locale, 1)} kg`);
  if (c.load_type !== "FCL" && c.volume_m3) parts.push(`${num(c.volume_m3, locale, 2)} m³`);
  if (c.dangerous_goods) parts.push(t("pub.cargo.dg_short"));
  return parts.join(" · ");
}

function Seg({ value, onChange, options, label }) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex h-8 w-full items-center rounded-full bg-secondary p-0.5">
      {options.map(([v, text]) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={value === v}
          onClick={() => onChange(v)}
          className={cn(
            "h-7 flex-1 rounded-full px-2 text-sm text-ink-secondary transition-colors hover:text-foreground",
            value === v && "surface-raised bg-surface font-medium text-foreground"
          )}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

function CargoPopover({ cargo, onChange, id, invalid, describedBy }) {
  const { t, locale } = useT();
  const [open, setOpen] = useState(false);
  const set = (patch) => onChange({ ...cargo, ...patch });
  const summary = pubCargoSummary(cargo, t, locale);
  const fcl = cargo.load_type === "FCL";
  const setRow = (i, patch) => set({ containers: cargo.containers.map((r, j) => (j === i ? { ...r, ...patch } : r)) });
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          id={id}
          type="button"
          aria-invalid={invalid ? "true" : undefined}
          aria-describedby={describedBy}
          className="surface-input flex h-8 w-full min-w-0 items-center gap-2 rounded-lg px-2.5 text-left text-md md:text-sm"
        >
          <Package className="size-4 shrink-0 text-ink-tertiary" aria-hidden />
          <span className={cn("min-w-0 flex-1 truncate tabular", !summary && "text-ink-tertiary")}>{summary || t("pub.cargo.add")}</span>
          <ChevronDown className="size-4 shrink-0 opacity-50" aria-hidden />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="surface-overlay w-[22rem] max-w-[calc(100vw-2rem)] rounded-xl border-0 p-3">
        <div className="flex flex-col gap-3">
          <Seg
            label={t("pub.w.cargo")}
            value={cargo.load_type}
            onChange={(v) => set({ load_type: v })}
            options={[
              ["loose", t("pub.cargo.loose")],
              ["FCL", t("pub.cargo.fcl")],
            ]}
          />
          {!fcl ? (
            <>
              <div className="grid grid-cols-[96px_1fr] gap-2">
                <Field label={t("pub.cargo.count")} htmlFor={`${id}-n`}>
                  <NumberInput id={`${id}-n`} integer min={1} value={cargo.packages} onChange={(v) => set({ packages: v })} />
                </Field>
                <Field label={t("pub.cargo.type")}>
                  <Select value={cargo.package_type || "pallet_eur"} onValueChange={(v) => set({ package_type: v })}>
                    <SelectTrigger aria-label={t("pub.cargo.type")}><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {PACKAGE_TYPES.map((p) => (
                        <SelectItem key={p} value={p}>{t(`pub.pkg.${p}`)}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Field label={t("pub.cargo.weight")} htmlFor={`${id}-w`}>
                  <NumberInput id={`${id}-w`} min={0} value={cargo.weight_kg} onChange={(v) => set({ weight_kg: v })} suffix="kg" />
                </Field>
                <Field label={`${t("pub.cargo.volume")} (${t("pub.w.optional")})`} htmlFor={`${id}-v`}>
                  <NumberInput id={`${id}-v`} min={0} value={cargo.volume_m3} onChange={(v) => set({ volume_m3: v })} suffix="m³" />
                </Field>
              </div>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox checked={cargo.stackable !== false} onCheckedChange={(v) => set({ stackable: !!v })} />
                {t("pub.cargo.stackable")}
              </label>
            </>
          ) : (
            <>
              <div className="flex flex-col gap-2">
                {cargo.containers.map((r, i) => (
                  <div key={i} className="grid grid-cols-[1fr_72px_28px] items-center gap-2">
                    <Select value={r.type} onValueChange={(v) => setRow(i, { type: v })}>
                      <SelectTrigger aria-label={t("pub.cargo.container")}><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {CONTAINER_TYPES.map((c) => (
                          <SelectItem key={c} value={c}>{c} · {t(`pub.ctr.${c}`)}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <NumberInput aria-label={t("pub.cargo.count")} integer min={1} value={r.count} onChange={(v) => setRow(i, { count: v })} />
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={t("pub.cargo.remove")}
                      disabled={cargo.containers.length === 1}
                      onClick={() => set({ containers: cargo.containers.filter((_, j) => j !== i) })}
                    >
                      <X />
                    </Button>
                  </div>
                ))}
                <Button
                  variant="ghost"
                  size="sm"
                  className="self-start"
                  onClick={() => set({ containers: [...cargo.containers, { type: "20GP", count: 1 }] })}
                >
                  <Plus /> {t("pub.cargo.add_container")}
                </Button>
              </div>
              <Field label={`${t("pub.cargo.weight")} (${t("pub.w.optional")})`} htmlFor={`${id}-wf`} hint={t("pub.cargo.weight_fcl_hint")}>
                <NumberInput id={`${id}-wf`} min={0} value={cargo.weight_kg} onChange={(v) => set({ weight_kg: v })} suffix="kg" />
              </Field>
            </>
          )}
          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={!!cargo.dangerous_goods} onCheckedChange={(v) => set({ dangerous_goods: !!v })} />
            {t("pub.cargo.dg")}
          </label>
          <div className="flex justify-end border-t border-[color:var(--border-subtle)] pt-3">
            <Button size="sm" onClick={() => setOpen(false)}>{t("pub.cargo.done")}</Button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export default function QuoteForm({ value, onChange, onSubmit, busy, variant = "bar", className }) {
  const { t } = useT();
  const uid = useId().replace(/:/g, "");
  const [errors, setErrors] = useState({});
  const [more, setMore] = useState(false);
  const formRef = useRef(null);
  const f = value;
  const set = (patch) => {
    onChange({ ...f, ...patch });
    const cleared = { ...errors };
    Object.keys(patch).forEach((k) => delete cleared[k]);
    if (Object.keys(cleared).length !== Object.keys(errors).length) setErrors(cleared);
  };
  const bar = variant === "bar";
  const ids = { o: `${uid}-o`, d: `${uid}-d`, c: `${uid}-c`, r: `${uid}-r`, ip: `${uid}-ip` };

  const submit = (e) => {
    e.preventDefault();
    const errs = {};
    if (!f.origin) errs.origin = t("pub.w.need_origin");
    if (!f.destination) errs.destination = t("pub.w.need_destination");
    const cp = cargoProblem(f.cargo);
    if (cp) errs.cargo = t(cp);
    setErrors(errs);
    if (Object.keys(errs).length) {
      const first = errs.origin ? ids.o : errs.destination ? ids.d : ids.c;
      formRef.current?.querySelector(`#${CSS.escape(first)}`)?.focus();
      return;
    }
    onSubmit(f);
  };

  const grid = bar
    ? "lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_112px_minmax(0,164px)_132px_128px_auto]"
    : "lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_144px_minmax(0,220px)_150px_auto]";

  return (
    <form ref={formRef} onSubmit={submit} noValidate aria-label={t("pub.w.label")} className={cn("flex flex-col gap-2", className)}>
      <div className={cn("grid grid-cols-2 items-start gap-x-2 gap-y-2.5", grid)}>
        <Field className="col-span-2 lg:col-span-1" htmlFor={ids.o} error={errors.origin}>
          <LocationField
            id={ids.o}
            label={t("pub.w.origin")}
            source="public"
            mode={f.mode}
            value={f.origin}
            onChange={(v) => set({ origin: v })}
            placeholder={t("pub.w.place_ph")}
            invalid={!!errors.origin}
            aria-describedby={errors.origin ? `${ids.o}-err` : undefined}
          />
        </Field>
        <Field className="col-span-2 lg:col-span-1" htmlFor={ids.d} error={errors.destination}>
          <LocationField
            id={ids.d}
            label={t("pub.w.destination")}
            source="public"
            mode={f.mode}
            value={f.destination}
            onChange={(v) => set({ destination: v })}
            placeholder={t("pub.w.place_ph")}
            invalid={!!errors.destination}
            aria-describedby={errors.destination ? `${ids.d}-err` : undefined}
          />
        </Field>
        <Field label={t("pub.w.mode")}>
          <Select value={f.mode || "any"} onValueChange={(v) => set({ mode: v === "any" ? null : v })}>
            <SelectTrigger aria-label={t("pub.w.mode")} className="[&>span]:flex [&>span]:items-center">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {MODES.map((m) => {
                const Icon = modeIcon(m === "any" ? null : m);
                return (
                  <SelectItem key={m} value={m}>
                    <span className="inline-flex items-center gap-2">
                      <Icon className="size-4 shrink-0 text-ink-tertiary" aria-hidden />
                      {t(`pub.mode.${m}`)}
                    </span>
                  </SelectItem>
                );
              })}
            </SelectContent>
          </Select>
        </Field>
        <Field className="order-last col-span-2 lg:order-none lg:col-span-1" label={t("pub.w.cargo")} htmlFor={ids.c} error={errors.cargo}>
          <CargoPopover
            id={ids.c}
            cargo={f.cargo}
            onChange={(c) => set({ cargo: c })}
            invalid={!!errors.cargo}
            describedBy={errors.cargo ? `${ids.c}-err` : undefined}
          />
        </Field>
        <Field label={`${t("pub.w.ready")}`} htmlFor={ids.r}>
          <DateInput id={ids.r} min={today()} value={f.ready_date || ""} onChange={(iso) => set({ ready_date: iso })} />
        </Field>
        {bar && (
          <Field className="order-last col-span-2 sm:col-span-1 lg:order-none lg:col-span-1" label={t("pub.inco.label")}>
            <Select value={f.incoterm || "none"} onValueChange={(v) => set({ incoterm: v === "none" ? "" : v })}>
              <SelectTrigger aria-label={t("pub.inco.label")}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="none">{t("pub.inco.none")}</SelectItem>
                {INCOTERMS.map((i) => (
                  <SelectItem key={i} value={i}>
                    <span className="font-medium">{i}</span> <span className="text-ink-tertiary">{t(`pub.inco.${i}`)}</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        )}
        <div className="order-last col-span-2 flex flex-col lg:order-none lg:col-span-1">
          <span aria-hidden className="hidden min-h-5 lg:block">&nbsp;</span>
          <Button type="submit" variant="accent" className={cn("w-full lg:w-auto", !bar && "lg:px-4")} disabled={busy} aria-busy={busy || undefined}>
            {t("pub.w.submit")}
          </Button>
        </div>
      </div>
      {bar && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          {f.incoterm && (
            <div className="flex min-w-[260px] flex-1 items-center gap-2 sm:max-w-md">
              <label htmlFor={ids.ip} className="shrink-0 text-xs text-ink-secondary">{t("pub.inco.place")} · {f.incoterm}</label>
              <Input id={ids.ip} value={f.incoterm_place} placeholder={t("pub.inco.place_ph")} onChange={(e) => set({ incoterm_place: e.target.value })} />
            </div>
          )}
          <button
            type="button"
            className="inline-flex h-7 items-center gap-1 rounded-full text-xs text-ink-secondary hover:text-foreground"
            aria-expanded={more}
            onClick={() => setMore((m) => !m)}
          >
            <ChevronDown className={cn("size-3.5 transition-transform", more && "rotate-180")} aria-hidden />
            {t("pub.q.more")}
          </button>
          {more && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
              <label className="flex items-center gap-2 text-sm">
                <Checkbox checked={f.customs_clearance !== false} onCheckedChange={(v) => set({ customs_clearance: !!v })} />
                {t("pub.q.customs")}
              </label>
              <div className="flex items-center gap-2">
                <span className="text-xs text-ink-secondary">{t("pub.q.insurance")}</span>
                <Select value={f.insurance || "NONE"} onValueChange={(v) => set({ insurance: v })}>
                  <SelectTrigger className="w-44" aria-label={t("pub.q.insurance")}><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {INSURANCE.map((i) => (
                      <SelectItem key={i} value={i}>{t(`pub.ins.${i}`)}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
          )}
        </div>
      )}
    </form>
  );
}
