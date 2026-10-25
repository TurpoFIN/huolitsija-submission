// Add / edit carrier dialog. POST /api/carriers, PATCH /api/carriers/{id}.
// Integration *status* is computed by the backend from environment variables; only the type is edited here.
import React, { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { ErrorState } from "@/components/app/states";
import { apiPatch, apiPost } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Field, INTEGRATION_TYPES, MODES, ModeIcon, NativeSelect, NumInput } from "./common";

const list = (s) => (s || "").split(/[,\s]+/).map((x) => x.trim()).filter(Boolean);

function toForm(c) {
  c = c || {};
  return {
    name: c.name || "", code: c.code || "", modes: [...(c.modes || [])], country: c.country || "",
    contact_email: c.contact_email || "", website: c.website || "", integration_type: c.integration?.type || "manual",
    on_time_pct: c.on_time_pct ?? null, co2_factor_g_tkm: c.co2_factor_g_tkm ?? null,
    dg_classes: (c.dg_classes_accepted || []).join(", "), dg_un_blocked: (c.dg_un_blocked || []).join(", "), dg_note: c.dg_note || "",
    services_note: c.services_note || "", active: c.active !== false,
  };
}

export function CarrierDialog({ open, onOpenChange, carrier, onSaved }) {
  const { t } = useT();
  const [form, setForm] = useState(toForm(carrier));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [errs, setErrs] = useState({});
  useEffect(() => {
    if (open) { setForm(toForm(carrier)); setError(null); setErrs({}); }
  }, [open, carrier]);
  const set = (p) => setForm((f) => ({ ...f, ...p }));

  const submit = async (e) => {
    e.preventDefault();
    const v = {};
    if (!form.name.trim()) v.name = t("oq.v.required");
    if (!form.code.trim()) v.code = t("oq.v.required");
    if (form.modes.length === 0) v.modes = t("oq.v.modes");
    if (form.contact_email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(form.contact_email)) v.contact_email = t("oq.v.email");
    setErrs(v);
    if (Object.keys(v).length) return;
    setBusy(true); setError(null);
    const body = {
      name: form.name.trim(), code: form.code.trim().toUpperCase(), modes: form.modes, country: form.country.trim().toUpperCase() || null,
      contact_email: form.contact_email.trim() || null, website: form.website.trim() || null,
      integration: { ...(carrier?.integration ? { docs_url: carrier.integration.docs_url, note: carrier.integration.note } : {}), type: form.integration_type },
      on_time_pct: form.on_time_pct, co2_factor_g_tkm: form.co2_factor_g_tkm,
      dg_classes_accepted: list(form.dg_classes), dg_un_blocked: list(form.dg_un_blocked).map((u) => (u.toUpperCase().startsWith("UN") ? u.toUpperCase() : `UN${u}`)),
      dg_note: form.dg_note.trim() || null, services_note: form.services_note.trim() || null, active: form.active,
    };
    try {
      const r = carrier?.id ? await apiPatch(`/carriers/${carrier.id}`, body) : await apiPost("/carriers", body);
      onSaved?.(r.carrier || r);
      onOpenChange(false);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-24px)] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{carrier?.id ? t("oq.carrier.edit") : t("oq.carriers.add")}</DialogTitle>
          <DialogDescription className="text-xs">{t("oq.carrier.dialog_hint")}</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-[minmax(0,1fr)_120px_80px]">
            <Field label={t("field.name")} htmlFor="cd-name" className="col-span-2 sm:col-span-1" error={errs.name}>
              <Input id="cd-name" value={form.name} onChange={(e) => set({ name: e.target.value })} autoFocus />
            </Field>
            <Field label={t("oq.carrier.code")} htmlFor="cd-code" error={errs.code} hint={t("oq.carrier.code_hint")}>
              <Input id="cd-code" value={form.code} className="font-mono uppercase" onChange={(e) => set({ code: e.target.value.toUpperCase() })} />
            </Field>
            <Field label={t("oq.carrier.country")} htmlFor="cd-cty">
              <Input id="cd-cty" value={form.country} maxLength={2} className="uppercase" placeholder="FI" onChange={(e) => set({ country: e.target.value.toUpperCase() })} />
            </Field>
          </div>
          <Field label={t("oq.carrier.modes")} error={errs.modes}>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={t("oq.carrier.modes")}>
              {MODES.map((m) => {
                const on = form.modes.includes(m);
                return (
                  <button key={m} type="button" aria-pressed={on} onClick={() => set({ modes: on ? form.modes.filter((x) => x !== m) : [...form.modes, m] })}
                    className={cn("inline-flex h-8 items-center gap-1.5 rounded-full px-3 text-sm shadow-[inset_0_0_0_0.5px_var(--border-default)]",
                      on ? "bg-primary text-primary-foreground [&_svg]:text-primary-foreground" : "text-ink-secondary hover:bg-hover")}>
                    <ModeIcon mode={m} />{tEnum(t, "mode", m)}
                  </button>
                );
              })}
            </div>
          </Field>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label={t("oq.carrier.contact")} htmlFor="cd-mail" error={errs.contact_email}>
              <Input id="cd-mail" type="email" value={form.contact_email} onChange={(e) => set({ contact_email: e.target.value })} />
            </Field>
            <Field label={t("oq.carrier.website")} htmlFor="cd-web">
              <Input id="cd-web" value={form.website} placeholder="https://" onChange={(e) => set({ website: e.target.value })} />
            </Field>
            <Field label={t("oq.carrier.integration_type")} hint={t("oq.carrier.integration_type_hint")}>
              <NativeSelect value={form.integration_type} onChange={(v) => set({ integration_type: v })} aria-label={t("oq.carrier.integration_type")}
                options={INTEGRATION_TYPES.map((x) => ({ value: x, label: t(`oq.integration.${x}`) }))} />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label={t("oq.carrier.co2")} htmlFor="cd-co2"><NumInput id="cd-co2" value={form.co2_factor_g_tkm} onChange={(v) => set({ co2_factor_g_tkm: v })} /></Field>
              <Field label={t("oq.carrier.benchmark_on_time")} htmlFor="cd-ot"><NumInput id="cd-ot" value={form.on_time_pct} onChange={(v) => set({ on_time_pct: v })} /></Field>
            </div>
            <Field label={t("oq.carrier.dg_classes")} htmlFor="cd-dg" hint={t("oq.carrier.dg_classes_hint")}>
              <Input id="cd-dg" value={form.dg_classes} placeholder="2.1, 3, 8, 9" onChange={(e) => set({ dg_classes: e.target.value })} />
            </Field>
            <Field label={t("oq.carrier.dg_blocked")} htmlFor="cd-un">
              <Input id="cd-un" value={form.dg_un_blocked} placeholder="UN3480, UN3090" className="uppercase" onChange={(e) => set({ dg_un_blocked: e.target.value.toUpperCase() })} />
            </Field>
          </div>
          <Field label={t("oq.carrier.dg_note")} htmlFor="cd-dgn">
            <Input id="cd-dgn" value={form.dg_note} onChange={(e) => set({ dg_note: e.target.value })} />
          </Field>
          <Field label={t("oq.carrier.services_note")} htmlFor="cd-sn">
            <Textarea id="cd-sn" value={form.services_note} className="min-h-[56px]" onChange={(e) => set({ services_note: e.target.value })} />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <Switch checked={form.active} onCheckedChange={(v) => set({ active: v })} aria-label={t("oq.carrier.active")} />
            {t("oq.carrier.active")}
            <span className="text-xs text-ink-tertiary">{t("oq.carrier.active_hint")}</span>
          </label>
          {error && <ErrorState compact error={error} />}
          <DialogFooter className="gap-2">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>{t("action.cancel")}</Button>
            <Button type="submit" disabled={busy}>{busy ? t("state.saving") : carrier?.id ? t("action.save") : t("oq.carriers.add")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
