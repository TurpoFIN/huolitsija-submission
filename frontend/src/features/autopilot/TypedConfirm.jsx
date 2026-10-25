import React, { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useT } from "@/lib/i18n";

/**
 * The typed-enable dialog (same pattern as features/DomainSettings.jsx): the action runs only after the person types
 * the word "enable". Escape / Cancel closes it and focus returns to the control that opened it (returnFocus).
 * onConfirm(word) may throw; its message is shown in the dialog.
 */
export function TypedConfirm({ open, onOpenChange, title, body, consequence, confirmLabel, onConfirm, returnFocus, busy, error }) {
  const { t } = useT();
  const [word, setWord] = useState("");
  useEffect(() => { if (open) setWord(""); }, [open]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent onCloseAutoFocus={(e) => { if (returnFocus?.current) { e.preventDefault(); returnFocus.current.focus(); } }}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{body}</DialogDescription>
        </DialogHeader>
        {consequence && <p className="text-sm text-ink-secondary">{consequence}</p>}
        <form onSubmit={(e) => { e.preventDefault(); if (word === "enable" && !busy) onConfirm(word); }} className="flex flex-col gap-3">
          <label className="flex flex-col gap-1.5 text-sm">
            <span>{t("ap.typed.label")}</span>
            <Input aria-label="enable" value={word} onChange={(e) => setWord(e.target.value)} autoComplete="off" autoCapitalize="off" spellCheck={false} />
          </label>
          {error && <p role="alert" className="text-sm text-danger-fg">{error}</p>}
          <DialogFooter className="gap-2">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>{t("action.cancel")}</Button>
            <Button type="submit" disabled={word !== "enable" || busy}>{busy ? t("state.saving") : confirmLabel}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
