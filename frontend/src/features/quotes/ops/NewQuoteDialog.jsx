// Ops "New quote": compact request form for a customer company -> POST /api/quotes {..., company_id, send:false}.
// Customer list comes from GET /api/quotes/companies?q= (internal); if it is unavailable ops paste the company id.
import React, { useEffect, useMemo, useState } from "react";
import { Plus, X } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { DateInput } from "@/components/ui/DateInput";
import { Segmented } from "@/components/app/FilterBar";
import { ErrorState } from "@/components/app/states";
import { apiGet, apiPost } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { CONTAINER_TYPES, Field, INCOTERMS, MODES, NativeSelect, NumInput, useDebounced } from "./common";
import { LocationInput } from "./LocationInput";

const inDays = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); };

const blank = () => ({
  company: null, company_id: "", mode: null, origin: null, destination: null, ready_date: inDays(5),
  incoterm: "FCA", incoterm_place: "", load_type: "loose", packages: 1, package_type: "pallet_eur",
  weight_kg: null, volume_m3: null, containers: [{ type: "40HC", count: 1 }], description: "",
  dangerous_goods: false, un_number: "", dg_class: "", packing_group: "", customs_clearance: false, insurance: "NONE", notes: "",
});

function CompanyPicker({ value, onPick, idValue, onId, error }) {
  const { t } = useT();
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 200);
  const [state, setState] = useState({ loading: true, items: [], unavailable: false });
  useEffect(() => {
    let off = false;
    apiGet(`/quotes/companies${dq ? `?q=${encodeURIComponent(dq)}` : ""}`)
      .then((r) => { if (!off) setState({ loading: false, items: r?.items || [], unavailable: false }); })
      .catch((e) => { if (!off) setState({ loading: false, items: [], unavailable: e.status === 404 || e.status === 405 || e.status === 403 }); });
    return () => { off = true; };
  }, [dq]);

  if (state.unavailable) {
    return (
      <Field label={t("field.customer")} htmlFor="nq-cid" hint={t("oq.new.company_id_hint")} error={error}>
        <Input id="nq-cid" value={idValue} className="font-mono" placeholder="00000000-0000-…" onChange={(e) => onId(e.target.value.trim())} />
      </Field>
    );
  }
  if (value) {
    return (
      <Field label={t("field.customer")}>
        <div className="surface-input flex h-8 items-center gap-2 rounded-lg px-2.5 text-sm">
          <span className="min-w-0 flex-1 truncate">{value.name}</span>
          {value.business_id && <span className="font-mono text-xs text-ink-tertiary">{value.business_id}</span>}
          <button type="button" className="rounded-full p-0.5 hover:bg-hover" aria-label={t("action.clear")} onClick={() => onPick(null)}><X className="size-3.5" /></button>
        </div>
      </Field>
    );
  }
  return (
    <Field label={t("field.customer")} htmlFor="nq-co" error={error}>
      <Input id="nq-co" value={q} placeholder={t("oq.new.company_search")} onChange={(e) => setQ(e.target.value)} autoComplete="off" />
      <ul className="max-h-36 overflow-y-auto rounded-lg shadow-[inset_0_0_0_0.5px_var(--border-default)]">
        {state.loading && <li className="px-2.5 py-1.5 text-xs text-ink-tertiary">{t("state.loading")}</li>}
        {!state.loading && state.items.length === 0 && <li className="px-2.5 py-1.5 text-xs text-ink-tertiary">{t("state.no_results")}</li>}
        {state.items.map((c) => (
          <li key={c.id}>
            <button type="button" onClick={() => onPick(c)} className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-sm hover:bg-hover">
              <span className="min-w-0 flex-1 truncate">{c.name}</span>
              {c.kyc_status && c.kyc_status !== "approved" && <span className="text-xs text-warning-fg">{t("oq.new.kyc_not_approved")}</span>}
              {c.sanctions_status && !["clear", "not_screened"].includes(c.sanctions_status) && <span className="text-xs text-danger-fg">{t("oq.new.sanctions_flag")}</span>}
              <span className="font-mono text-xs text-ink-tertiary">{c.business_id || c.country || ""}</span>
            </button>
          </li>
        ))}
      </ul>
    </Field>
  );
}

