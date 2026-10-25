// Ops actions on a shipment: change status (allowed transitions + reason), record milestone, holds, cancel, internal notes.
import React, { useState } from "react";
import { ChevronDown, Lock } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { tEnum, useT } from "@/lib/i18n";
import { HOLD_TYPES, RECORDABLE_MILESTONES, TRANSITIONS, dateTime, holdLabel, msLabel } from "@/features/shipments/common";

const toLocalInput = (d) => {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
};

function allowed(s) {
  const a = Array.isArray(s.allowed_transitions) ? s.allowed_transitions : TRANSITIONS[s.status] || [];
  return a.filter((x) => x !== "cancelled");
}

export function canCancel(s) {
  const a = Array.isArray(s.allowed_transitions) ? s.allowed_transitions : TRANSITIONS[s.status] || [];
  return a.includes("cancelled");
}

/** Header actions: primary "Record milestone" + menu with status, hold, cancel. */
export function OpsActionBar({ s, onChanged }) {
  const { t } = useT();
  const { can } = useAuth();
  const [open, setOpen] = useState(null);
  const write = can("shipments.write");
  const locked = ["delivered", "cancelled"].includes(s.status);
  if (!write) return null;
  const close = () => setOpen(null);
  const done = () => { setOpen(null); onChanged?.(); };
  return (
    <>
      {!locked && <Button size="sm" onClick={() => setOpen("milestone")}>{t("shipments.action.milestone")}</Button>}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="sm" variant="outline">{t("shipments.actions")} <ChevronDown /></Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem disabled={allowed(s).length === 0} onSelect={() => setOpen("status")}>{t("shipments.action.status")}</DropdownMenuItem>
          <DropdownMenuItem disabled={locked} onSelect={() => setOpen("hold")}>{t("shipments.action.hold")}</DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem disabled={!canCancel(s)} className="text-danger-fg" onSelect={() => setOpen("cancel")}>{t("shipments.action.cancel")}</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {open === "status" && <StatusDialog s={s} onClose={close} onDone={done} />}
      {open === "milestone" && <MilestoneDialog s={s} onClose={close} onDone={done} />}
      {open === "hold" && <HoldDialog s={s} onClose={close} onDone={done} />}
      {open === "cancel" && <CancelDialog s={s} onClose={close} onDone={done} />}
    </>
  );
}

function useSubmit(fn, onDone) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const submit = async (e) => {
    e?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await fn();
      onDone();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, submit };
}

