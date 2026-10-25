// Kateohjeet: margin rules the pricing agent applies. Decision: is each option priced by the rule we intend?
import React, { useMemo, useState } from "react";
import { toast } from "sonner";
import { FlaskConical, Info, Pencil, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Section } from "@/components/app/layout";
import { EmptyState } from "@/components/app/states";
import { useConfirmChange } from "@/components/app/ConfirmChange";
import { apiDelete, apiPatch, apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { money, num, pct } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { Field, MODES, MoneyInput, NativeSelect, NumInput, SERVICES, useIsNarrow } from "@/features/quotes/ops/common";
import { Chip, ComError, ComLoadable, Eur, ScrollTable, mutationError, useLabel } from "./common";
import { listOf, marginRule, simulation } from "./shapes";
import { useCustomerOptions } from "./TendersTab";

const SIZE_UNITS = ["kg", "teu", "ldm"];

function Scope({ r }) {
  const { t, locale } = useT();
  const label = useLabel();
  const chips = [
    r.mode && tEnum(t, "mode", r.mode),
    r.service,
    r.lane && <span className="font-mono">{r.lane}</span>,
    r.company_id && (r.company_name || t("com.rule.one_customer")),
    (r.size_min !== null || r.size_max !== null) && `${r.size_min !== null ? num(r.size_min, locale) : "0"}–${r.size_max !== null ? num(r.size_max, locale) : "∞"} ${label("unit", r.size_unit || "kg")}`,
  ].filter(Boolean);
  if (!chips.length) return <span className="text-xs text-ink-tertiary">{t("com.rule.everything")}</span>;
  return <span className="flex flex-wrap gap-1">{chips.map((c, i) => <Chip key={i}>{c}</Chip>)}</span>;
}

export default function MarginRulesTab() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("settings.write");
  const narrow = useIsNarrow();
  const q = useApi("/margin-rules");
  const [edit, setEdit] = useState(null);
  const { confirm, dialog } = useConfirmChange();
  const rows = useMemo(() => listOf(q.data, "rules").map(marginRule).sort((a, b) => Number(b.active) - Number(a.active) || (b.priority || 0) - (a.priority || 0)), [q.data]);
  const def = q.data && !Array.isArray(q.data) ? q.data.default || q.data.defaults || null : null;

  const toggle = async (r, active) => {
    try { await apiPatch(`/margin-rules/${r.id}`, { active }); q.reload(); } catch (e) { toast.error(mutationError(e, t, locale)); }
  };
  const remove = (r) => confirm(async () => {
    try { await apiDelete(`/margin-rules/${r.id}`); toast.success(t("com.rule.deleted")); q.reload(); } catch (e) { toast.error(mutationError(e, t, locale)); }
  }, t("com.rule.delete_q", { name: r.name || "" }));

  return (
    <div>
      <p className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-ink-secondary">
        {q.data && <span>{t("com.rule.active_n", { n: num(rows.filter((r) => r.active).length, locale) })}</span>}
        {def && <span>{t("com.rule.default", { pct: pct(def.margin_pct, locale), floor: money(def.floor_cents, locale) })}</span>}
      </p>
      <p className="mb-3 inline-flex items-center gap-1.5 text-xs text-ink-tertiary"><Info className="size-3.5" aria-hidden />{t("com.rule.precedence")}</p>
      <div className="grid gap-x-8 xl:grid-cols-[minmax(0,1fr)_400px]">
        <Section title={t("com.rule.rules")} actions={canWrite && <Button size="sm" onClick={() => setEdit({})}><Plus />{t("com.rule.new")}</Button>}>
          <ComLoadable q={q} perm="settings.write" isEmpty={() => rows.length === 0}
            empty={<EmptyState title={t("com.rule.empty")} body={t("com.rule.empty_body")} action={canWrite && <Button size="sm" onClick={() => setEdit({})}><Plus />{t("com.rule.new")}</Button>} />}>
            {() => (narrow ? (
              <ul className="divide-y divide-[color:var(--border-subtle)] border-y">
                {rows.map((r) => (
                  <li key={r.id} className="flex flex-col gap-1.5 px-1 py-2.5">
                    <span className="flex items-start justify-between gap-3">
                      <span className="min-w-0 text-sm font-medium">{r.name || t("com.rule.unnamed")}</span>
                      <span className="text-sm tabular">{pct(r.margin_pct, locale)}</span>
                    </span>
                    <Scope r={r} />
                    <span className="flex items-center gap-3 text-xs text-ink-secondary">
                      <span>{t("com.rule.floor")}: <Eur cents={r.floor_cents} /></span>
                      <span>{t("com.rule.priority")}: {num(r.priority, locale)}</span>
                      {canWrite && <Switch className="ml-auto" checked={r.active} onCheckedChange={(v) => toggle(r, v)} aria-label={t("com.rule.active")} />}
                      {canWrite && <Button size="icon-sm" variant="ghost" aria-label={t("action.edit")} onClick={() => setEdit(r)}><Pencil /></Button>}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <ScrollTable label={t("com.rule.rules")}>
                <table className="w-full text-sm">
                  <thead className="text-xs text-ink-tertiary"><tr className="border-b border-[color:var(--border-subtle)]">
                    <th className="px-2 py-2 text-left font-medium">{t("com.rule.name")}</th>
                    <th className="px-2 py-2 text-left font-medium">{t("com.rule.scope")}</th>
                    <th className="px-2 py-2 text-right font-medium">{t("com.rule.margin")}</th>
                    <th className="px-2 py-2 text-right font-medium">{t("com.rule.floor")}</th>
                    <th className="px-2 py-2 text-right font-medium">{t("com.rule.priority")}</th>
                    <th className="px-2 py-2 text-left font-medium">{t("com.rule.active")}</th>
                    <th className="px-2 py-2"><span className="sr-only">{t("com.actions")}</span></th>
                  </tr></thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.id} className={r.active ? "border-b border-[color:var(--border-subtle)]" : "border-b border-[color:var(--border-subtle)] text-ink-tertiary"}>
                        <td className="px-2 py-2 font-medium">{r.name || t("com.rule.unnamed")}</td>
                        <td className="px-2 py-2"><Scope r={r} /></td>
                        <td className="px-2 py-2 text-right tabular">{pct(r.margin_pct, locale)}</td>
                        <td className="px-2 py-2 text-right"><Eur cents={r.floor_cents} /></td>
                        <td className="px-2 py-2 text-right tabular">{num(r.priority, locale)}</td>
                        <td className="px-2 py-2">{canWrite ? <Switch checked={r.active} onCheckedChange={(v) => toggle(r, v)} aria-label={`${t("com.rule.active")}: ${r.name || ""}`} /> : (r.active ? t("com.yes") : t("com.no"))}</td>
                        <td className="px-2 py-2 text-right whitespace-nowrap">
                          {canWrite && <>
                            <Button size="icon-sm" variant="ghost" aria-label={`${t("action.edit")} ${r.name || ""}`} onClick={() => setEdit(r)}><Pencil /></Button>
                            <Button size="icon-sm" variant="ghost" aria-label={`${t("action.delete")} ${r.name || ""}`} onClick={() => remove(r)}><Trash2 /></Button>
                          </>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollTable>
            ))}
          </ComLoadable>
        </Section>
        <Section title={t("com.sim.title")}><Simulator rules={rows} /></Section>
      </div>
      {edit && <RuleDialog rule={edit.id ? edit : null} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); q.reload(); }} />}
      {dialog}
    </div>
  );
}

