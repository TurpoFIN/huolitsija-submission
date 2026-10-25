// Ops reports an exception by hand (POST /exceptions). The creator becomes the owner.
import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { ErrorState } from "@/components/app/states";
import { apiPost } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { Field, ShipmentPicker } from "./common";
import { SEVERITIES, SLA_HOURS, TYPES, typeLabel } from "./text";

export default function NewExceptionDialog({ open, onOpenChange, onCreated, initialShipment }) {
  const { t } = useT();
  const [shipment, setShipment] = useState(null);
  const [type, setType] = useState("delay");
  const [severity, setSeverity] = useState("medium");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [visible, setVisible] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    if (!open) return;
    setShipment(initialShipment || null);
    setType("delay");
    setSeverity("medium");
    setTitle("");
    setDescription("");
    setVisible(true);
    setError(null);
    setTouched(false);
  }, [open, initialShipment]);

  const problems = [!shipment && t("exceptions.create.need_shipment"), title.trim().length < 3 && t("exceptions.create.need_title")].filter(Boolean);

  const submit = async () => {
    setTouched(true);
    if (problems.length) return;
    setBusy(true);
    setError(null);
    try {
      const ex = await apiPost("/exceptions", {
        shipment_id: shipment.id, type, severity, title: title.trim(), description: description.trim(), customer_visible: visible,
      });
      toast.success(t("exceptions.create.done"));
      onOpenChange(false);
      onCreated?.(ex);
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
          <DialogTitle>{t("exceptions.create.title")}</DialogTitle>
          <DialogDescription className="sr-only">{t("exceptions.create.title")}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <Field label={t("exceptions.create.shipment")} htmlFor="nx-shipment" error={touched && !shipment ? t("exceptions.create.need_shipment") : null}>
            <ShipmentPicker id="nx-shipment" value={shipment} onChange={setShipment} autoFocus
              placeholder={t("exceptions.create.shipment_ph")} emptyText={t("exceptions.create.shipment_none")} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("exceptions.create.type")}>
              <Select value={type} onValueChange={setType}>
                <SelectTrigger aria-label={t("exceptions.create.type")}><SelectValue /></SelectTrigger>
                <SelectContent>
                  {TYPES.map((k) => <SelectItem key={k} value={k}>{typeLabel(t, k)}</SelectItem>)}
                </SelectContent>
              </Select>
            </Field>
            <Field label={t("exceptions.create.severity")} hint={t("exceptions.create.sla", { h: SLA_HOURS[severity] })}>
              <Select value={severity} onValueChange={setSeverity}>
                <SelectTrigger aria-label={t("exceptions.create.severity")}><SelectValue /></SelectTrigger>
                <SelectContent>
                  {SEVERITIES.map((k) => <SelectItem key={k} value={k}>{tEnum(t, "severity", k)}</SelectItem>)}
                </SelectContent>
              </Select>
            </Field>
          </div>
          <Field label={t("exceptions.create.title_field")} htmlFor="nx-title" error={touched && title.trim().length < 3 ? t("exceptions.create.need_title") : null}>
            <Input id="nx-title" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} placeholder={t("exceptions.create.title_ph")}
              aria-invalid={touched && title.trim().length < 3 ? true : undefined} />
          </Field>
          <Field label={t("exceptions.create.description")} htmlFor="nx-desc">
            <Textarea id="nx-desc" value={description} maxLength={4000} onChange={(e) => setDescription(e.target.value)}
              placeholder={t("exceptions.create.description_ph")} />
          </Field>
          <label className="flex items-start gap-2 text-sm">
            <Switch checked={visible} onCheckedChange={setVisible} className="mt-0.5" />
            <span>
              {t("exceptions.create.customer_visible")}
              <span className="block text-xs text-ink-tertiary">{visible ? t("exceptions.d.customer_visible_hint") : t("exceptions.d.internal_hint")}</span>
            </span>
          </label>
          {error && <ErrorState error={error} compact />}
        </div>
        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>{t("action.cancel")}</Button>
          <Button onClick={submit} disabled={busy}>{busy ? t("state.saving") : t("exceptions.create.submit")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
