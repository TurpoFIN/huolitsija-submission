import dictionaries from '@/i18n';
import { currentLocale } from './localizedData';
export function apiErrorMessage(status, detail, locale=currentLocale()) {
  const t=k=>dictionaries[locale]?.['review.'+k]||dictionaries.en['review.'+k];
  if(status===0)return t('offline');
  if(status===404)return t('not_found');
  if(status===401)return t('unauthorized');
  if(status===403)return t('forbidden');
  if(status>=500)return t('server_error');
  if(status===409)return t('conflict');
  if(locale==='en' && typeof detail==='string')return detail;
  return t(status===422||status===400?'invalid':'request_error');
}
