import React, { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useT } from '@/lib/i18n';
import { apiPost } from '@/lib/api';

export default function SourcingStatus({ quote, token, onRefresh }) {
  const { t, locale } = useT();
  const [email, setEmail] = useState('');
  const [state, setState] = useState('idle');
  const [error, setError] = useState(null);
  useEffect(() => { setState('idle'); setError(null); }, [quote?.id]);
  const s = quote?.sourcing;
  if (!s || s.status === 'none') return null;
  const subscribe = async (event) => {
    event.preventDefault(); setState('saving'); setError(null);
    try {
      await apiPost(`/public/quote/${quote.id}/notify`, { token, email, locale });
      setState('saved');
    } catch (e) { setState('idle'); setError(e.message); }
  };
  return <section className="rounded-xl border p-3 text-sm flex flex-col gap-2" aria-label={t('gq.sourcing')}>
    <p className="font-medium">{t('gq.sourcing')}</p>
    <p>{s[locale === 'fi' ? 'message_fi' : 'message_en']}</p>
    {s.eta_at && ['requested', 'partial'].includes(s.status) && <p className="text-xs text-ink-secondary">{t(s.eta_basis?.startsWith('partner reply') ? 'gq.eta_history' : 'gq.eta_default')}</p>}
    {onRefresh && <Button size="sm" variant="outline" className="self-start" onClick={onRefresh}>{t('gq.refresh')}</Button>}
    {token && ['requested', 'partial'].includes(s.status) && (state === 'saved'
      ? <p role="status">{t('gq.subscribed')}</p>
      : <form className="flex flex-wrap items-end gap-2" onSubmit={subscribe}>
        <label className="flex flex-col gap-1 min-w-0 text-xs">{t('gq.email')}
          <Input type="email" required value={email} onChange={e => setEmail(e.target.value)} autoComplete="email" />
        </label>
        <Button type="submit" size="sm" variant="secondary" disabled={state === 'saving'}>{t('gq.notify')}</Button>
      </form>)}
    {error && <p role="alert" className="text-danger-fg">{error}</p>}
  </section>;
}
