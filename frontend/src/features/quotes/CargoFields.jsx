/**
 * CargoFields — controlled editor for the Cargo object (api-contract "Cargo").
 *
 * Props (stable):
 *   value      Cargo  {load_type "loose"|"FCL", description, packages, package_type, weight_kg, volume_m3, stackable,
 *                      containers[{type, count}], dangerous_goods, dg{un_number, class, packing_group, proper_shipping_name,
 *                      emergency_phone}, value_cents}. Use emptyCargo() from ./quoteFormat for a blank one.
 *   onChange   (Cargo) => void   (always the whole object; empty numeric inputs are null, never 0)
 *   compact    bool  denser two-column layout for narrow places (public widget, side panels); description goes behind a disclosure
 *   errors     {[field]: message}  keys: load_type, packages, package_type, weight_kg, volume_m3, containers, value_cents,
 *              description, "dg.un_number", "dg.class", "dg.packing_group", "dg.proper_shipping_name", "dg.emergency_phone"
 *   dgDetails  bool  also ask proper shipping name + 24 h emergency phone (booking)
 *   aiFields   string[]  field keys filled by the AI parser; they get an AgentMark next to the label
 *   idPrefix   string  prefix for input ids (default "cargo")
 *   hideValue / hideDescription  bool  leave those optional inputs out
 */
import React, { useState } from "react";
import { ChevronDown, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Segmented } from "@/components/app/FilterBar";
import { AgentMark } from "@/components/app/StatusBadge";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Field, NumberInput } from "./fields";
import { CONTAINER_TYPES, DG_CLASSES, PACKAGE_TYPES, PACKING_GROUPS } from "./quoteFormat";

const NONE = "__none";

