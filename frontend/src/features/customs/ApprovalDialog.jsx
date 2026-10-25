// Decide the pending "customs_filing" approval: approve (states the customs debt it creates) or reject with a reason.
import React, { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { ErrorState } from "@/components/app/states";
import { FourEyesNotice } from "@/components/app/FourEyes";
import { fourEyesState } from "@/lib/fourEyes";
import { apiPost } from "@/lib/api";
import { money } from "@/lib/format";
import { useT } from "@/lib/i18n";

export default function ApprovalDialog({ decl, decision, open, onOpenChange, onDone }) {
  const { t, locale } = useT();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [fourEyes, setFourEyes] = useState(null);
  useEffect(() => { if (open) { setNote(""); setError(null); setFourEyes(null); } }, [open]);
  const tot = decl?.totals || {};
  const reject = decision === "rejected";
  const submit = async (override = false) => {
    if (reject && !note.trim()) { setError({ message: t("customs.rr.note_required") }); return; }
    setBusy(true);
    setError(null);
    try {
      await apiPost(`/approvals/${decl.approval_id}/decide`, { decision, note: note.trim() || undefined, ...(override ? { override: true } : {}) });
      onOpenChange(false);
      await onDone?.(decision);
    } catch (e) {
      const state = fourEyesState(e);
      if (state) setFourEyes(state); else setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{reject ? t("action.reject") : t("customs.confirm_approve.title", { lrn: decl?.lrn })}</DialogTitle>
          {!reject && (
            <DialogDescription>
              {t("customs.confirm_approve.body", {
                company: decl?.company_name || "—",
                duty: money(tot.duty_cents, locale, { decimals: 2 }),
                vat: money(tot.vat_cents, locale, { decimals: 2 }),
                route: decl?.vat_route ? t(`customs.vat.${decl.vat_route}`) : "—",
              })}
            </DialogDescription>
          )}
        </DialogHeader>
        {!reject && decl?.gateway?.channel === "manual" && <p className="text-sm text-ink-secondary">{t("customs.confirm_approve.manual")}</p>}
        <Textarea value={note} onChange={(e) => setNote(e.target.value)} rows={3} placeholder={t("customs.rr.note")} aria-label={t("customs.rr.note")} />
        {!reject && <FourEyesNotice state={fourEyes} note={note} busy={busy} onOverride={() => submit(true)} />}
        {error && <ErrorState error={error} compact />}
        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>{t("action.cancel")}</Button>
          <Button variant={reject ? "destructive" : "default"} onClick={() => submit()} disabled={busy || (!reject && !!fourEyes)}>
            {busy ? t("state.saving") : reject ? t("action.reject") : t("customs.action.approve_file")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
