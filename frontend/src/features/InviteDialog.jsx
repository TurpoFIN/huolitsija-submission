import React, { useState } from "react";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorState } from "@/components/app/states";
import { apiPost } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { toast } from "sonner";

/** Invite dialog shared by customer Team and ops Team & roles. roles: string[]; carriers: [{id,name}] for partner role. */
export function InviteDialog({ endpoint, roles, carriers, title, onDone, trigger }) {
  const { t, locale } = useT();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ email: "", role: roles[roles.length - 1], carrier_id: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiPost(endpoint, { email: form.email, role: form.role, carrier_id: form.role === "partner" ? form.carrier_id : null, locale });
      toast.success(t("team.invite_sent", { email: form.email }));
      setOpen(false);
      setForm({ email: "", role: roles[roles.length - 1], carrier_id: "" });
      onDone?.();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>{title}</DialogTitle></DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <label className="text-sm font-medium" htmlFor="inv-email">{t("field.email")}</label>
          <Input id="inv-email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} autoFocus />
          <label className="text-sm font-medium">{t("field.role")}</label>
          <Select value={form.role} onValueChange={(v) => setForm({ ...form, role: v })}>
            <SelectTrigger aria-label={t("field.role")}><SelectValue /></SelectTrigger>
            <SelectContent>{roles.map((r) => <SelectItem key={r} value={r}>{tEnum(t, "role", r)}</SelectItem>)}</SelectContent>
          </Select>
          {form.role === "partner" && (
            <>
              <label className="text-sm font-medium">{t("staff.carrier")}</label>
              <Select value={form.carrier_id} onValueChange={(v) => setForm({ ...form, carrier_id: v })}>
                <SelectTrigger aria-label={t("staff.carrier")}><SelectValue placeholder="—" /></SelectTrigger>
                <SelectContent>{(carriers || []).map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}</SelectContent>
              </Select>
              <p className="text-xs text-ink-tertiary">{t("staff.carrier_hint")}</p>
            </>
          )}
          {error && <ErrorState compact error={error} />}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setOpen(false)}>{t("action.cancel")}</Button>
            <Button type="submit" disabled={busy || !form.email || (form.role === "partner" && !form.carrier_id)}>{t("action.invite")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
