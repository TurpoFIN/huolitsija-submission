import { domainError } from "@/features/domainErrors";
import React, { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Section } from "@/components/app/layout";
import { Loadable } from "@/components/app/states";
import { useT } from "@/lib/i18n";
import { useAuth } from "@/lib/auth";
import { apiPatch, apiPost, useApi } from "@/lib/api";
import { dateTime, num } from "@/lib/format";
import { Link } from "react-router-dom";

function Field({ label, children }) {
  return <label className="grid gap-1.5 py-2 text-sm sm:grid-cols-[minmax(0,1fr)_260px] sm:items-center"><span>{label}</span>{children}</label>;
}
const bank = ["seller_business_id", "seller_vat_id", "seller_iban", "seller_bic"];
const numeric = ["partner_target_margin_pct", "partner_max_price_cents", "partner_sell_price_cents", "partner_daily_send_limit", "partner_per_partner_daily_limit", "partner_max_rounds",
  "partner_rfqs_per_lane", "partner_counter_anchor_pct", "autopilot_quote_max_cents", "autopilot_min_margin_pct"];
const whole = ["partner_daily_send_limit", "partner_per_partner_daily_limit", "partner_max_rounds", "partner_rfqs_per_lane"];
const parse = (v) => { if (String(v).trim() === "") throw new Error("Enter a value / Syötä arvo"); const n = Number(String(v).replace(",", ".")); if (!Number.isFinite(n)) throw new Error("Enter a number / Syötä numero"); return n; };