export default function CargoFields({ value, onChange, compact, errors = {}, dgDetails, aiFields = [], idPrefix = "cargo", hideValue, hideDescription }) {
  const { t } = useT();
  const c = value || {};
  const [moreOpen, setMoreOpen] = useState(!!c.description);
  const set = (patch) => onChange({ ...c, ...patch });
  const setDg = (patch) => set({ dg: { ...(c.dg || {}), ...patch } });
  const id = (k) => `${idPrefix}-${k.replace(".", "-")}`;
  const mark = (k) => (aiFields.includes(k) ? <AgentMark /> : null);
  const err = (k) => errors[k];
  const fcl = c.load_type === "FCL";
  const containers = Array.isArray(c.containers) ? c.containers : [];
  const grid = compact ? "grid grid-cols-2 gap-x-3 gap-y-3" : "grid grid-cols-2 gap-x-3 gap-y-3 lg:grid-cols-4";

  const setLoad = (lt) => {
    if (lt === c.load_type) return;
    if (lt === "FCL") set({ load_type: "FCL", containers: containers.length ? containers : [{ type: "40GP", count: 1 }] });
    else set({ load_type: "loose" });
  };
  const setContainer = (i, patch) => set({ containers: containers.map((x, j) => (j === i ? { ...x, ...patch } : x)) });

  const description = !hideDescription && (
    <Field label={t("quotes.cargo.description")} htmlFor={id("description")} error={err("description")} mark={mark("description")}
      className={compact ? "col-span-2" : "col-span-2 lg:col-span-2"}>
      <Input id={id("description")} value={c.description || ""} maxLength={300} placeholder={t("quotes.cargo.description_ph")}
        onChange={(e) => set({ description: e.target.value })} aria-invalid={err("description") ? "true" : undefined} />
    </Field>
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Segmented
          label={t("quotes.cargo.load_type")}
          value={fcl ? "FCL" : "loose"}
          onChange={setLoad}
          options={[
            { value: "loose", label: t("quotes.cargo.loose") },
            { value: "FCL", label: t("quotes.cargo.fcl") },
          ]}
        />
        {mark("load_type")}
        {err("load_type") && <span role="alert" className="text-xs text-danger-fg">{err("load_type")}</span>}
      </div>

      {!fcl ? (
        <div className={grid}>
          <Field label={t("quotes.cargo.packages")} htmlFor={id("packages")} error={err("packages")} mark={mark("packages")}>
            <NumberInput id={id("packages")} integer min={1} value={c.packages ?? null} onChange={(v) => set({ packages: v })}
              invalid={!!err("packages")} />
          </Field>
          <Field label={t("quotes.cargo.package_type")} htmlFor={id("package_type")} error={err("package_type")} mark={mark("package_type")}>
            <Select value={c.package_type || "pallet_eur"} onValueChange={(v) => set({ package_type: v })}>
              <SelectTrigger id={id("package_type")}><SelectValue /></SelectTrigger>
              <SelectContent>
                {PACKAGE_TYPES.map((p) => (
                  <SelectItem key={p} value={p}>{t(`quotes.pkg.${p}`)}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label={t("quotes.cargo.weight")} htmlFor={id("weight_kg")} error={err("weight_kg")} mark={mark("weight_kg")}>
            <NumberInput id={id("weight_kg")} value={c.weight_kg ?? null} onChange={(v) => set({ weight_kg: v })} suffix="kg"
              invalid={!!err("weight_kg")} />
          </Field>
          <Field label={t("quotes.cargo.volume")} htmlFor={id("volume_m3")} error={err("volume_m3")} mark={mark("volume_m3")}>
            <NumberInput id={id("volume_m3")} value={c.volume_m3 ?? null} onChange={(v) => set({ volume_m3: v })} suffix="m³"
              invalid={!!err("volume_m3")} />
          </Field>
          <label className={cn("flex items-center gap-2 text-sm", compact ? "col-span-2" : "col-span-2 lg:col-span-4")}>
            <Checkbox aria-label={t("quotes.cargo.stackable")} checked={!!c.stackable} onCheckedChange={(v) => set({ stackable: !!v })} />
            {t("quotes.cargo.stackable")}
            {mark("stackable")}
          </label>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-2" role="group" aria-label={t("quotes.cargo.containers")}>
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-ink-secondary">{t("quotes.cargo.containers")}</span>
              {mark("containers")}
            </div>
            {containers.map((row, i) => (
              <div key={i} className="flex items-center gap-2">
                <Select value={row.type || "40GP"} onValueChange={(v) => setContainer(i, { type: v })}>
                  <SelectTrigger className="w-full max-w-[14rem]" aria-label={t("quotes.cargo.container_type")}><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {CONTAINER_TYPES.map((ct) => (
                      <SelectItem key={ct} value={ct}>{t(`quotes.ctr.${ct}`)}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <span className="text-sm text-ink-tertiary" aria-hidden>×</span>
                <NumberInput integer min={1} value={row.count ?? null} onChange={(v) => setContainer(i, { count: v })} className="w-16 shrink-0"
                  aria-label={t("quotes.cargo.container_count")} />
                <Button variant="ghost" size="icon-sm" aria-label={t("action.delete")} disabled={containers.length <= 1}
                  onClick={() => set({ containers: containers.filter((_, j) => j !== i) })}>
                  <Trash2 />
                </Button>
              </div>
            ))}
            <div>
              <Button variant="ghost" size="sm" onClick={() => set({ containers: [...containers, { type: "40HC", count: 1 }] })}>
                <Plus /> {t("quotes.cargo.add_container")}
              </Button>
            </div>
            {err("containers") && <p role="alert" className="text-xs text-danger-fg">{err("containers")}</p>}
          </div>
          <div className={grid}>
            <Field label={t("quotes.cargo.weight")} htmlFor={id("weight_kg")} error={err("weight_kg")} mark={mark("weight_kg")}>
              <NumberInput id={id("weight_kg")} value={c.weight_kg ?? null} onChange={(v) => set({ weight_kg: v })} suffix="kg"
                invalid={!!err("weight_kg")} />
            </Field>
            <Field label={t("quotes.cargo.volume_opt")} htmlFor={id("volume_m3")} error={err("volume_m3")} mark={mark("volume_m3")}>
              <NumberInput id={id("volume_m3")} value={c.volume_m3 ?? null} onChange={(v) => set({ volume_m3: v })} suffix="m³" />
            </Field>
          </div>
        </div>
      )}

      <div className="flex flex-col gap-3">
        <label className="flex items-center gap-2 text-sm">
          <Checkbox
            aria-label={t("quotes.cargo.dg")} checked={!!c.dangerous_goods}
            onCheckedChange={(v) => set({ dangerous_goods: !!v, dg: v ? c.dg || { un_number: "", class: null, packing_group: null } : null })}
          />
          {t("quotes.cargo.dg")}
          {mark("dangerous_goods")}
        </label>
        {c.dangerous_goods && (
          <div className={cn(grid, "border-l-2 border-[color:var(--border-default)] pl-3")}>
            <Field label={t("quotes.dg.un_number")} htmlFor={id("dg.un_number")} error={err("dg.un_number")} mark={mark("dg.un_number")}>
              <Input id={id("dg.un_number")} value={c.dg?.un_number || ""} placeholder="UN1263" className="font-mono uppercase"
                aria-invalid={err("dg.un_number") ? "true" : undefined}
                onChange={(e) => setDg({ un_number: e.target.value.toUpperCase() })}
                onBlur={(e) => {
                  const v = e.target.value.trim().toUpperCase().replace(/\s/g, "");
                  if (/^\d{4}$/.test(v)) setDg({ un_number: `UN${v}` });
                }} />
            </Field>
            <Field label={t("quotes.dg.class")} htmlFor={id("dg.class")} error={err("dg.class")} mark={mark("dg.class")}>
              <Select value={c.dg?.class || NONE} onValueChange={(v) => setDg({ class: v === NONE ? null : v })}>
                <SelectTrigger id={id("dg.class")} aria-invalid={err("dg.class") ? "true" : undefined}><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>{t("quotes.dg.choose")}</SelectItem>
                  {DG_CLASSES.map((k) => (
                    <SelectItem key={k} value={k}>{t("quotes.dg.class_n", { n: k })}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field label={t("quotes.dg.packing_group")} htmlFor={id("dg.packing_group")} error={err("dg.packing_group")} mark={mark("dg.packing_group")}>
              <Select value={c.dg?.packing_group || NONE} onValueChange={(v) => setDg({ packing_group: v === NONE ? null : v })}>
                <SelectTrigger id={id("dg.packing_group")}><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>{t("quotes.dg.pg_none")}</SelectItem>
                  {PACKING_GROUPS.map((k) => (
                    <SelectItem key={k} value={k}>{`PG ${k}`}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            {dgDetails && (
              <>
                <Field label={t("quotes.dg.psn")} htmlFor={id("dg.proper_shipping_name")} error={err("dg.proper_shipping_name")}
                  className={compact ? "col-span-2" : "col-span-2 lg:col-span-2"}>
                  <Input id={id("dg.proper_shipping_name")} value={c.dg?.proper_shipping_name || ""} placeholder={t("quotes.dg.psn_ph")}
                    aria-invalid={err("dg.proper_shipping_name") ? "true" : undefined}
                    onChange={(e) => setDg({ proper_shipping_name: e.target.value })} />
                </Field>
                <Field label={t("quotes.dg.emergency_phone")} htmlFor={id("dg.emergency_phone")} error={err("dg.emergency_phone")}>
                  <Input id={id("dg.emergency_phone")} type="tel" value={c.dg?.emergency_phone || ""} placeholder="+358 …"
                    aria-invalid={err("dg.emergency_phone") ? "true" : undefined}
                    onChange={(e) => setDg({ emergency_phone: e.target.value })} />
                </Field>
              </>
            )}
          </div>
        )}
      </div>

      {(!hideValue || !hideDescription) && (
        <div className={grid}>
          {!hideValue && (
            <Field label={t("quotes.cargo.value")} htmlFor={id("value_cents")} error={err("value_cents")} mark={mark("value_cents")}
              hint={t("quotes.cargo.value_hint")}>
              <NumberInput id={id("value_cents")} suffix="EUR"
                value={c.value_cents === null || c.value_cents === undefined ? null : c.value_cents / 100}
                onChange={(v) => set({ value_cents: v === null ? null : Math.round(v * 100) })} invalid={!!err("value_cents")} />
            </Field>
          )}
          {!compact && description}
        </div>
      )}
      {compact && !hideDescription && (
        <div>
          {!moreOpen ? (
            <Button variant="ghost" size="xs" onClick={() => setMoreOpen(true)} aria-expanded={false}>
              <ChevronDown /> {t("quotes.cargo.add_description")}
            </Button>
          ) : (
            <div className="grid grid-cols-2 gap-3">{description}</div>
          )}
        </div>
      )}
    </div>
  );
}
