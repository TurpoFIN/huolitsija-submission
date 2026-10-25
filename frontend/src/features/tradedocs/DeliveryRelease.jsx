import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ErrorState, LoadingRows } from '@/components/app/states';
import { useApi, apiPost, fileUrl } from '@/lib/api';

const labels = [
  ['reference', 'Luovutusviite', 'Release reference'],
  ['issuer', 'Valtuutuksen antaja', 'Authorizing carrier / terminal'],
  ['terminal', 'Luovuttava terminaali', 'Releasing terminal'],
  ['haulier', 'Valtuutettu kuljetusliike', 'Authorized haulier'],
  ['transport_document', 'Rahtikirjan numero', 'Transport document number'],
  ['valid_until', 'Voimassa asti (paikallinen aika)', 'Valid until (local time)'],
  ['reason', 'Tarkastuksen peruste', 'Review basis'],
];

export default function DeliveryRelease({ shipmentId, release, lang, saved }) {
  const fi = lang === 'fi';
  const t = (a, b) => fi ? a : b;
  const documents = useApi(`/documents?shipment_id=${shipmentId}`);
  const [form, setForm] = useState({ document_id: '', reference: '', issuer: '', terminal: '', haulier: '', transport_document: '', valid_until: '', reason: '' });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async e => {
    e.preventDefault(); setError(null); setBusy(true);
    try {
      await apiPost(`/shipments/${shipmentId}/delivery-release`, { ...form, valid_until: new Date(form.valid_until).toISOString() });
      saved();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <details className="border-t border-[color:var(--border-subtle)] py-2" data-testid="delivery-release">
    <summary className="cursor-pointer text-sm">{t('Luovutusvaltuutus', 'Release authorization')}: {release ? release.reference : t('Puuttuu', 'Missing')}</summary>
    {release && <p role="status" className="mt-2 text-sm">{release.demo ? 'DEMO · ' : ''}{release.issuer} · {release.haulier} · {release.valid_until} · {t('Käsin kirjattu', 'Manually recorded')}</p>}
    <p className="my-2 text-xs text-ink-secondary">{t('Lataa lähdeasiakirja lähetyksen asiakirjoihin ja tarkasta luovutuksen ehdot. Kirjaus ei ole palveluntarjoajan vahvistus.', 'Upload the source to shipment documents and review the release conditions. This record is not provider verification.')}</p>
    {documents.loading && <LoadingRows rows={1} />}
    {documents.error && <ErrorState error={documents.error} onRetry={documents.reload} />}
    <form onSubmit={submit} className="space-y-3">
      <label className="block text-sm">{t('Lähdeasiakirja', 'Source document')}
        <select required aria-label={t('Lähdeasiakirja', 'Source document')} className="h-9 w-full min-w-0 rounded-md border border-input bg-background px-2 text-sm" value={form.document_id} onChange={e => setForm({ ...form, document_id: e.target.value })}>
          <option value="">{t('Valitse ladattu asiakirja', 'Select uploaded document')}</option>
          {(documents.data?.items || []).map(d => <option key={d.id} value={d.id}>{d.filename}</option>)}
        </select>
      </label>
      {form.document_id && <a className="text-sm underline" href={fileUrl(`/documents/${form.document_id}/file`)} target="_blank" rel="noreferrer">{t('Avaa lähdeasiakirja', 'Open source document')}</a>}
      {!documents.loading && documents.data && !documents.data.items.length && <p className="text-sm">{t('Lataa ensin luovutusvaltuutus lähetyksen asiakirjoihin.', 'Upload the release authorization to shipment documents first.')}</p>}
      <Button type="button" size="sm" variant="ghost" onClick={documents.reload}>{t('Päivitä asiakirjat', 'Refresh documents')}</Button>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {labels.map(([key, a, b]) => <label className="block min-w-0 text-sm" key={key}>{t(a, b)}
          <Input required type={key === 'valid_until' ? 'datetime-local' : 'text'} minLength={key === 'reason' ? 5 : 3} value={form[key]} onChange={e => setForm({ ...form, [key]: e.target.value })} />
        </label>)}
      </div>
      {error && <ErrorState error={error} />}
      <Button type="submit" disabled={busy || !form.document_id}>{t('Kirjaa tarkastettu valtuutus', 'Record reviewed authorization')}</Button>
    </form>
  </details>;
}