function RuleDialog({ rule, onClose, onSaved }) {
  const { t, locale } = useT();
  const label = useLabel();
  const customers = useCustomerOptions(true);
  const [f, setF] = useState(() => ({
    name: rule?.name || "", mode: rule?.mode || null, service: rule?.service || null, lane: rule?.lane || "", company_id: rule?.company_id || null,
    size_min: rule?.size_min ?? null, size_max: rule?.size_max ?? null, size_unit: rule?.size_unit || "kg",
    margin_pct: rule?.margin_pct ?? null, floor_cents: rule?.floor_cents ?? null, priority: rule?.priority ?? 0, active: rule ? rule.active : true,
  }));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const laneOk = !f.lane.trim() || /^[A-Z*]{2}([A-Z0-9]{3})?>[A-Z*]{2}([A-Z0-9]{3})?$/.test(f.lane.trim().toUpperCase());
  const save = async () => {
    if (f.margin_pct === null) { setErr(t("com.rule.need_margin")); return; }
    if (!laneOk) { setErr(t("com.rule.bad_lane")); return; }
    setBusy(true); setErr(null);
    const body = { ...f, name: f.name.trim() || null, lane: f.lane.trim().toUpperCase() || null,
      size_unit: f.size_min !== null || f.size_max !== null ? f.size_unit : null };
    try {
      if (rule?.id) await apiPatch(`/margin-rules/${rule.id}`, body); else await apiPost("/margin-rules", body);
      toast.success(t("action.saved"));
      onSaved();
    } catch (e) {
      setErr(mutationError(e, t, locale));
    } finally {
      setBusy(false);
    }
  };
  const services = f.mode ? SERVICES[f.mode] || [] : Object.values(SERVICES).flat();
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[calc(100dvh-24px)] max-w-xl overflow-y-auto">
        <DialogHeader><DialogTitle>{rule ? t("com.rule.edit") : t("com.rule.new")}</DialogTitle><DialogDescription>{t("com.rule.dialog_hint")}</DialogDescription></DialogHeader>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field className="sm:col-span-2" label={t("com.rule.name")} htmlFor="mr-name"><Input id="mr-name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
          <Field label={t("field.mode")} htmlFor="mr-mode"><NativeSelect id="mr-mode" value={f.mode} placeholder={t("com.rule.any")} onChange={(v) => setF({ ...f, mode: v, service: null })} options={MODES.map((m) => ({ value: m, label: tEnum(t, "mode", m) }))} /></Field>
          <Field label={t("com.rule.service")} htmlFor="mr-svc"><NativeSelect id="mr-svc" value={f.service} placeholder={t("com.rule.any")} onChange={(v) => setF({ ...f, service: v })} options={services.map((s) => ({ value: s, label: s }))} /></Field>
          <Field label={t("com.rule.lane")} htmlFor="mr-lane" hint={t("com.rule.lane_hint")} error={!laneOk ? t("com.rule.bad_lane") : null}>
            <Input id="mr-lane" value={f.lane} placeholder="FI>DE" onChange={(e) => setF({ ...f, lane: e.target.value })} className="font-mono uppercase" aria-invalid={!laneOk || undefined} />
          </Field>
          <Field label={t("field.customer")} htmlFor="mr-cust"><NativeSelect id="mr-cust" value={f.company_id} placeholder={t("com.rule.any")} onChange={(v) => setF({ ...f, company_id: v })} options={customers} /></Field>
          <Field label={t("com.rule.size")} className="sm:col-span-2">
            <div className="grid grid-cols-[1fr_1fr_100px] gap-2">
              <NumInput aria-label={t("com.rule.size_min")} placeholder={t("com.rule.size_min")} value={f.size_min} onChange={(v) => setF({ ...f, size_min: v })} />
              <NumInput aria-label={t("com.rule.size_max")} placeholder={t("com.rule.size_max")} value={f.size_max} onChange={(v) => setF({ ...f, size_max: v })} />
              <NativeSelect aria-label={t("com.rule.size_unit")} value={f.size_unit} onChange={(v) => setF({ ...f, size_unit: v || "kg" })} options={SIZE_UNITS.map((u) => ({ value: u, label: label("unit", u) }))} />
            </div>
          </Field>
          <Field label={t("com.rule.margin_pct")} htmlFor="mr-pct"><NumInput id="mr-pct" value={f.margin_pct} onChange={(v) => setF({ ...f, margin_pct: v })} /></Field>
          <Field label={t("com.rule.floor_eur")} htmlFor="mr-floor" hint={t("com.rule.floor_hint")}><MoneyInput id="mr-floor" cents={f.floor_cents} onChange={(v) => setF({ ...f, floor_cents: v })} /></Field>
          <Field label={t("com.rule.priority")} htmlFor="mr-prio" hint={t("com.rule.priority_hint")}><NumInput id="mr-prio" value={f.priority} onChange={(v) => setF({ ...f, priority: v ?? 0 })} /></Field>
          <label className="flex items-center gap-2 self-end pb-1.5 text-sm"><Switch checked={f.active} onCheckedChange={(v) => setF({ ...f, active: v })} />{t("com.rule.active")}</label>
        </div>
        {err && <p role="alert" className="text-sm text-danger-fg">{err}</p>}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>{t("action.cancel")}</Button>
          <Button onClick={save} disabled={busy}>{busy ? t("state.saving") : t("action.save")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Simulator({ rules }) {
  const { t, locale } = useT();
  const label = useLabel();
  const customers = useCustomerOptions(true);
  const [f, setF] = useState({ origin: "FIHEL", destination: "DEHAM", mode: null, weight_kg: 1200, volume_m3: 4, company_id: null });
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const end = (v) => { const c = v.trim().toUpperCase(); return c.length === 5 ? { unlocode: c, country: c.slice(0, 2) } : { country: c }; };
  const run = async (e) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const r = await apiPost("/margin-rules/simulate", {
        company_id: f.company_id, mode: f.mode,
        request: { origin: end(f.origin), destination: end(f.destination), mode: f.mode, modes: f.mode ? [f.mode] : undefined,
          cargo: { weight_kg: f.weight_kg, volume_m3: f.volume_m3 }, company_id: f.company_id },
      });
      setRes(r);
    } catch (x) {
      setErr(x);
    } finally {
      setBusy(false);
    }
  };
  const opts = res ? simulation(res) : [];
  const ruleName = (o) => o.rule_name || rules.find((r) => r.id === o.rule_id)?.name || null;
  return (
    <div>
      <form onSubmit={run} className="grid grid-cols-2 gap-2">
        <Field label={t("field.origin")} htmlFor="sim-o"><Input id="sim-o" value={f.origin} onChange={(e) => setF({ ...f, origin: e.target.value })} className="font-mono uppercase" /></Field>
        <Field label={t("field.destination")} htmlFor="sim-d"><Input id="sim-d" value={f.destination} onChange={(e) => setF({ ...f, destination: e.target.value })} className="font-mono uppercase" /></Field>
        <Field label={t("field.mode")} htmlFor="sim-m"><NativeSelect id="sim-m" value={f.mode} placeholder={t("com.rule.any")} onChange={(v) => setF({ ...f, mode: v })} options={MODES.map((m) => ({ value: m, label: tEnum(t, "mode", m) }))} /></Field>
        <Field label={t("com.sim.weight")} htmlFor="sim-w"><NumInput id="sim-w" value={f.weight_kg} onChange={(v) => setF({ ...f, weight_kg: v })} /></Field>
        <Field label={t("com.sim.volume")} htmlFor="sim-v"><NumInput id="sim-v" value={f.volume_m3} onChange={(v) => setF({ ...f, volume_m3: v })} /></Field>
        <Field label={t("field.customer")} htmlFor="sim-c"><NativeSelect id="sim-c" value={f.company_id} placeholder={t("com.rule.any")} onChange={(v) => setF({ ...f, company_id: v })} options={customers} /></Field>
        <div className="col-span-2"><Button type="submit" size="sm" disabled={busy}><FlaskConical />{busy ? t("com.sim.running") : t("com.sim.run")}</Button></div>
      </form>
      {err && <div className="mt-3"><ComError error={err} compact /></div>}
      {res && (opts.length === 0 ? <p className="mt-3 text-sm text-ink-tertiary">{t("com.sim.no_options")}</p> : (
        <ul className="mt-3 flex flex-col divide-y divide-[color:var(--border-subtle)] border-y" aria-live="polite">
          {opts.map((o) => (
            <li key={o.key} className="flex flex-col gap-1 py-2">
              <span className="flex items-baseline justify-between gap-3 text-sm">
                <span className="min-w-0 truncate font-medium">{o.label}</span>
                <Eur cents={o.sell_cents} />
              </span>
              <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-secondary">
                <span>{t("com.sim.cost")} <Eur cents={o.cost_cents} /></span>
                <span>{t("com.rule.margin")} {pct(o.margin_pct, locale)}</span>
                {o.floor_applied && <span className="text-warning-fg">{t("com.sim.floor_applied", { floor: money(o.floor_cents, locale) })}</span>}
              </span>
              <span className="text-xs">
                <span className="text-ink-tertiary">{t("com.sim.priced_by")} </span>
                {o.source === "rule" || o.rule_id ? <Chip>{ruleName(o) || o.rule_id}</Chip> : <Chip>{label("msrc", o.source || "default")}</Chip>}
              </span>
            </li>
          ))}
        </ul>
      ))}
    </div>
  );
}
