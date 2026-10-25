// Broker records Tulli's answer (manual channel): event, MRN, note, optional time and file. POST /customs/{id}/record-response.
import React, { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import DateInput from "@/components/ui/DateInput";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import TimeInput from "@/components/ui/TimeInput";
import { FileUpload } from "@/components/app/FileUpload";
import { ErrorState } from "@/components/app/states";
import { apiUpload } from "@/lib/api";
import { useT } from "@/lib/i18n";

// Responses that the state machine accepts from each status (services/customs.py TRANSITIONS).
export const RESPONSES = {
  approved: ["submitted", "accepted", "released", "rejected"],
  submitted: ["accepted", "released", "under_control", "rejected", "not_released"],
  accepted: ["released", "under_control", "not_released", "invalidated"],
  under_control: ["released", "not_released", "invalidated"],
  rejected: ["submitted", "invalidated"],
  released: ["closed"],
  not_released: ["closed"],
  invalidated: ["closed"],
};
const MRN_RE = /^\d{2}FI[A-Z0-9]{12}[A-Z]\d$/;
const NEEDS_MRN = ["accepted", "released"];

// UCC MRN check digit: ISO 6346 over the first 17 characters (same as services/customs.py mrn_check_digit).
function iso6346(ch) {
  if (/\d/.test(ch)) return Number(ch);
  const v = 10 + ch.charCodeAt(0) - 65;
  return v + Math.floor((v - 1) / 10);
}
export function mrnCheckDigit(mrn17) {
  return String(([...mrn17.toUpperCase()].reduce((a, c, i) => a + iso6346(c) * 2 ** i, 0) % 11) % 10);
}
const NEEDS_NOTE = ["rejected", "not_released", "under_control"];

export default function RecordResponseDialog({ decl, open, onOpenChange, onDone }) {
  const { t } = useT();
  const options = useMemo(() => RESPONSES[decl?.status] || [], [decl?.status]);
  const [event, setEvent] = useState(options[0] || "");
  const [mrn, setMrn] = useState(decl?.mrn || "");
  const [note, setNote] = useState("");
  const [atDate, setAtDate] = useState("");
  const [atTime, setAtTime] = useState("");
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    if (open) {
      setEvent(options[0] || "");
      setMrn(decl?.mrn || "");
      setNote("");
      setAtDate("");
      setAtTime("");
      setFile(null);
      setError(null);
      setTouched(false);
    }
  }, [open, options, decl?.mrn]);

  const mrnClean = mrn.replace(/\s/g, "").toUpperCase();
  const mrnErr = mrnClean ? (MRN_RE.test(mrnClean) ? null : t("customs.rr.mrn_invalid"))
    : NEEDS_MRN.includes(event) && !decl?.mrn ? t("customs.rr.mrn_required") : null;
  const noteErr = NEEDS_NOTE.includes(event) && !note.trim() ? t("customs.rr.note_required") : null;
  // Time of Tulli's response: local date + 24 h time; both or neither (empty = now)
  const atErr = (atDate && !atTime) || (!atDate && atTime) ? t("customs.rr.at_both") : null;
  const at = atDate && atTime ? `${atDate}T${atTime}` : "";
  const todayLocal = (() => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  })();
  // Typo hints (advisory, like the backend): the broker copies the MRN from Tulli's decision.
  const mrnHints = mrnClean && !mrnErr ? [
    mrnCheckDigit(mrnClean.slice(0, 17)) !== mrnClean[17] ? t("customs.rr.check_digit", { got: mrnClean[17], want: mrnCheckDigit(mrnClean.slice(0, 17)) }) : null,
    !["R", "S", "W"].includes(mrnClean[16]) ? t("customs.rr.procedure", { l: mrnClean[16] }) : null,
  ].filter(Boolean) : [];
  const showMrn = ["accepted", "released", "submitted", "under_control", "rejected"].includes(event) || !!decl?.mrn;

  const submit = async (e) => {
    e.preventDefault();
    setTouched(true);
    if ((showMrn && mrnErr) || noteErr || atErr || !event) return;
    const f = new FormData();
    f.append("event", event);
    if (mrnClean) f.append("mrn", mrnClean);
    if (note.trim()) f.append("note", note.trim());
    if (at) f.append("at", new Date(at).toISOString().replace(/\.\d{3}Z$/, "Z"));
    if (file) f.append("file", file);
    setBusy(true);
    setError(null);
    try {
      const d = await apiUpload(`/customs/${decl.id}/record-response`, f);
      onDone?.(d);
      onOpenChange(false);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t("customs.rr.title")}</DialogTitle>
          <DialogDescription>{t("customs.rr.body", { lrn: decl?.lrn })}</DialogDescription>
        </DialogHeader>
        {options.length === 0 ? (
          <p className="text-sm text-ink-secondary">{t("customs.rr.none")}</p>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="rr-event">{t("customs.rr.event")}</Label>
              <Select value={event} onValueChange={setEvent}>
                <SelectTrigger id="rr-event"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {options.map((o) => <SelectItem key={o} value={o}>{t(`customs.rr.event.${o}`)}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            {showMrn && (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="rr-mrn">{t("customs.rr.mrn")}</Label>
                <Input id="rr-mrn" value={mrn} onChange={(e) => setMrn(e.target.value)} placeholder="26FI…" autoComplete="off"
                  className="font-mono uppercase" aria-invalid={touched && !!mrnErr} aria-describedby="rr-mrn-hint" />
                <p id="rr-mrn-hint" className={touched && mrnErr ? "text-xs text-danger-fg" : "text-xs text-ink-tertiary"}>
                  {touched && mrnErr ? mrnErr : t("customs.rr.mrn_hint")}
                </p>
                {mrnHints.map((h) => <p key={h} className="text-xs text-warning-fg">{h}</p>)}
              </div>
            )}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="rr-note">{t("customs.rr.note")}</Label>
              <Textarea id="rr-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("customs.rr.note_hint")}
                aria-invalid={touched && !!noteErr} rows={3} />
              {touched && noteErr && <p className="text-xs text-danger-fg">{noteErr}</p>}
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="rr-at">{t("customs.rr.at")}</Label>
              <div className="flex gap-2">
                <DateInput id="rr-at" value={atDate} onChange={setAtDate} max={todayLocal} clearable className="w-40"
                  aria-invalid={touched && !!atErr} aria-describedby={touched && atErr ? "rr-at-err" : undefined} />
                <TimeInput value={atTime} onChange={setAtTime} className="w-24" aria-label={t("customs.rr.at_time")}
                  aria-invalid={touched && !!atErr} />
              </div>
              {touched && atErr ? <p id="rr-at-err" className="text-xs text-danger-fg">{atErr}</p>
                : <p className="text-xs text-ink-secondary">{t("customs.rr.at_hint")}</p>}
            </div>
            <div className="flex flex-col gap-1.5">
              <span className="text-sm font-medium">{t("customs.rr.file")}</span>
              <FileUpload onFiles={(fs) => setFile(fs[0] || null)} accept=".pdf,.xml,.png,.jpg,.jpeg" />
            </div>
            {error && <ErrorState error={error} compact />}
            <DialogFooter className="gap-2">
              <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>{t("action.cancel")}</Button>
              <Button type="submit" disabled={busy}>{busy ? t("state.saving") : t("customs.rr.submit")}</Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
