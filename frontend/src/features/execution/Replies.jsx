import React from 'react';
import {useSearchParams} from 'react-router-dom';
import {useApi} from '@/lib/api';
import {useT} from '@/lib/i18n';
import {Loadable} from '@/components/app/states';
import {PageHeader} from '@/components/app/layout';
import {Form,Field,Rows,Section,State,isoLocal} from './common';
export function PartnerSlots(){const {t}=useT();const q=useApi('/execution/partner/slots');return <div className="space-y-4"><PageHeader title={t('ex.slots')}/><Loadable q={q}>{d=><>{!d.items.length&&<p>{t('ex.empty')}</p>}{d.items.map(r=><Section key={r.id} title={r.reference}><Appointment row={r} path={`/execution/partner/slots/${r.id}/reply`} onSaved={q.reload}/></Section>)}</>}</Loadable></div>;}
function Appointment({row:r,path,onSaved}){const {t}=useT();return <><p className="text-sm"><State value={r.kind}/> · <State value={r.status}/> · {new Date(r.window.start).toLocaleString()} – {new Date(r.window.end).toLocaleString()}</p>
 {['requested','proposed'].includes(r.status)&&<Form path={path} onSaved={onSaved} label={t('ex.save')} transform={v=>({action:v.action,note:v.note,window:v.action==='propose'?{start:isoLocal(v.start_time),end:isoLocal(v.end_time)}:null})}>
 <Field name="action" label={t('ex.status')} options={['confirm','propose','reject'].map(value=>({value,label:t('ex.'+value)}))}/><Field name="start_time" type="datetime-local" required={false}/><Field name="end_time" type="datetime-local" required={false}/><Field name="note" required={false}/></Form>}</>;}
export default function Reply({warehouse=false}){const {t}=useT();const [params]=useSearchParams();const path=`/execution/${warehouse?'warehouse':'slot'}-reply?t=${encodeURIComponent(params.get('t')||'')}`;const q=useApi(path);
 return <main className="max-w-2xl mx-auto p-5 space-y-4"><PageHeader title={t(warehouse?'ex.receipt':'ex.slots')}/><Loadable q={q}>{r=><Section title={r.reference}>
 {warehouse?<><p><State value={r.kind}/> · <State value={r.status}/> · {t('ex.expected')}: {r.packages}</p><p className="text-sm">{t('ex.disclaimer')}</p>
 {['submitted','discrepancy'].includes(r.status)&&<Form path={path} onSaved={q.reload} label={t('ex.milestone')} transform={v=>({...v,packages:Number(v.packages)})}><Field name="packages" type="number"/><Field name="evidence"/></Form>}</>:<Appointment row={r} path={path} onSaved={result=>q.setData({...r,...result})}/>}</Section>}</Loadable></main>;
}
