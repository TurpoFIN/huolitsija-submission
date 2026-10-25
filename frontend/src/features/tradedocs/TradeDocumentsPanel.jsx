import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { Section } from '@/components/app/layout';
import { StatusBadge } from '@/components/app/StatusBadge';
import { ErrorState, LoadingRows } from '@/components/app/states';
import { useApi, apiGet, apiPost, fileUrl } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { useT } from '@/lib/i18n';
import { text, fields, deliveryState } from './text';
import DeliveryRelease from './DeliveryRelease';

const selectClass = 'h-9 w-full min-w-0 rounded-md border border-input bg-background px-2 text-sm';
const state = { draft: 'pending', issued: 'success', superseded: 'expired', cancelled: 'expired' };
export default function TradeDocumentsPanel({ shipment, mode }) {
  const { locale } = useT();
  const lang = locale === 'en' ? 'en' : 'fi';
  const tr = key => text[key]?.[lang === 'fi' ? 0 : 1] || key;
  const { can } = useAuth();
  const editable = mode === 'ops' && can('shipments.write');
  const q = useApi(`/shipments/${shipment.id}/trade-documents`);
  const [kind, setKind] = useState('');
  const [draft, setDraft] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState(null);
  const [amending, setAmending] = useState(null);
  const run = async (action, message) => {
    setBusy(true); setError(null); setNotice('');
    try { const result = await action(); setNotice(typeof message === 'function' ? message(result) : message); q.reload(); return result; }
    catch (e) { setError(e); return null; }
    finally { setBusy(false); }
  };
  const options = (q.data?.available || []).filter(x => x.applies);
  const chosen = kind || options[0]?.kind;
  const create = (k, final) => run(() => apiPost(`/shipments/${shipment.id}/trade-documents`, { kind: k, final }), tr('generated'));
  return <Section title={tr('title')}>
    <div data-testid="trade-documents" className="mb-6 space-y-3">
      {q.loading && !q.data && <LoadingRows rows={3} />}
      {q.error && <ErrorState error={q.error} onRetry={q.reload} />}
      {error && <ErrorState error={error} />}
      {error && chosen === 'delivery_order' && <p role="alert" className="text-sm">{lang === 'en' && typeof error.data?.detail === 'string' ? error.data.detail : 'Tarkasta luovutusvaltuutuksen lähdeasiakirja ja voimassaolo sekä lähetyksen pidot ja tullin luovutus. Päivitä valtuutus ennen lopullisen asiakirjan laatimista.'}</p>}
      {notice && <p role="status" className="text-sm">{notice}</p>}
      {editable && q.data && <form className="flex flex-wrap items-end gap-2" onSubmit={e => { e.preventDefault(); create(chosen, !draft); }}>
        <label className="min-w-0 flex-1 text-xs text-ink-secondary">{tr('kind')}
          <select aria-label={tr('kind')} className={selectClass} value={chosen || ''} onChange={e => setKind(e.target.value)}>
            {options.map(x => <option key={x.kind} value={x.kind}>{x.title[lang]}</option>)}
          </select>
        </label>
        <label className="flex h-9 items-center gap-2 text-sm"><input type="checkbox" checked={draft} onChange={e => setDraft(e.target.checked)} />{tr('draft')}</label>
        <Button type="submit" disabled={busy || !chosen}>{tr('create')}</Button>
      </form>}
      {editable && <p className="text-xs text-ink-secondary">{tr('generateNote')}</p>}
      {editable && chosen === 'delivery_order' && <DeliveryRelease shipmentId={shipment.id} release={q.data?.delivery_release} lang={lang} saved={q.reload} />}
      {q.data && !q.data.documents.length && <p className="py-4 text-sm text-ink-secondary">{tr('empty')}</p>}
      <ul className="divide-y divide-[color:var(--border-subtle)]">
        {(q.data?.documents || []).map(doc => <li key={doc.id} className="py-3" data-doc-kind={doc.kind}>
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0"><h3 className="text-sm font-medium">{doc.title[lang]}</h3>
              <p className="mt-1 break-all font-mono text-xs text-ink-secondary">{doc.doc_no} · {tr('version')} {doc.version}</p>
            </div>
            <StatusBadge size="sm" state={state[doc.status]} label={tr(doc.status)} />
          </div>
          <p className="mt-1 text-xs text-ink-secondary">{tr(deliveryState(doc.dispatches))}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Button asChild size="sm" variant="outline"><a href={fileUrl(`/trade-documents/${doc.id}/pdf?lang=fi`)} target="_blank" rel="noreferrer">{tr('pdf')}</a></Button>
            <Button asChild size="sm" variant="outline"><a href={fileUrl(`/trade-documents/${doc.id}/pdf?lang=en`)} target="_blank" rel="noreferrer">{tr('pdfEn')}</a></Button>
            <Button size="sm" variant="ghost" onClick={() => setSelected(doc)}>{tr('history')}</Button>
            {editable && doc.status !== 'cancelled' && <>
              <Button size="sm" variant="ghost" disabled={busy} onClick={async () => { const full = await run(() => apiGet(`/trade-documents/${doc.id}`), ''); if (full) setAmending(full); }}>{tr('amend')}</Button>
              {doc.status === 'draft' && !doc.draft_only && <Button size="sm" variant="ghost" disabled={busy} onClick={() => create(doc.kind, true)}>{tr('promote')}</Button>}
              {doc.kind === 'delivery_order' && <Button size="sm" variant="ghost" disabled={busy} onClick={() => run(() => apiPost(`/trade-documents/${doc.doc_no}/refresh`, {}), tr('saved'))}>{lang === 'fi' ? 'Päivitä luovutusvaltuutus' : 'Refresh release evidence'}</Button>}
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => run(() => apiPost(`/trade-documents/${doc.id}/send`, {}), r =>
                [r.sent?.length ? `${tr('sent')}: ${r.sent.length}` : '', r.logged?.length ? tr('logged') : '', r.failed?.length ? tr('failed') : '', r.missing?.length ? tr('incomplete') : '', r.waiting ? tr('pending') : '', r.skipped?.length ? tr('sent') : ''].filter(Boolean).join(' · ') || tr('notSent'))}>{tr('send')}</Button>
            </>}
          </div>
        </li>)}
      </ul>
      {selected && <History doc={selected} tr={tr} close={() => setSelected(null)} />}
      {amending && <Amend doc={amending} tr={tr} lang={lang} close={() => setAmending(null)} saved={() => { setAmending(null); setNotice(tr('saved')); q.reload(); }} />}
    </div>
  </Section>;
}
function History({ doc, tr, close }) {
  const q = useApi(`/trade-documents/${doc.doc_no}/versions`);
  return <Dialog open onOpenChange={open => !open && close()}><DialogContent className="max-h-[85vh] overflow-auto">
    <DialogHeader><DialogTitle>{doc.doc_no}</DialogTitle><DialogDescription>{tr('historyNote')}</DialogDescription></DialogHeader>
    {q.loading && <LoadingRows rows={3} />}{q.error && <ErrorState error={q.error} onRetry={q.reload} />}
    {(q.data?.versions || []).map(v => <div key={v.id} className="border-b border-[color:var(--border-subtle)] pb-3 text-sm">
      <div className="flex justify-between gap-2"><span>{tr('version')} {v.version}</span><span>{tr(v.status)}</span></div>
      {v.amendment?.reason && <p className="mt-1 break-words">{v.amendment.reason}</p>}
      <div className="my-2 flex flex-wrap gap-3">{['fi','en'].map(l => <a key={l} className="underline" href={fileUrl(`/trade-documents/${v.id}/pdf?lang=${l}`)} target="_blank" rel="noreferrer">PDF {l.toUpperCase()}</a>)}
        {(v.formats || []).map(f => <a key={f} className="underline" href={fileUrl(`/trade-documents/${v.id}/data?format=${f}`)} target="_blank" rel="noreferrer">{f}</a>)}</div>
      {(v.dispatches || []).map((d, i) => <p key={i} className="break-words text-xs text-ink-secondary">{d.to || d.role} · {tr(d.status)} · {new Date(d.at).toLocaleString()}</p>)}
    </div>)}
  </DialogContent></Dialog>;
}
function Amend({ doc, tr, lang, close, saved }) {
  const [field, setField] = useState('refs.customer');
  const [value, setValue] = useState('');
  const [reason, setReason] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const current = field.split('.').reduce((v, k) => v?.[k], doc.data);
  const submit = async e => {
    e.preventDefault(); setBusy(true); setError(null);
    try { await apiPost(`/trade-documents/${doc.doc_no}/amend`, { changes: { [field]: value }, reason }); saved(); }
    catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <Dialog open onOpenChange={open => !open && close()}><DialogContent>
    <DialogHeader><DialogTitle>{tr('amend')} · {doc.doc_no}</DialogTitle><DialogDescription>{tr('historyNote')}</DialogDescription></DialogHeader>
    <form onSubmit={submit} className="space-y-3">
      <label className="block text-sm">{tr('field')}<select className={selectClass} value={field} onChange={e => { setField(e.target.value); setValue(''); }}>{fields.map(([p, fi, en]) => <option key={p} value={p}>{lang === 'fi' ? fi : en}</option>)}</select></label>
      <p className="break-words text-xs text-ink-secondary">{tr('current')}: {current ?? tr('missing')}</p>
      <label className="block text-sm">{tr('value')}<Input required value={value} onChange={e => setValue(e.target.value)} /></label>
      <label className="block text-sm">{tr('reason')}<Input required minLength={3} value={reason} onChange={e => setReason(e.target.value)} /></label>
      {error && <ErrorState error={error} />}
      <Button type="submit" disabled={busy || reason.trim().length < 3}>{tr('save')}</Button>
    </form>
  </DialogContent></Dialog>;
}
