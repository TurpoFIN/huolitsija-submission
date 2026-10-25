import { domainError } from "@/features/domainErrors";
import React, { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Section } from "@/components/app/layout";
import { useT } from "@/lib/i18n";
import { useAuth } from "@/lib/auth";
import { apiPost } from "@/lib/api";
import { date, money } from "@/lib/format";

const cents = (s) => {if(!/^\d+(?:[.,]\d{1,2})?$/.test(s))throw new Error('Syötä summa kahdella desimaalilla / Enter an amount with up to two decimals');return Math.round(Number(s.replace(',','.'))*100);};

export function RecordedDecision({decision}){
  const {t,locale}=useT();if(!decision)return null;
  return <div className="mt-3 border-t border-[color:var(--border-subtle)] pt-3">
    <p className="text-sm font-medium">{t('domain.recorded')} · {date(decision.decision_date,locale)}</p><p className="mt-1 break-all font-mono text-xs">MRN {decision.mrn}</p>
    <dl className="mt-2 text-sm"><div className="flex justify-between gap-3 py-1"><dt>{t('domain.duty')}</dt><dd className="tabular">{money(decision.duty_cents,locale,{decimals:2})}</dd></div><div className="flex justify-between gap-3 py-1"><dt>{t('domain.vat')}</dt><dd className="tabular">{money(decision.vat_cents,locale,{decimals:2})}</dd></div><div className="flex justify-between gap-3 py-1"><dt>{t('domain.paid_by')}</dt><dd>{t(`domain.${decision.paid_by}`)}</dd></div></dl>
  </div>;
}

export default function CustomsDecision({decl,onSaved}){
  const {t}=useT();const {can}=useAuth();const [values,setValues]=useState({});const [decisionDate,setDate]=useState('');const [paidBy,setPaidBy]=useState('');const [error,setError]=useState('');const [busy,setBusy]=useState(false);
  const save=async()=>{setBusy(true);setError('');try{await apiPost(`/customs/${decl.id}/decision`,{mrn:decl.mrn,decision_date:decisionDate,paid_by:paidBy,items:decl.items.map(it=>({seq:it.seq,duty_cents:cents(values[it.seq]?.duty || ''),vat_cents:cents(values[it.seq]?.vat || '')}))});await onSaved();}catch(e){setError(domainError(e,t));}finally{setBusy(false);}};
  if(decl.customs_decision)return null;
  return <Section title={t('domain.decision')}>
    {decl.customs_decision?<RecordedDecision decision={decl.customs_decision}/>:<>
      <p className="mb-3 text-sm text-ink-secondary">{t('domain.decision_pending')}</p>
      {can('customs.file')&&['accepted','under_control','released','closed'].includes(decl.status)&&<>
        <p className="mb-3 text-xs text-ink-tertiary">{t('domain.decision_help')}</p>
        <div className="grid gap-3 sm:grid-cols-2"><label className="text-sm">{t('domain.decision_date')}<Input type="date" value={decisionDate} onChange={e=>setDate(e.target.value)}/></label><label className="text-sm">{t('domain.paid_by')}<select aria-label={t("domain.paid_by")} className="mt-1 h-8 w-full rounded-md border bg-background px-2" value={paidBy} onChange={e=>setPaidBy(e.target.value)}><option value="">{t('domain.select')}</option><option value="forwarder">{t('domain.forwarder')}</option><option value="importer">{t('domain.importer')}</option></select></label></div>
        <ul className="mt-3 divide-y divide-[color:var(--border-subtle)]">{decl.items.map(it=><li key={it.seq} className="py-3"><p className="mb-2 text-sm">{t('domain.item')} {it.seq} · {it.description}</p><div className="grid gap-3 sm:grid-cols-2">{['duty','vat'].map(k=><label key={k} className="text-xs text-ink-secondary">{t(`domain.${k}`)}<Input inputMode="decimal" value={values[it.seq]?.[k]??''} onChange={e=>setValues({...values,[it.seq]:{...values[it.seq],[k]:e.target.value}})}/></label>)}</div></li>)}</ul>
        <Button disabled={busy||!decisionDate||!paidBy} onClick={save}>{t(busy?'state.saving':'domain.record_decision')}</Button>
      </>}
      {error&&<p role="alert" className="mt-2 text-sm text-destructive">{error}</p>}
    </>}
  </Section>;
}

export function TradeDefencePanel({decl,patch}){
  const {t}=useT();const [values,setValues]=useState({});const [error,setError]=useState('');const [busy,setBusy]=useState(false);
  const flagged=decl.items.filter(it=>it.flags?.some(f=>f.kind==='trade_defence'));
  if(!flagged.length)return null;
  const save=async()=>{setBusy(true);setError('');try{await patch({items:flagged.map(it=>{const v=values[it.seq]||{};const code=v.code??it.taric_additional_codes?.join(',')??'';const reason=v.reason??it.trade_defence_clearance_reason??'';return {seq:it.seq,taric_additional_codes:code.split(',').map(x=>x.trim()).filter(Boolean),...(reason?{trade_defence_clearance_reason:reason}:{})};})},'trade-defence');}catch(e){setError(domainError(e,t));}finally{setBusy(false);}};
  return <Section title={t('domain.risk')}><p className="mb-2 text-sm text-ink-secondary">{t('domain.risk_hint')}</p>{flagged.map(it=><div key={it.seq} className="border-b border-[color:var(--border-subtle)] py-3"><p className="mb-2 text-sm">{t('domain.item')} {it.seq} · {it.description}</p><label className="block text-xs">{t('domain.code')}<Input disabled={!decl.can?.edit} value={values[it.seq]?.code??it.taric_additional_codes?.join(',')??''} onChange={e=>setValues({...values,[it.seq]:{...values[it.seq],code:e.target.value}})} /></label><label className="mt-2 block text-xs">{t('domain.reason')}<Textarea aria-label={t("domain.reason")} disabled={!decl.can?.edit} value={values[it.seq]?.reason??it.trade_defence_clearance_reason??''} onChange={e=>setValues({...values,[it.seq]:{...values[it.seq],reason:e.target.value}})}/></label>{it.trade_defence_cleared_by&&<p className="mt-1 text-xs text-ink-tertiary">{it.trade_defence_cleared_by.name} · {it.trade_defence_cleared_at}</p>}</div>)}{decl.can?.edit&&<Button className="mt-3" disabled={busy} onClick={save}>{t(busy?'state.saving':'domain.save_risk')}</Button>}{error&&<p role="alert" className="mt-2 text-sm text-destructive">{error}</p>}</Section>;
}
