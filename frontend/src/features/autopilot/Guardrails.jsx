import React, { useEffect, useMemo, useRef, useState } from "react";
import { Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Section } from "@/components/app/layout";
import { Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { apiPatch, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { TypedConfirm } from "./TypedConfirm";
import { apError, centsToEur, decimalText, eurToCents, parseDecimal, parseWhole } from "./shared";

// [key, kind]. kind decides the unit, the parser and how the value is shown in the field.
const GROUPS = [
  { id: "limits", keys: [["guard_spend_per_day_cents", "eur"], ["guard_confidence_floor", "ratio"], ["autopilot_quote_max_cents", "eur"],
    ["autopilot_min_margin_pct", "pct"], ["guard_default_credit_limit_cents", "eur"], ["guard_claim_limit_cents", "eur"],
    ["insurance_claim_limit_cents", "eur"], ["rate_expiry_warning_days", "days"]] },
  { id: "owner", keys: [["owner_queue_sla_hours", "owner_hours"], ["owner_escalation_hours", "owner_hours"], ["owner_alert_email", "email"],
    ["customs_confirm_timeout_hours", "hours"]] },
  { id: "dunning", keys: [["dunning_enabled", "bool"], ["dunning_reminder1_days", "days"], ["dunning_reminder2_days", "days"],
    ["dunning_final_days", "days"], ["dunning_collection_days", "days"], ["dunning_fixed_compensation_cents", "eur"],
    ["dunning_reminder_fee_cents", "eur"]] },
];
const DUNNING_DAYS = ["dunning_reminder1_days", "dunning_reminder2_days", "dunning_final_days", "dunning_collection_days"];
const UNIT = { eur: "€", pct: "%", hours: "h", owner_hours: "h", days: "ap.unit.days", ratio: "", email: "", bool: "" };

function toText(kind, v, locale) {
  if (kind === "eur") return centsToEur(v, locale, true);
  if (kind === "ratio" || kind === "pct") return decimalText(v, locale);
  if (kind === "bool") return !!v;
  return v === null || v === undefined ? "" : String(v);
}

function fromText(kind, text, locale) {
  if (kind === "eur") return eurToCents(text, locale);
  if (kind === "ratio") { const n = parseDecimal(text); if (n < 0.5 || n > 1) throw new Error("ap.err.confidence"); return n; }
  if (kind === "pct") { const n = parseDecimal(text); if (n >= 100) throw new Error("ap.err.margin"); return n; }
  if (kind === "hours" || kind === "days") { const n = parseWhole(text); if (n > 365) throw new Error("ap.err.days"); return n; }
  if (kind === "owner_hours") { const n = parseWhole(text); if (n < 1 || n > 365) throw new Error("ap.err.owner_hours"); return n; }
  if (kind === "email") { const s = String(text || "").trim(); if (s && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(s)) throw new Error("ap.err.email"); return s; }
  return !!text;
}

function GuardGroup({ group, values, editable, onChanged }) {
  const { t, locale } = useT();
  const initial = useMemo(() => Object.fromEntries(group.keys.map(([k, kind]) => [k, toText(kind, values?.[k], locale)])), [group, values, locale]);
  const [form, setForm] = useState(initial);
  const [dirty, setDirty] = useState(false);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  // The status poll refreshes the values every 15 s; it never overwrites what the person is typing.
  useEffect(() => { if (!dirty) setForm(initial); }, [initial, dirty]);
  const change = (k, v) => { setForm((f) => ({ ...f, [k]: v })); setDirty(true); setSaved(false); setErrors((e) => ({ ...e, [k]: null })); };

  const save = async () => {
    const body = {};
    const errs = {};
    group.keys.forEach(([k, kind]) => {
      try {
        const v = fromText(kind, form[k], locale);
        if (v !== values?.[k]) body[k] = v;
      } catch (e) { errs[k] = t(e.message); }
    });
    setErrors(errs);
    if (Object.keys(errs).length) return;
    if (DUNNING_DAYS.some((k) => k in body)) DUNNING_DAYS.forEach((k) => { body[k] = fromText("days", form[k], locale); });
    if (!Object.keys(body).length) { setDirty(false); setSaved(true); return; }
    setBusy(true); setError("");
    try { await apiPatch("/autopilot/guardrails", body); await onChanged(); setDirty(false); setSaved(true); }
    catch (e) { setError(apError(e, t)); }
    finally { setBusy(false); }
  };

  return (
    <Section title={t(`ap.g.${group.id}`)}>
      <p className="mb-1 max-w-[72ch] text-sm text-ink-secondary">{t(`ap.g.${group.id}_hint`)}</p>
      <div className="flex flex-col">
        {group.keys.map(([k, kind]) => {
          const id = `ap-g-${k}`;
          const unit = UNIT[kind];
          return (
            <div key={k} className="flex flex-col gap-2 border-b border-[color:var(--border-subtle)] py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
              <div className="min-w-0">
                <label htmlFor={id} className="text-sm">{t(`ap.f.${k}`)}</label>
                <p id={`${id}-hint`} className="text-xs text-ink-tertiary">{t(`ap.f.${k}_hint`)}</p>
              </div>
              <div className="flex shrink-0 flex-col items-start gap-1 sm:items-end">
                {kind === "bool" ? (
                  <Switch id={id} className="data-[state=unchecked]:bg-[color:var(--text-disabled)]" checked={!!form[k]} disabled={!editable || busy} onCheckedChange={(v) => change(k, v)} aria-describedby={`${id}-hint`} />
                ) : (
                  <div className="flex items-center gap-2">
                    <Input id={id} value={form[k]} disabled={!editable || busy} onChange={(e) => change(k, e.target.value)}
                      inputMode={kind === "email" ? "email" : kind === "eur" || kind === "ratio" || kind === "pct" ? "decimal" : "numeric"}
                      type={kind === "email" ? "email" : "text"} autoComplete="off" aria-invalid={errors[k] ? "true" : undefined}
                      aria-describedby={`${id}-hint${errors[k] ? ` ${id}-err` : ""}`}
                      className={cn(kind === "email" ? "w-56 sm:w-64" : "w-36 text-right tabular")} />
                    {<span className="w-6 text-sm text-ink-tertiary">{unit && unit.startsWith("ap.") ? t(unit) : unit}</span>}
                  </div>
                )}
                {errors[k] && <p id={`${id}-err`} className="text-xs text-danger-fg">{errors[k]}</p>}
              </div>
            </div>
          );
        })}
      </div>
      {editable && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <Button disabled={busy || !dirty} onClick={save}>{busy ? t("state.saving") : t("action.save")}</Button>
          {dirty && !busy && <Button variant="ghost" onClick={() => { setForm(initial); setDirty(false); setErrors({}); setError(""); }}>{t("ap.g.undo")}</Button>}
          {saved && !dirty && <span role="status" className="inline-flex items-center gap-1 text-sm text-ink-secondary"><Check className="size-4" aria-hidden />{t("ap.saved")}</span>}
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-sm text-danger-fg">{error}</p>}
    </Section>
  );
}

function CustomsDelegation({ delegation, editable, onChanged }) {
  const { t, locale } = useT();
  const rec = delegation?.record;
  const on = !!delegation?.enabled;
  const [scope, setScope] = useState(delegation?.scope || "direct");
  const [name, setName] = useState(rec?.signer_name || "");
  const [title, setTitle] = useState(rec?.signer_title || "");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const trigger = useRef(null);
  const ready = name.trim().length >= 3 && title.trim().length >= 2;
  const send = async (enabled) => {
    setBusy(true); setError(""); setSaved("");
    try {
      await apiPost("/autopilot/customs-delegation", enabled
        ? { enabled: true, scope, confirmation: "enable", signer_name: name.trim(), signer_title: title.trim() }
        : { enabled: false, scope: delegation?.scope || "direct" });
      await onChanged(); setOpen(false); setSaved(t(enabled ? "ap.cd.saved_on" : "ap.cd.saved_off"));
    } catch (e) { setError(apError(e, t)); }
    finally { setBusy(false); }
  };
  return (
    <Section title={t("ap.cd.title")} id="ap-customs">
      <p className="mb-3 max-w-[72ch] text-sm text-ink-secondary">{t("ap.cd.why")}</p>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <StatusBadge state={on ? "success" : "expired"} label={t(on ? "ap.cd.on" : "ap.cd.off")} />
        {on && <span className="text-sm">{t(`ap.cd.scope.${delegation.scope}`)}</span>}
      </div>
      {rec?.at && (
        <p className="mb-3 text-xs text-ink-tertiary">
          {t(rec.enabled ? "ap.cd.granted_by" : "ap.cd.revoked_by", { signer: rec.signer_name || "—", role: rec.signer_title || "—", name: rec.by?.name || "—", time: dateTime(rec.at, locale) })}
        </p>
      )}
      {editable && (
        <div className="flex max-w-[640px] flex-col gap-3">
          <div className="flex flex-col gap-1.5 text-sm">
            <span id="ap-cd-scope">{t("ap.cd.scope")}</span>
            <Segmented className="self-start" label={t("ap.cd.scope")} value={scope} onChange={setScope} options={[
              { value: "direct", label: t("ap.cd.scope.direct") }, { value: "direct_and_indirect", label: t("ap.cd.scope.direct_and_indirect") },
            ]} />
            {scope === "direct_and_indirect" && <p className="text-xs text-ink-tertiary">{t("ap.cd.indirect_hint")}</p>}
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1.5 text-sm"><span>{t("ap.cd.signer_name")}</span><Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" /></label>
            <label className="flex flex-col gap-1.5 text-sm"><span>{t("ap.cd.signer_title")}</span><Input value={title} onChange={(e) => setTitle(e.target.value)} autoComplete="organization-title" /></label>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button ref={trigger} disabled={!ready || busy} onClick={() => { setError(""); setOpen(true); }}>{t(on ? "ap.cd.update" : "ap.cd.grant")}</Button>
            {on && <Button variant="outline" disabled={busy} onClick={() => send(false)}>{t("ap.cd.revoke")}</Button>}
            {!ready && <span className="text-xs text-ink-tertiary">{t("ap.cd.need_signer")}</span>}
            {saved && <span role="status" className="inline-flex items-center gap-1 text-sm text-ink-secondary"><Check className="size-4" aria-hidden />{saved}</span>}
          </div>
          {error && !open && <p role="alert" className="text-sm text-danger-fg">{error}</p>}
        </div>
      )}
      <TypedConfirm open={open} onOpenChange={setOpen} returnFocus={trigger} title={t("ap.cd.confirm_title")} body={t("ap.typed.body")}
        consequence={t("ap.cd.confirm_consequence", { scope: t(`ap.cd.scope.${scope}`), signer: name.trim(), role: title.trim() })}
        confirmLabel={t(on ? "ap.cd.update" : "ap.cd.grant")} onConfirm={() => send(true)} busy={busy} error={error} />
    </Section>
  );
}

/** Rajat: the guardrails agents work inside, the customs delegation and the dunning steps. */
export function Guardrails({ status, onChanged }) {
  const { can } = useAuth();
  const editable = can("settings.write");
  const { t } = useT();
  return (
    <div className="max-w-[1024px]">
      {!editable && <p className="mb-3 text-sm text-ink-tertiary">{t("ap.state.read_only")}</p>}
      <GuardGroup group={GROUPS[0]} values={status.guardrails} editable={editable} onChanged={onChanged} />
      <CustomsDelegation delegation={status.customs_delegation} editable={editable} onChanged={onChanged} />
      <GuardGroup group={GROUPS[1]} values={status.guardrails} editable={editable} onChanged={onChanged} />
      <GuardGroup group={GROUPS[2]} values={status.guardrails} editable={editable} onChanged={onChanged} />
    </div>
  );
}
