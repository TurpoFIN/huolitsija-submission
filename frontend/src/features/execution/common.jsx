import React, { useState } from 'react';
import { useT } from '@/lib/i18n';
import { apiPost } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ErrorState, EmptyState } from '@/components/app/states';
export function State({value}) { const {t}=useT(); return <span>{value ? t(`ex.state.${value}`) : t('ex.missing')}</span>; }
export function Field({name,label,type='text',options,required=true,defaultValue}) {
  const {t}=useT();
  return <label className="flex flex-col gap-1 text-sm min-w-0"><span>{label || t(`ex.${name}`)}</span>
    {options ? <select aria-label={label || t(`ex.${name}`)} className="h-9 rounded-md border bg-background px-2 min-w-0" name={name} required={required} defaultValue={defaultValue || ''}>
      <option value="">{t('ex.select')}</option>{options.map(o=><option key={o.value} value={o.value}>{o.label}</option>)}</select>
      : <Input name={name} type={type} required={required} defaultValue={defaultValue} step={type==='number'?'any':undefined} />}</label>;
}
export function Form({path,transform,children,onSaved,label}) {
 const {t}=useT(); const [busy,setBusy]=useState(false),[error,setError]=useState(null),[result,setResult]=useState(null);
 async function submit(e){e.preventDefault(); const data=Object.fromEntries(new FormData(e.currentTarget));setBusy(true);setError(null);setResult(null);
  try {const r=await apiPost(typeof path==='function'?path(data):path, transform?transform(data):data);setResult(r);onSaved?.(r);}
  catch(e){setError(e);}finally{setBusy(false);}}
 return <form onSubmit={submit} className="space-y-3">{error&&<ErrorState error={error}/>}
  <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{children}</div>
  <Button type="submit" disabled={busy}>{label||t('ex.save')}</Button>
  {result&&<p role="status" className="text-sm">{result.status?<State value={result.status}/>:t('ex.saved')}</p>}</form>;
}
export function Action({path,body={},children,onSaved}){
 const [error,setError]=useState(null),[busy,setBusy]=useState(false);
 return <span className="inline-flex flex-col gap-1"><Button size="sm" variant="outline" disabled={busy} onClick={async()=>{setBusy(true);setError(null);try{await apiPost(path,body);onSaved?.();}catch(e){setError(e);}finally{setBusy(false);}}}>{children}</Button>{error&&<ErrorState error={error}/>}</span>;
}
export function Rows({items,columns}) {const {t}=useT(); if(!items?.length)return <EmptyState title={t('ex.empty')}/>;
 return <div className="max-w-full overflow-x-auto"><table className="w-full text-sm"><thead className="hidden sm:table-header-group"><tr className="border-b">{columns.map(c=><th className="text-left font-medium p-2 whitespace-nowrap" key={c.key}>{c.label}</th>)}</tr></thead>
 <tbody className="block sm:table-row-group">{items.map((r,i)=><tr className="block border-b py-3 sm:py-0 sm:table-row align-top" key={r.id||r.shipment_id||i}>{columns.map(c=><td className="grid grid-cols-[7rem_minmax(0,1fr)] gap-2 py-1 px-2 sm:table-cell sm:p-2" key={c.key}><span className="sm:hidden text-muted-foreground">{c.label}</span><span className="min-w-0 break-words">{c.render?c.render(r):r[c.key]??t('ex.missing')}</span></td>)}</tr>)}</tbody></table></div>;
}
export function Section({title,children}) {return <section className="border rounded-lg p-4 space-y-4 min-w-0"><h2 className="text-base font-medium">{title}</h2>{children}</section>;}
export function isoLocal(v){if(!v)return null; const d=new Date(v); return Number.isNaN(d.getTime())?null:d.toISOString();}