export default function DomainSettings() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const editable = can("settings.write");
  const q = useApi("/settings");
  const status = useApi("/autonomy/status");
  const [form, setForm] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const confirmTrigger = useRef(null);
  const [word, setWord] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (!q.data) return;
    const d = { ...q.data, lanes: q.data.partner_approved_lanes.join("\n") };
    numeric.forEach(k => { d[k] = String(k.endsWith("_cents") ? q.data[k] / 100 : q.data[k]).replace(".", locale === "fi" ? "," : "."); });
    setForm(d);
  }, [q.data, locale]);
  const change = (k,v) => {setForm(f=>({...f,[k]:v}));setSaved(false);};
  // The switch is disabled while busy, so focus goes back to it only after the reload has re-enabled it.
  const refocus = useRef(null);
  useEffect(() => { if (!busy && refocus.current) { document.getElementById(refocus.current)?.focus(); refocus.current = null; } }, [busy]);
  const toggle = async (name, enabled) => {
    setBusy(true); setError("");
    try { await apiPost(`/settings/autonomy/${name}`, {enabled, confirmation:word}); setConfirm(null);setWord("");await q.reload();await status.reload();window.dispatchEvent(new Event("huo.autonomy"));setSaved(true);refocus.current=name; }
    catch(e){setError(domainError(e,t));} finally{setBusy(false);}
  };
  const kill = async () => {
    setBusy(true); setError("");
    try { await apiPost("/partner-network/stop", {}); await q.reload(); await status.reload(); window.dispatchEvent(new Event("huo.autonomy")); setSaved(true); }
    catch(e){setError(domainError(e,t));} finally{setBusy(false);}
  };
  const save = async () => {
    setBusy(true);setError("");setSaved(false);
    try {
      const body = Object.fromEntries(bank.map(k=>[k,form[k]?.trim() || ""]));
      numeric.forEach(k=>{body[k]=k.endsWith("_cents")?Math.round(parse(form[k])*100):whole.includes(k)?Math.round(parse(form[k])):parse(form[k]);});
      body.partner_approved_lanes=form.lanes.split("\n").map(x=>x.trim()).filter(Boolean);
      await apiPatch("/settings",body);setSaved(true);await q.reload();
    }catch(e){setError(domainError(e,t));}finally{setBusy(false);}
  };
  return <Loadable q={q}>{()=>form && <>
    <Section title={t("domain.autonomy")}>
      {[['autopilot_enabled','autopilot'],['partner_network_enabled','network']].map(([key,name])=><div key={key} className="border-b border-[color:var(--border-subtle)] py-3">
        <div className="flex items-center justify-between gap-4"><label htmlFor={key} className="text-sm font-medium">{t(`domain.${name}`)} · {t(form[key]?"domain.on":"domain.off")}</label>
          <span className="flex items-center gap-3">{key==="partner_network_enabled"&&form[key]&&editable&&<Button size="sm" variant="destructive" disabled={busy} onClick={kill}>{t("src.stop")}</Button>}
          <Switch id={key} checked={!!form[key]} disabled={!editable||busy} onCheckedChange={v=>{if(v){confirmTrigger.current=document.getElementById(key);setConfirm({key,name});setWord("");}else toggle(key,false);}} /></span>
        </div><p className="mt-1 max-w-[65ch] text-sm text-ink-secondary">{t(`domain.${name}_hint`)}</p>
        {status.data?.changes?.[key+"_changed"] && <p className="mt-1 text-xs text-ink-tertiary">{status.data.changes[key+"_changed"].by.name} · {dateTime(status.data.changes[key+"_changed"].at,locale)}</p>}
      </div>)}
    </Section>
    <Section title={t("domain.bank")}>
      {bank.some(k=>!form[k]) && <p role="status" className="mb-2 rounded-lg p-3 text-sm status-pending">{t("domain.setup")}</p>}
      {bank.map(k=><Field key={k} label={t(`domain.${k}`)}><Input value={form[k] || ""} onChange={e=>change(k,e.target.value)} disabled={!editable} autoComplete="off" /></Field>)}
    </Section>
    <Section title={t("domain.policy")}>
      {numeric.map(k=><Field key={k} label={t(`domain.${k}`)}><Input inputMode="decimal" className="text-right tabular" value={form[k]} disabled={!editable} onChange={e=>change(k,e.target.value)} /></Field>)}
      <Field label={t("domain.lanes")}><Textarea aria-label={t("domain.lanes")} value={form.lanes} disabled={!editable} onChange={e=>change("lanes",e.target.value)} aria-describedby="lane-help" /></Field>
      <p id="lane-help" className="text-xs text-ink-tertiary">{t("domain.lanes_hint")}</p>
      <div className="mt-3 flex items-center gap-3">{editable && <Button disabled={busy} onClick={save}>{t(busy?"state.saving":"action.save")}</Button>}{saved&&<p role="status" className="text-sm">{t("domain.saved")}</p>}</div>
      {error && <p role="alert" className="mt-2 text-sm text-destructive">{error}</p>}
    </Section>
    <SourcingSummary />
    <Section title={t("domain.actions")}><Loadable q={status}>{d=><details><summary className="cursor-pointer text-sm">{t("domain.actions_log")} ({d.actions.length})</summary><ul className="mt-2 divide-y divide-[color:var(--border-subtle)]">{d.actions.slice(0,10).map(a=><li key={a.id} className="flex flex-wrap items-center gap-2 py-2 text-sm"><span>{t(`domain.action.${a.kind}`)}</span><span className="text-xs text-ink-tertiary">{dateTime(a.at,locale)}</span><span className="ml-auto text-xs text-ink-secondary">{a.reversal ? t("domain.saved") : a.reversible ? <Button variant="outline" size="sm" onClick={async()=>{try{await apiPost(`/autonomy/actions/${a.id}/reverse`,{});status.reload();}catch(e){setError(domainError(e,t));}}}>{t("domain.undo")}</Button>:t("domain.irreversible")}</span></li>)}</ul></details>}</Loadable></Section>
    <Dialog open={!!confirm} onOpenChange={o=>{if(!o)setConfirm(null);}}><DialogContent onCloseAutoFocus={e=>{e.preventDefault();confirmTrigger.current?.focus();}}>
      <DialogHeader><DialogTitle>{t("domain.confirm_title",{name:t(`domain.${confirm?.name || 'autopilot'}`)})}</DialogTitle><DialogDescription>{t("domain.confirm")}</DialogDescription></DialogHeader>
      <Input aria-label="enable" value={word} onChange={e=>setWord(e.target.value)} autoComplete="off" />
      {error&&<p role="alert" className="text-sm text-destructive">{error}</p>}
      <DialogFooter><Button variant="ghost" onClick={()=>setConfirm(null)}>{t("action.cancel")}</Button><Button disabled={word!=="enable"||busy} onClick={()=>toggle(confirm.key,true)}>{t("domain.enable")}</Button></DialogFooter>
    </DialogContent></Dialog>
  </>}</Loadable>;
}

function SourcingSummary(){
  const {t,locale}=useT();const q=useApi('/partner-network/status');
  return <Section title={t('src.settings.title')} actions={<Button asChild variant="outline" size="sm"><Link to="/ops/sourcing">{t('src.settings.open')}</Link></Button>}>
    <Loadable q={q}>{d=>{const r=d.rfqs||{};const open=(r.awaiting_reply||0)+(r.queued||0)+(r.discovered||0)+(r.clarification_sent||0);
      return <p className="text-sm text-ink-secondary">{t('src.settings.summary',{open:num(open,locale),accepted:num(d.accepted_rates,locale),sends:num(d.sends_today,locale),limit:num(d.limit,locale)})}</p>;}}</Loadable>
  </Section>;
}
