import React from 'react';
import {useParams,Link} from 'react-router-dom';
import {useT} from '@/lib/i18n';
import {useApi} from '@/lib/api';
import {Loadable} from '@/components/app/states';
import {Action,Form,Field,Rows,State,Section} from './common';
export default function ShipmentExecution({shipmentId}){
 const params=useParams(),sid=shipmentId||params.id; const {t,locale}=useT(); const q=useApi(`/execution/shipments/${sid}`);
 return <Section title={t('ex.details')}><Loadable q={q}>{d=><>
 {!d.record?<><p className="text-sm text-ink-secondary">{t('ex.no_booking')}</p><Form path={`/execution/shipments/${sid}/start`} onSaved={q.reload} label={t('ex.start')}>
 <Field name="carrier_booking_reference" label={t('ex.booking')}/><Field name="carrier_code"/><Field name="vgm_signatory" label={t('ex.signatory')} required={false}/></Form></>:<>
 <p className="text-sm">{d.record.carrier_name} · {d.record.carrier_booking_reference} · <State value={d.si?.status||d.record.state}/></p>
 <Rows items={d.cutoffs} columns={[{key:'label',label:t('ex.cutoff'),render:r=>r[`label_${locale}`]},{key:'at',label:t('ex.start_time'),render:r=>new Date(r.at).toLocaleString(locale)},
 {key:'source',label:t('ex.source'),render:r=>r.source==='derived'?t('ex.derived'):t('ex.carrier')},{key:'state',label:t('ex.status'),render:r=><State value={r.state}/>}]}/>
 {(d.record.containers||[]).map(c=><Form key={c.index} path={`/execution/shipments/${sid}/containers/${c.index}`} onSaved={q.reload} transform={v=>({...v,tare_kg:Number(v.tare_kg),cargo_gross_kg:Number(v.cargo_gross_kg)})}>
 <Field name="equipment_reference" label={t('ex.container')} defaultValue={c.equipment_reference}/><Field name="seal_number" label={t('ex.seal')} defaultValue={c.seal_number}/>
 <Field name="tare_kg" label={t('ex.tare')} type="number" defaultValue={c.tare_kg}/><Field name="cargo_gross_kg" label={t('ex.weight')} type="number" defaultValue={c.cargo_gross_kg}/>
 <Field name="vgm_signatory" label={t('ex.signatory')} defaultValue={d.record.vgm_signatory}/></Form>)}
 {[...(d.si_missing||[]),...(d.vgm_missing||[])].length>0&&<p className="text-sm" role="status">{t('ex.missing')}: {[...new Set([...(d.si_missing||[]),...(d.vgm_missing||[])].map(m=>m[locale]))].join(', ')}</p>}
 {d.record.si_required&&<div className="flex flex-wrap gap-2"><Action path={`/execution/shipments/${sid}/si`} onSaved={q.reload}>{t('ex.si')}</Action><Action path={`/execution/shipments/${sid}/vgm`} onSaved={q.reload}>{t('ex.vgm')}</Action></div>}
 <Rows items={d.vgm} columns={[{key:'equipment_reference',label:t('ex.container')},{key:'weight_kg',label:'VGM kg'},{key:'status',label:t('ex.status'),render:r=><State value={r.status}/>} ]}/>
 </>}
 <Link className="text-sm underline" to="/ops/execution">{t('ex.title')}</Link></>}</Loadable></Section>;
}
