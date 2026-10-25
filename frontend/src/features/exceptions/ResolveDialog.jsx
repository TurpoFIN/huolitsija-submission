// Resolve an exception: record the outcome and (optionally) send the customer a message in one step.
import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { AgentMark } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { apiGet, apiPost } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { Field } from "./common";
import { localTitle } from "./text";

/**
 * exception: list or detail record (ops shape). If it has no `ai_message`, the dialog loads the detail
 * so the customer message starts from the latest (possibly edited) AI draft.
 */
export default function ResolveDialog({ exception, open, onOpenChange, onResolved }) {
  const { t, locale } = useT();
  const [resolution, setResolution] = useState("");
  const [send, setSend] = useState(false);
  const [message, setMessage] = useState("");
  const [prefilled, setPrefilled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [touched, setTouched] = useState(false);
  const visible = exception?.customer_visible !== false && !!exception?.company_id;

  useEffect(() => {
    if (!open || !exception) return undefined;
    let alive = true;
    setResolution("");
    setError(null);
    setTouched(false);
    const apply = (ex) => {
      const pending = ex?.ai_message?.status === "pending_approval" ? ex.ai_message.body : null;
      const draft = pending || (ex?.ai_message ? null : ex?.ai_assessment?.customer_message_draft) || "";
      setMessage(draft);
      setPrefilled(!!draft);
      setSend(!!draft && ex?.customer_visible !== false);
    };
    if (exception.ai_message !== undefined) apply(exception);
    else {
      apply(exception);
      apiGet(`/exceptions/${exception.id}`).then((ex) => { if (alive) apply(ex); }).catch(() => {});
    }
    return () => { alive = false; };
    // Only reset when the dialog opens for an exception, not on every list poll.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, exception?.id]);

  const short = resolution.trim().length < 3;
  const submit = async () => {
    setTouched(true);
    if (short) return;
    if (send && !message.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const res = await apiPost(`/exceptions/${exception.id}/resolve`, {
        resolution: resolution.trim(),
        customer_message: send && visible ? message.trim() : null,
      });
      toast.success(res.customer_message ? t("exceptions.resolve.done_sent") : t("exceptions.resolve.done"));
      onOpenChange(false);
      onResolved?.(res);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("exceptions.resolve.title")}</DialogTitle>
          <DialogDescription className="text-ink-secondary">
            {exception ? `${exception.shipment_reference || ""}${exception.shipment_reference ? " · " : ""}${localTitle(exception, t, locale)}` : ""}
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <Field label={t("exceptions.resolve.resolution")} htmlFor="exc-resolution"
            error={touched && short ? t("exceptions.resolve.resolution_short") : null}>
            <Textarea id="exc-resolution" value={resolution} onChange={(e) => setResolution(e.target.value)} autoFocus
              placeholder={t("exceptions.resolve.resolution_ph")} aria-invalid={touched && short ? true : undefined} className="min-h-[72px]" />
          </Field>
          {visible ? (
            <div className="flex flex-col gap-2">
              <label className="flex items-center gap-2 text-sm">
                <Checkbox checked={send} onCheckedChange={(v) => setSend(!!v)} />
                {t("exceptions.resolve.send")}
              </label>
              {send && (
                <>
                  <Textarea value={message} onChange={(e) => { setMessage(e.target.value); }} className="min-h-[140px]"
                    aria-label={t("exceptions.msg.title")} aria-invalid={touched && !message.trim() ? true : undefined} />
                  <p className="flex flex-wrap items-center gap-1.5 text-xs text-ink-tertiary">
                    {prefilled && <><AgentMark /> {t("exceptions.resolve.prefilled")}</>}
                    <span>{t("exceptions.resolve.send_hint")}</span>
                  </p>
                  {touched && !message.trim() && <p className="text-xs text-danger-fg">{t("exceptions.msg.empty")}</p>}
                </>
              )}
            </div>
          ) : (
            <p className="text-xs text-ink-tertiary">{t("exceptions.resolve.internal")}</p>
          )}
          {error && <ErrorState error={error} compact />}
        </div>
        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>{t("action.cancel")}</Button>
          <Button onClick={submit} disabled={busy}>
            {busy ? t("state.saving") : send && visible ? t("exceptions.resolve.submit_send") : t("exceptions.resolve.submit")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