export function NewQuoteDialog({ open, onOpenChange, onCreated }) {
  const { t } = useT();
  const [f, setF] = useState(blank);
  const [errs, setErrs] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => { if (open) { setErrs({}); setError(null); } }, [open]);
  const set = (p) => setF((x) => ({ ...x, ...p }));
  const companyId = f.company?.id || f.company_id;

  const modeOptions = useMemo(() => [{ value: null, label: t("oq.new.all_modes") }, ...MODES.map((m) => ({ value: m, label: tEnum(t, "mode", m) }))], [t]);

  const submit = async (e) => {
    e.preventDefault();
    const v = {};
    if (!companyId) v.company = t("oq.v.required");
    if (!f.origin) v.origin = t("oq.v.required");
    if (!f.destination) v.destination = t("oq.v.required");
    if (f.load_type === "loose" && !f.weight_kg) v.weight = t("oq.v.required");
    if (f.dangerous_goods && !f.un_number.trim()) v.un = t("oq.v.required");
    if (f.incoterm && !f.incoterm_place.trim()) v.incoterm_place = t("oq.v.incoterm_place");
    setErrs(v);
    if (Object.keys(v).length) return;
    const cargo = {
      load_type: f.load_type, description: f.description || null, weight_kg: f.weight_kg, dangerous_goods: f.dangerous_goods,
      ...(f.load_type === "loose"
        ? { packages: f.packages, package_type: f.package_type, volume_m3: f.volume_m3 }
        : { containers: f.containers.filter((c) => c.count > 0) }),
      ...(f.dangerous_goods ? { dg: { un_number: f.un_number.trim().toUpperCase(), class: f.dg_class || null, packing_group: f.packing_group || null } } : {}),
    };
    const body = {
      company_id: companyId, send: false, mode: f.mode, origin: f.origin, destination: f.destination, ready_date: f.ready_date,
      incoterm: f.incoterm || null, incoterm_place: f.incoterm_place || null, cargo,
      insurance: { cover: f.insurance }, customs_clearance: f.customs_clearance, notes: f.notes || null,
    };
    setBusy(true); setError(null);
    try {
      const r = await apiPost("/quotes", body);
      onOpenChange(false);
      setF(blank());
      onCreated?.(r.quote || r);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-24px)] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t("oq.q.new")}</DialogTitle>
          <DialogDescription className="text-xs">{t("oq.new.hint")}</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
          <CompanyPicker value={f.company} onPick={(c) => set({ company: c })} idValue={f.company_id} onId={(id) => set({ company_id: id })} error={errs.company} />
          <Field label={t("field.mode")}>
            <Segmented value={f.mode} onChange={(m) => set({ mode: m })} options={modeOptions} label={t("field.mode")} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("field.origin")} htmlFor="nq-o" error={errs.origin}>
              <LocationInput id="nq-o" value={f.origin} mode={f.mode} invalid={!!errs.origin} onChange={(v) => set({ origin: v })} placeholder={t("oq.new.loc_ph")} />
            </Field>
            <Field label={t("field.destination")} htmlFor="nq-d" error={errs.destination}>
              <LocationInput id="nq-d" value={f.destination} mode={f.mode} invalid={!!errs.destination} onChange={(v) => set({ destination: v })} placeholder={t("oq.new.loc_ph")} />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-[140px_110px_minmax(0,1fr)]">
            <Field label={t("oq.new.ready")} htmlFor="nq-rd">
              <DateInput id="nq-rd" min={new Date().toISOString().slice(0, 10)} value={f.ready_date} onChange={(iso) => set({ ready_date: iso })} />
            </Field>
            <Field label={t("field.incoterm")}>
              <NativeSelect value={f.incoterm} onChange={(v) => set({ incoterm: v })} aria-label={t("field.incoterm")} placeholder="—"
                options={INCOTERMS.map((i) => ({ value: i, label: i }))} />
            </Field>
            <Field label={t("oq.new.incoterm_place")} htmlFor="nq-ip" className="col-span-2 sm:col-span-1" error={errs.incoterm_place}>
              <Input id="nq-ip" value={f.incoterm_place} onChange={(e) => set({ incoterm_place: e.target.value })} placeholder={t("oq.new.incoterm_place_ph")} />
            </Field>
          </div>

          <Field label={t("oq.new.load")}>
            <Segmented value={f.load_type} onChange={(v) => set({ load_type: v })} label={t("oq.new.load")}
              options={[{ value: "loose", label: t("oq.new.loose") }, { value: "FCL", label: t("oq.new.fcl") }]} />
          </Field>
          {f.load_type === "loose" ? (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Field label={t("oq.new.packages")} htmlFor="nq-pk"><NumInput id="nq-pk" value={f.packages} onChange={(v) => set({ packages: v })} /></Field>
              <Field label={t("oq.new.package_type")}>
                <NativeSelect value={f.package_type} onChange={(v) => set({ package_type: v })} aria-label={t("oq.new.package_type")}
                  options={["pallet_eur", "pallet_fin", "carton", "crate", "other"].map((p) => ({ value: p, label: t(`oq.pkg.${p}`) }))} />
              </Field>
              <Field label={t("oq.new.weight")} htmlFor="nq-w" error={errs.weight}><NumInput id="nq-w" value={f.weight_kg} onChange={(v) => set({ weight_kg: v })} /></Field>
              <Field label={t("oq.new.volume")} htmlFor="nq-v"><NumInput id="nq-v" value={f.volume_m3} onChange={(v) => set({ volume_m3: v })} /></Field>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {f.containers.map((c, i) => (
                <div key={i} className="grid grid-cols-[1fr_90px_auto] gap-2">
                  <NativeSelect value={c.type} aria-label={t("oq.new.container")} onChange={(v) => set({ containers: f.containers.map((x, j) => (j === i ? { ...x, type: v } : x)) })}
                    options={CONTAINER_TYPES.map((ct) => ({ value: ct, label: ct }))} />
                  <NumInput aria-label={t("oq.new.count")} value={c.count} onChange={(v) => set({ containers: f.containers.map((x, j) => (j === i ? { ...x, count: v } : x)) })} />
                  <Button size="icon-sm" variant="ghost" aria-label={t("oq.remove_row")} disabled={f.containers.length === 1}
                    onClick={() => set({ containers: f.containers.filter((_, j) => j !== i) })}><X /></Button>
                </div>
              ))}
              <div className="flex items-end gap-3">
                <Button size="xs" variant="ghost" onClick={() => set({ containers: [...f.containers, { type: "20GP", count: 1 }] })}><Plus />{t("oq.new.add_container")}</Button>
                <Field label={t("oq.new.weight")} htmlFor="nq-wf" className="ml-auto w-36"><NumInput id="nq-wf" value={f.weight_kg} onChange={(v) => set({ weight_kg: v })} /></Field>
              </div>
            </div>
          )}
          <Field label={t("oq.new.description")} htmlFor="nq-desc">
            <Input id="nq-desc" value={f.description} onChange={(e) => set({ description: e.target.value })} placeholder={t("oq.new.description_ph")} />
          </Field>
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
            <label className="inline-flex items-center gap-2 text-sm">
              <Checkbox checked={f.dangerous_goods} onCheckedChange={(v) => set({ dangerous_goods: !!v })} />{t("oq.new.dg")}
            </label>
            <label className="inline-flex items-center gap-2 text-sm">
              <Checkbox checked={f.customs_clearance} onCheckedChange={(v) => set({ customs_clearance: !!v })} />{t("oq.new.customs")}
            </label>
            <label className="inline-flex items-center gap-2 text-sm">
              {t("oq.new.insurance")}
              <NativeSelect className="w-28" value={f.insurance} onChange={(v) => set({ insurance: v || "NONE" })} aria-label={t("oq.new.insurance")}
                options={["NONE", "ICC_A", "ICC_B", "ICC_C"].map((c) => ({ value: c, label: c === "NONE" ? t("oq.new.no_insurance") : c.replace("_", " ") }))} />
            </label>
          </div>
          {f.dangerous_goods && (
            <div className={cn("grid grid-cols-3 gap-3")}>
              <Field label={t("oq.dg.un")} htmlFor="nq-un" error={errs.un}><Input id="nq-un" value={f.un_number} className="uppercase" placeholder="UN1263" onChange={(e) => set({ un_number: e.target.value.toUpperCase() })} /></Field>
              <Field label={t("oq.dg.class")} htmlFor="nq-cl"><Input id="nq-cl" value={f.dg_class} placeholder="3" onChange={(e) => set({ dg_class: e.target.value })} /></Field>
              <Field label={t("oq.dg.pg")}>
                <NativeSelect value={f.packing_group} onChange={(v) => set({ packing_group: v || "" })} placeholder="—" aria-label={t("oq.dg.pg")}
                  options={["I", "II", "III"].map((p) => ({ value: p, label: p }))} />
              </Field>
            </div>
          )}
          {error && <ErrorState compact error={error} />}
          <DialogFooter className="gap-2">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>{t("action.cancel")}</Button>
            <Button type="submit" disabled={busy}>{busy ? t("oq.new.pricing") : t("oq.new.create")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
