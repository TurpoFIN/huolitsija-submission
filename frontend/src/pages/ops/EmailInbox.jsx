import React, { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { PageHeader } from '@/components/app/layout';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { EmptyState, Loadable, Freshness } from '@/components/app/states';
import { useApi } from '@/lib/api';
import { useT } from '@/lib/i18n';
import { Thread, mailStatus } from '@/features/email/Thread';

export default function EmailInbox() {
  const {t} = useT(); const [params,setParams] = useSearchParams();
  const [search,setSearch] = useState(''); const [owner,setOwner] = useState(false);
  const id=params.get('thread');
  const list=useApi(`/email-channel/threads?q=${encodeURIComponent(search)}${owner?'&owner=true':''}`);
  const thread=useApi(id?`/email-channel/threads/${id}`:null);
  const status=useApi('/email-channel/status');
  const loose=useApi('/email-channel/messages?unthreaded=true');
  return <>
    <PageHeader title={t('email.title')} actions={<Freshness at={list.updatedAt} onRefresh={()=>{list.reload();thread.reload();}} refreshing={list.refreshing}/>}/>
    <Loadable q={status}>{s=><div className="mb-4 text-sm text-ink-secondary break-words">
      <p>{s.address} · {s.provider === 'log' ? t('email.log_only') : s.provider}</p>
      {!s.imap_configured && !s.webhook_configured && <p className="status-in-review rounded-lg p-2 mt-2">{t('email.inbound_missing')}</p>}
      {!s.enabled && <p>{t('email.disabled')}</p>}
    </div>}</Loadable>
    <div className="flex flex-wrap gap-3 mb-4 items-center">
      <Input className="sm:max-w-sm" aria-label={t('email.search')} placeholder={t('email.search')} value={search} onChange={e=>setSearch(e.target.value)}/>
      <label className="text-sm flex items-center gap-2"><input type="checkbox" checked={owner} onChange={e=>setOwner(e.target.checked)}/>{t('email.owner_filter')}</label>
    </div>
    <details className="mb-4 text-sm"><summary className="cursor-pointer">{t('email.unassigned')}</summary>
      <Loadable q={loose}>{d=>d.items.length ? <ul className="divide-y">{d.items.map(m=><li key={m.id} className="py-2 break-words">
        <p>{m.from_email} · {m.subject} · {mailStatus(m.status,t)}</p><p>{m.new_text}</p>
      </li>)}</ul> : <p className="py-2 text-ink-secondary">{t('email.no_unassigned')}</p>}</Loadable>
    </details>
    <div className="grid lg:grid-cols-[300px_minmax(0,1fr)] gap-6">
      <div className={id?'hidden lg:block min-w-0':'min-w-0'}><Loadable q={list}>{d=>d.items.length ? <ul className="divide-y border-y">{d.items.map(th=><li key={th.id}>
        <Link className={`block py-3 px-2 break-words focus-visible:outline ${id===th.id?'bg-muted':''}`} to={`?thread=${th.id}`}>
          <span className="text-sm block font-medium">{th.customer_name || th.customer_email}</span>
          <span className="text-sm block">{th.subject}</span>
          <span className="text-xs text-ink-secondary">{th.ref && `${th.ref} · `}{th.needs_owner?t('email.needs_owner'):mailStatus(th.state,t)}</span>
        </Link></li>)}</ul> : <EmptyState title={t('email.empty')}/>}</Loadable></div>
      <div className="min-w-0">{id ? <><Button variant="ghost" className="mb-3 lg:hidden" onClick={()=>setParams({})}>{t('email.back')}</Button>
        <Loadable q={thread}>{d=><Thread key={d.id} thread={d} reload={thread.reload}/>}</Loadable></>
        : <p className="text-sm text-ink-secondary hidden lg:block">{t('email.select')}</p>}</div>
    </div>
  </>;
}