function StatusDialog({ s, onClose, onDone }) {
  const { t } = useT();
  const opts = allowed(s);
  const [status, setStatus] = useState(opts[0] || "");
  const [note, setNote] = useState("");
  const needNote = true; // a reason is always recorded with a manual status change
  const f = useSubmit(() => apiPost(`/shipments/${s.id}/status`, { status, note: note.trim() || null }), onDone);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("shipments.action.status")}</DialogTitle>
          <DialogDescription className="flex items-center gap-2">
            {s.reference} <StatusBadge group="status" value={s.status} size="sm" /> →{status ? <StatusBadge group="status" value={status} size="sm" /> : null}
          </DialogDescription>
        </DialogHeader>
        {opts.length === 0 ? (
          <p className="text-sm text-ink-secondary">{t("shipments.form.no_transitions", { status: tEnum(t, "status", s.status) })}</p>
        ) : (
          <form onSubmit={f.submit} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <Label htmlFor="st-new">{t("shipments.form.status")}</Label>
              <Select value={status} onValueChange={setStatus}>
                <SelectTrigger id="st-new"><SelectValue /></SelectTrigger>
                <SelectContent>{opts.map((o) => <SelectItem key={o} value={o}>{tEnum(t, "status", o)}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="st-note">{t("shipments.form.reason")}</Label>
              <Textarea id="st-note" rows={3} value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} required={needNote} />
              <p className="text-xs text-ink-tertiary">{t("shipments.form.note_required")}</p>
            </div>
            {f.error && <ErrorState error={f.error} compact />}
            <DialogFooter>
              <Button variant="outline" onClick={onClose}>{t("action.cancel")}</Button>
              <Button type="submit" disabled={f.busy || !status || note.trim().length < 3}>{f.busy ? t("state.saving") : t("action.confirm")}</Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

function MilestoneDialog({ s, onClose, onDone }) {
  const { t } = useT();
  const done = new Set((s.milestones || []).filter((m) => m.at).map((m) => `${m.code}:${m.leg_seq ?? ""}`));
  const next = (s.milestones || []).find((m) => !m.at && RECORDABLE_MILESTONES.includes(m.code));
  const [code, setCode] = useState(next?.code || "note");
  const [leg, setLeg] = useState(next?.leg_seq ? String(next.leg_seq) : "any");
  const [at, setAt] = useState(toLocalInput(new Date()));
  const [note, setNote] = useState("");
  const f = useSubmit(() => apiPost(`/shipments/${s.id}/milestones`, {
    code, note: note.trim() || null, at: at ? new Date(at).toISOString() : null, leg_seq: leg === "any" ? null : Number(leg),
  }), onDone);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("shipments.action.milestone")}</DialogTitle>
          <DialogDescription>{s.reference}</DialogDescription>
        </DialogHeader>
        <form onSubmit={f.submit} className="flex flex-col gap-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor="ms-code">{t("shipments.form.milestone")}</Label>
              <Select value={code} onValueChange={setCode}>
                <SelectTrigger id="ms-code"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {RECORDABLE_MILESTONES.map((c) => (
                    <SelectItem key={c} value={c}>{msLabel(t, { code: c })}{done.has(`${c}:`) ? " ✓" : ""}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="ms-leg">{t("shipments.form.leg")}</Label>
              <Select value={leg} onValueChange={setLeg}>
                <SelectTrigger id="ms-leg"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="any">{t("shipments.form.leg_any")}</SelectItem>
                  {(s.legs || []).map((l) => (
                    <SelectItem key={l.seq} value={String(l.seq)}>{l.seq}. {l.from?.name} → {l.to?.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="ms-at">{t("shipments.form.at")}</Label>
            <Input id="ms-at" type="datetime-local" value={at} max={toLocalInput(new Date())} onChange={(e) => setAt(e.target.value)} />
            <p className="text-xs text-ink-tertiary">{t("shipments.form.at_hint")}</p>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="ms-note">{t("shipments.form.note")}</Label>
            <Textarea id="ms-note" rows={2} value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} />
          </div>
          {f.error && <ErrorState error={f.error} compact />}
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>{t("action.cancel")}</Button>
            <Button type="submit" disabled={f.busy || (code === "note" && !note.trim())}>{f.busy ? t("state.saving") : t("action.save")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function HoldDialog({ s, onClose, onDone }) {
  const { t } = useT();
  const [type, setType] = useState("customs");
  const [reason, setReason] = useState("");
  const f = useSubmit(() => apiPost(`/shipments/${s.id}/holds`, { type, reason: reason.trim() }), onDone);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("shipments.action.hold")}</DialogTitle>
          <DialogDescription>{s.reference}</DialogDescription>
        </DialogHeader>
        <form onSubmit={f.submit} className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="hold-type">{t("shipments.form.hold_type")}</Label>
            <Select value={type} onValueChange={setType}>
              <SelectTrigger id="hold-type"><SelectValue /></SelectTrigger>
              <SelectContent>{HOLD_TYPES.map((h) => <SelectItem key={h} value={h}>{holdLabel(t, h)}</SelectItem>)}</SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="hold-reason">{t("shipments.form.reason")}</Label>
            <Textarea id="hold-reason" rows={3} value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} required />
          </div>
          {f.error && <ErrorState error={f.error} compact />}
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>{t("action.cancel")}</Button>
            <Button type="submit" disabled={f.busy || reason.trim().length < 2}>{f.busy ? t("state.saving") : t("action.save")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function CancelDialog({ s, onClose, onDone }) {
  const { t } = useT();
  const [reason, setReason] = useState("");
  const f = useSubmit(() => apiPost(`/shipments/${s.id}/cancel`, { reason: reason.trim() }), onDone);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("shipments.action.cancel")}</DialogTitle>
          <DialogDescription>{t("shipments.form.cancel_warning")}</DialogDescription>
        </DialogHeader>
        <form onSubmit={f.submit} className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="cancel-reason">{t("shipments.form.cancel_reason")}</Label>
            <Textarea id="cancel-reason" rows={3} value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} required />
          </div>
          {f.error && <ErrorState error={f.error} compact />}
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>{t("action.back")}</Button>
            <Button type="submit" variant="destructive" disabled={f.busy || reason.trim().length < 3}>{f.busy ? t("state.saving") : t("shipments.action.cancel")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Internal notes (ops only; never shown to customers). */
export function InternalNotes({ s, onAdded }) {
  const { t, locale } = useT();
  const [text, setText] = useState("");
  const [items, setItems] = useState(null);
  const notes = items || s.internal_notes || [];
  const f = useSubmit(async () => {
    const n = await apiPost(`/shipments/${s.id}/notes`, { text: text.trim() });
    setItems([n, ...notes]);
    setText("");
    onAdded?.();
  }, () => {});
  if (!Array.isArray(s.internal_notes) && !items) return null;
  return (
    <Section title={<span className="inline-flex items-center gap-1.5"><Lock className="size-3.5 text-ink-tertiary" aria-hidden />{t("shipments.notes.title")}</span>}>
      <form onSubmit={f.submit} className="mb-3 flex flex-col gap-2">
        <Label htmlFor="note-text" className="sr-only">{t("shipments.notes.title")}</Label>
        <Textarea id="note-text" rows={2} value={text} maxLength={2000} placeholder={t("shipments.notes.placeholder")} onChange={(e) => setText(e.target.value)} />
        {f.error && <ErrorState error={f.error} compact />}
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs text-ink-tertiary">{t("shipments.notes.hint")}</p>
          <Button size="sm" type="submit" disabled={f.busy || !text.trim()}>{f.busy ? t("state.saving") : t("shipments.notes.add")}</Button>
        </div>
      </form>
      {notes.length === 0 ? (
        <p className="text-sm text-ink-tertiary">{t("shipments.notes.empty")}</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {notes.map((n) => (
            <li key={n.id} className="text-sm">
              <p className="whitespace-pre-wrap">{n.text}</p>
              <p className="text-xs text-ink-tertiary">{[n.by?.name || n.by, dateTime(n.at, locale)].filter(Boolean).join(" · ")}</p>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
