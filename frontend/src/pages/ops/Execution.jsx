import React, {useState} from 'react';
import {Link,useSearchParams} from 'react-router-dom';
import {useT} from '@/lib/i18n';
import {useApi,fileUrl} from '@/lib/api';
import {PageHeader} from '@/components/app/layout';
import {Loadable} from '@/components/app/states';
import {Button} from '@/components/ui/button';
import {Action,Field,Form,Rows,Section,State,isoLocal} from '@/features/execution/common';
import ShipmentExecution from '@/features/execution/ShipmentExecution';
export default function Execution(){
 const {t,locale}=useT(),[params,setParams]=useSearchParams();const tab=params.get('tab')||'overview';
 const q=useApi('/execution/'+({overview:'overview',slots:'slots',consolidations:'consolidations',crossdock:'crossdock',warehouse:'warehouse-orders'}[tab]||'overview'));
 const shipments=useApi('/shipments'),warehouses=useApi('/execution/warehouses');
 const opts=(shipments.data?.items||[]).map(s=>({value:s.id,label:s.reference}));
 const [selected,setSelected]=useState(params.get('shipment')||'');
 const s=(shipments.data?.items||[]).find(s=>s.id===selected);
 const values=(keys)=>keys.map(value=>({value,label:t('ex.state.'+value)}));
 const col=(key,label,render)=>({key,label:t('ex.'+(label||key)),render});
 const st=col('status',null,r=><State value={r.status}/>);
 const changed=()=>{q.reload();warehouses.reload();};
 const chooseShipment=<Field name="shipment_id" label={t('ex.shipment')} options={opts}/>;
 return <div className="space-y-5 min-w-0"><PageHeader title={t('ex.title')} actions={<Button variant="outline" onClick={changed}>{t('ex.refresh')}</Button>}/>
 <nav className="flex flex-wrap gap-2 border-b pb-2" aria-label={t('ex.title')}>{['overview','slots','consolidations','crossdock','warehouse'].map(k=><Button key={k} variant={k===tab?'secondary':'ghost'} onClick={()=>setParams({tab:k})}>{t('ex.'+k)}</Button>)}</nav>
 <Loadable q={q}>{d=><>
 {tab==='overview'&&<><Rows items={d.items} columns={[col('reference',null,r=><button className="underline" onClick={()=>setSelected(r.shipment_id)}>{r.reference}</button>),col('carrier'),col('si_status','status',r=><State value={r.si_status||r.state}/>),col('next_cutoff','next',r=>r.next_cutoff?new Date(r.next_cutoff.at).toLocaleString(locale):t('ex.state.done'))]}/>
 <label className="flex flex-col gap-1 text-sm max-w-md">{t('ex.shipment')}<select aria-label={t("ex.shipment")} value={selected} onChange={e=>setSelected(e.target.value)} className="border rounded-md h-9 bg-background px-2"><option value="">{t('ex.select')}</option>{opts.map(o=><option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
 {selected&&<ShipmentExecution key={selected} shipmentId={selected}/>}
 {(d.alerts||[]).map(a=><p className="border-l-2 pl-3 text-sm" key={a.id}>{a[`title_${locale}`]}</p>)}</>}
 {tab==='slots'&&<><Rows items={d.items} columns={[col('reference'),col('kind',null,r=><State value={r.kind}/>),col('carrier_name','carrier'),col('window','start_time',r=>`${new Date(r.window.start).toLocaleString(locale)} – ${new Date(r.window.end).toLocaleString(locale)}`),st,col('action','mail_status',r=>r.status==='proposed'?<Action path={`/execution/slots/${r.id}/accept`} onSaved={changed}>{t('ex.accept')}</Action>:<State value={r.email_status}/>)]}/>
 <Section title={t('ex.request')}><label className="flex flex-col gap-1 text-sm max-w-md">{t('ex.shipment')}<select aria-label={t("ex.shipment")} className="border rounded-md h-9 bg-background px-2" value={selected} onChange={e=>setSelected(e.target.value)}><option value="">{t('ex.select')}</option>{opts.map(o=><option value={o.value} key={o.value}>{o.label}</option>)}</select></label>
 {selected&&<SlotForm sid={selected} onSaved={changed}/>}</Section></>}
 {tab==='consolidations'&&<><p className="text-sm text-ink-secondary">{t('ex.capacity_note')}</p>
 {d.items.map(r=><Section key={r.id} title={`${r.reference} · ${r.origin_hub} → ${r.destination_hub}`}><p className="text-sm"><State value={r.status}/> · {r.weight_kg} / {r.capacity_kg} kg · {r.volume_m3} / {r.capacity_m3} m³</p>
 <Rows items={r.houses} columns={[col('reference'),col('packages'),col('weight_kg','weight'),col('volume_m3','volume')]}/>
 <a className="text-sm underline" href={fileUrl(`/api/execution/consolidations/${r.id}/load-plan.csv`)}>{t('ex.load_plan')}</a>
 {r.status==='open'&&<><Form path={`/execution/consolidations/${r.id}/houses`} onSaved={changed} label={t('ex.assign')}>{chooseShipment}</Form><Action path={`/execution/consolidations/${r.id}/actions/close`} onSaved={changed}>{t('ex.close')}</Action></>}
 {['closed','arrived'].includes(r.status)&&<Form path={`/execution/consolidations/${r.id}/actions/${r.status==='closed'?'arrive':'deconsolidate'}`} onSaved={changed} label={t('ex.'+(r.status==='closed'?'arrive':'deconsolidate'))}><Field name="evidence"/></Form>}</Section>)}
 <Section title={t('ex.new_master')}><Form path="/execution/consolidations" onSaved={changed} label={t('ex.create')} transform={v=>({...v,cutoff:isoLocal(v.cutoff),capacity_kg:Number(v.capacity_kg),capacity_m3:Number(v.capacity_m3)})}>
 <Field name="origin_hub" label={t('ex.origin')}/><Field name="destination_hub" label={t('ex.destination')}/><Field name="mode" options={values(['sea','road','rail'])}/>
 <Field name="capacity_kg" type="number"/><Field name="capacity_m3" type="number"/><Field name="cutoff" type="datetime-local"/></Form></Section></>}
 {tab==='crossdock'&&<><p className="text-sm text-ink-secondary">{t('ex.disclaimer')}</p>{d.items.map(r=><Section title={`${r.reference} · ${r.hub}`} key={r.id}>
 <p><State value={r.status}/> · {t('ex.expected')}: {r.expected_packages}</p><Rows items={r.steps} columns={[col('action','step',r=><State value={r.action}/>),col('packages'),col('evidence')]}/>
 {r.reconciliations?.map((x,i)=><p key={i} className="text-sm">{t('ex.reconcile')}: {x.packages} · {x.evidence}</p>)}
 {r.status==='discrepancy'&&<Form path={`/execution/crossdock/${r.id}/reconcile`} onSaved={changed} label={t('ex.reconcile')} transform={v=>({...v,packages:Number(v.packages)})}><Field name="packages" type="number"/><Field name="evidence"/></Form>}
 {r.status!=='complete'&&r.status!=='discrepancy'&&<Form path={`/execution/crossdock/${r.id}/steps`} onSaved={changed} label={t('ex.step')} transform={v=>({...v,packages:Number(v.packages),action:['arrive','unload','sort','reload','depart'][r.steps.length]})}>
 <Field name="packages" type="number"/><Field name="evidence"/><p className="text-sm self-end"><State value={['arrive','unload','sort','reload','depart'][r.steps.length]}/></p></Form>}</Section>)}
 <Section title={t('ex.crossdock')}><Form path="/execution/crossdock" onSaved={changed} label={t('ex.create')} transform={v=>({...v,packages:Number(v.packages)})}>{chooseShipment}<Field name="hub"/><Field name="packages" type="number"/></Form></Section></>}
 {tab==='warehouse'&&<><p className="text-sm text-ink-secondary">{t('ex.disclaimer')}</p>{d.items.map(r=><Section key={r.id} title={`${r.reference} · ${r.warehouse_name}`}>
 <p className="text-sm"><State value={r.kind}/> · <State value={r.status}/> · {r.packages} {t('ex.package_units')} · <State value={r.provider}/></p>{r.error&&<p role="alert">{t('ex.connection_failed')}</p>}
 {r.external_reference&&<p>{r.external_reference} · <State value={r.external_state}/></p>}
 {r.history?.map((h,i)=><p key={i} className="text-sm">{h.evidence} · {h.packages} · <State value={h.status}/></p>)}
 {['draft','failed','awaiting_approval'].includes(r.status)&&<Action path={`/execution/warehouse-orders/${r.id}/send`} onSaved={changed}>{t('ex.send')}</Action>}
 {['submitted','discrepancy'].includes(r.status)&&<Form path={`/execution/warehouse-orders/${r.id}/milestone`} onSaved={changed} label={t('ex.milestone')} transform={v=>({...v,packages:Number(v.packages)})}><Field name="packages" type="number"/><Field name="evidence"/></Form>}</Section>)}
 <Section title={t('ex.new_order')}><Form path="/execution/warehouse-orders" onSaved={changed} label={t('ex.create')}>{chooseShipment}<Field name="kind" options={values(['inbound','outbound'])}/><Field name="warehouse_id" label={t('ex.warehouse')} options={(warehouses.data?.items||[]).map(w=>({value:w.id,label:w.name}))}/></Form></Section>
 <details className="border rounded-lg p-4"><summary className="cursor-pointer">{t('ex.new_warehouse')}</summary><div className="pt-4"><Form path="/execution/warehouses" onSaved={changed} label={t('ex.create')} transform={v=>({...v,provider:'email'})}><Field name="name"/><Field name="email" type="email"/></Form></div></details></>}
 </>}</Loadable></div>;
}
function SlotForm({sid,onSaved}){const {t}=useT();const s=useApi(`/shipments/${sid}`);
 return <Loadable q={s}>{d=><Form path={`/execution/shipments/${sid}/slots`} onSaved={onSaved} label={t('ex.request')} transform={v=>({leg_seq:Number(v.leg_seq),kind:v.kind,window:{start:isoLocal(v.start_time),end:isoLocal(v.end_time)}})}>
 <Field name="leg_seq" label={t('ex.leg')} options={(d.legs||d.shipment?.legs||[]).map(l=>({value:l.seq,label:`${l.seq}: ${l.from?.name||l.from?.code} → ${l.to?.name||l.to?.code}`}))}/><Field name="kind" options={['pickup','delivery'].map(value=>({value,label:t('ex.state.'+value)}))}/>
 <Field name="start_time" type="datetime-local"/><Field name="end_time" type="datetime-local"/></Form>}</Loadable>;
}
