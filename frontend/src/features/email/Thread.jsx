import React, { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Section } from '@/components/app/layout';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { EmptyState, ErrorState, Loadable } from '@/components/app/states';
import { apiPost, fileUrl, useApi } from '@/lib/api';
import { useT } from '@/lib/i18n';
import { dateTime } from '@/lib/format';

export function mailStatus(value, t) {
  const key = `email.status.${value}`;
  return t(key) === key ? t('email.unavailable') : t(key);
}

export function Thread({ thread, reload }) {
  const { t, locale } = useT();
  const [reply, setReply] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  async function send(e) {
    e.preventDefault(); setBusy(true); setError(null); setResult(null);
    try {
      const r = await apiPost(`/email-channel/threads/${thread.id}/reply`, {text:reply});
      setResult(mailStatus(r.status, t));
      if (r.status === 'sent' || r.status === 'delivered') setReply('');
      reload?.();
    } catch(e) { setError(e); } finally { setBusy(false); }
  }
  return <section className="min-w-0 space-y-4" aria-label={t('email.thread')}>
    <div className="space-y-1 break-words">
      <h2 className="text-lg">{thread.subject}</h2>
      <p className="text-sm text-ink-secondary">{thread.customer_email} · {mailStatus(thread.state,t)}</p>
      {thread.needs_owner && <p className="text-sm status-in-review rounded-lg px-3 py-2">{t('email.needs_owner')}</p>}
      <div className="flex flex-wrap gap-3 text-sm">
        {thread.quote && <Link className="underline" to={`/ops/quotes/${thread.quote.id}`}>{thread.quote.number}</Link>}
        {thread.shipment && <Link className="underline" to={`/ops/shipments/${thread.shipment.id}`}>{thread.shipment.reference}</Link>}
        {thread.invoice && <Link className="underline" to={`/ops/billing/${thread.invoice.id}`}>{thread.invoice.number} · {mailStatus(thread.invoice.status,t)}</Link>}
      </div>
    </div>
    <ol className="divide-y border-y">
      {(thread.messages || []).map(m => <li key={m.id} className="py-4 space-y-2 min-w-0 break-words">
        <div className="flex flex-wrap justify-between gap-2 text-xs text-ink-secondary">
          <span>{t(m.direction === 'in' ? 'email.in' : 'email.out')} · {m.from_email}</span>
          <time>{dateTime(m.created_at,locale)}</time>
        </div>
        <div className="flex flex-wrap gap-2 items-baseline"><h3 className="text-sm font-medium">{m.subject}</h3>
          <span className="text-xs">{mailStatus(m.delivery?.status || m.status,t)}</span></div>
        <p className="text-sm whitespace-pre-wrap break-words">{m.new_text || m.text || t('email.no_text')}</p>
        {!!m.attachments?.length && <ul className="text-sm space-y-1">{m.attachments.map((a,i) => <li key={i}>
          {a.file_id ? <a className="underline" href={fileUrl(`/email-channel/messages/${m.id}/attachments/${i}`)}>{a.filename}</a> : a.filename}
          {' · '}{Math.ceil(a.size/1024)} KB</li>)}</ul>}
        {m.delivery?.error || m.error ? <p role="alert" className="text-sm status-failed rounded-lg p-2">{m.delivery?.error || m.error}</p> : null}
        <details className="text-xs text-ink-secondary"><summary className="cursor-pointer">{t('email.audit')}</summary>
          <dl className="mt-2 space-y-1 break-all"><dt>Message-ID</dt><dd>{m.message_id || t('email.unavailable')}</dd>
            {m.in_reply_to && <><dt>In-Reply-To</dt><dd>{m.in_reply_to}</dd></>}
            {m.decision && <><dt>{t('email.decision')}</dt><dd>{m.decision}</dd></>}
          </dl></details>
      </li>)}
    </ol>
    <form onSubmit={send} className="space-y-2">
      <label htmlFor={`reply-${thread.id}`} className="text-sm">{t('email.reply')}</label>
      <Textarea id={`reply-${thread.id}`} value={reply} onChange={e=>setReply(e.target.value)} disabled={busy} rows={3} />
      {error && <ErrorState error={error}/>}
      {result && <p role="status" className="text-sm">{result}</p>}
      <Button type="submit" disabled={busy || !reply.trim()}>{busy ? t('email.sending') : t('email.send')}</Button>
    </form>
  </section>;
}

export default function EmailPanel({ kind='shipment' }) {
  const {id} = useParams(); const {t} = useT();
  const q = useApi(`/email-channel/for?${kind}_id=${encodeURIComponent(id)}`);
  return <Section title={t('email.title')}><Loadable q={q}>{d => d.items?.length
    ? <div className="space-y-6">{d.items.map(th=><Thread key={th.id} thread={th} reload={q.reload}/>)}</div>
    : <EmptyState title={t('email.empty')}/>}</Loadable></Section>;
}
