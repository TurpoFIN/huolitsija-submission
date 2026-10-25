import React, { useState } from 'react';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { useT } from '@/lib/i18n';
// Select the intended new value first; only confirmation dispatches a mutation.
export function useConfirmChange() {
  const { t } = useT();
  const [pending,setPending] = useState(null);
  const confirm = (action,label) => setPending({action,label});
  const dialog = <AlertDialog open={!!pending} onOpenChange={open=>!open&&setPending(null)}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t('review.confirm.title')}</AlertDialogTitle><AlertDialogDescription>{pending?.label || t('review.confirm.body')}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>{t('action.cancel')}</AlertDialogCancel><AlertDialogAction onClick={()=>{const action=pending?.action;setPending(null);action?.();}}>{t('review.confirm.save')}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>;
  return { confirm, dialog };
}
