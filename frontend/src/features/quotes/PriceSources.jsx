import React from 'react';
import { useT } from '@/lib/i18n';
import { displayText } from '@/lib/localizedData';
import { date } from '@/lib/format';

export default function PriceSources({ option }) {
  const { t, locale } = useT();
  const sources = [...new Map((option.lines || []).filter(l => l.source).map(l => [JSON.stringify(l.source), l.source])).values()];
  if (!sources.length) return null;
  return <details className="text-xs text-ink-secondary">
    <summary className="cursor-pointer">{t('gq.sources')}</summary>
    <ul className="mt-2 flex flex-col gap-2">
      {sources.map((s, i) => <li key={i} className="break-words">
        <span className="font-medium">{t(`gq.${s.kind}`)}</span>{s.index ? ` · ${s.index}` : ''}
        {s.as_of && <span> · {t('gq.asof')}: {date(s.as_of, locale)}</span>}
        {s.note && <span className="block">{locale === 'fi' && s.note_fi ? s.note_fi : displayText(s.note, locale)}</span>}
        {/^https?:\/\//.test(s.url || '') && <a className="underline" href={s.url} target="_blank" rel="noreferrer">{t('gq.source_link')}</a>}
      </li>)}
    </ul>
  </details>;
}
