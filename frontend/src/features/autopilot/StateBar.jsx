import React, { useRef, useState } from "react";
import { OctagonX, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { StatusBadge } from "@/components/app/StatusBadge";
import { apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime, money } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { TypedConfirm } from "./TypedConfirm";
import { apError } from "./shared";

const notifyShell = () => window.dispatchEvent(new Event("huo.autonomy"));

/** The decision at the top of the control room: is Autopilot acting, and the two controls that change it. */
export function StateBar({ status, onChanged }) {
  const { t, locale } = useT();
  const { can } = useAuth();
  const editable = can("settings.write");
  const [dialog, setDialog] = useState(null); // "enable" | "kill" | "resume"
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [flash, setFlash] = useState("");
  const switchRef = useRef(null);
  const killRef = useRef(null);
  const resumeRef = useRef(null);
  const d = status;
  const mode = d.halted ? "halted" : d.autopilot_enabled ? "on" : "off";
  const changed = d.halted ? d.halted_changed : d.changed;

  const run = async (fn, done) => {
    setBusy(true); setError("");
    try { await fn(); notifyShell(); await onChanged(); setDialog(null); setNote(""); setFlash(t(done)); }
    catch (e) { setError(apError(e, t)); }
    finally { setBusy(false); }
  };
  const enable = () => run(() => apiPost("/settings/autonomy/autopilot_enabled", { enabled: true, confirmation: "enable" }), "ap.state.saved_on");
  const disable = () => run(() => apiPost("/settings/autonomy/autopilot_enabled", { enabled: false }), "ap.state.saved_off");
  const kill = () => run(() => apiPost("/autopilot/kill", { note: note.trim() || null }), "ap.state.saved_killed");
  const resume = () => run(() => apiPost("/autopilot/resume", { confirmation: "enable" }), "ap.state.saved_resumed");

  return (
    <section aria-labelledby="ap-state" className="mb-6 border-b border-[color:var(--border-subtle)] pb-4">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0 flex-1 basis-[22rem]">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge state={mode === "on" ? "success" : mode === "halted" ? "failed" : "expired"} label={t(`ap.state.badge.${mode}`)} />
            <h2 id="ap-state" className="text-lg">{t(`ap.state.${mode}`)}</h2>
          </div>
          <p className="mt-1 max-w-[72ch] text-sm text-ink-secondary">{t(`ap.state.${mode}_hint`)}</p>
          {changed?.at && (
            <p className="mt-1 text-xs text-ink-tertiary">
              {t(d.halted ? "ap.state.halted_by" : "ap.state.changed_by", { name: changed.by?.name || "—", time: dateTime(changed.at, locale) })}
              {d.halted && changed.note ? ` · ${changed.note}` : ""}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <label htmlFor="ap-switch" className="inline-flex h-8 items-center gap-2 text-sm font-medium">
            {t("ap.state.switch")}
            <Switch id="ap-switch" ref={switchRef} className="data-[state=unchecked]:bg-[color:var(--text-disabled)]" checked={!!d.autopilot_enabled} disabled={!editable || busy}
              onCheckedChange={(v) => { setError(""); setFlash(""); if (v) setDialog("enable"); else disable(); }} />
          </label>
          <span aria-hidden className="hidden h-6 w-px bg-[color:var(--border-default)] sm:block" />
          {d.halted ? (
            <Button ref={resumeRef} variant="outline" disabled={!editable || busy} onClick={() => { setError(""); setDialog("resume"); }}>
              <Play /> {t("ap.state.resume")}
            </Button>
          ) : (
            <Button ref={killRef} variant="destructive" disabled={!editable || busy} onClick={() => { setError(""); setNote(""); setDialog("kill"); }}>
              <OctagonX /> {t("ap.state.kill")}
            </Button>
          )}
        </div>
      </div>
      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm">
        <div className="flex gap-1.5"><dt className="text-ink-tertiary">{t("ap.state.spend")}</dt>
          <dd className="tabular">{money(d.spend_today_cents, locale, { decimals: 2 })} / {money(d.guardrails?.guard_spend_per_day_cents, locale, { decimals: 2 })}</dd></div>
        <div className="flex gap-1.5"><dt className="text-ink-tertiary">{t("ap.state.decisions_today")}</dt>
          <dd className="tabular">{t("ap.state.decisions_value", { auto: d.decisions_today?.auto ?? 0, owner: d.decisions_today?.owner_queue ?? 0 })}</dd></div>
        {d.jobs?.held > 0 && <div className="flex gap-1.5"><dt className="text-ink-tertiary">{t("ap.state.held")}</dt><dd className="tabular">{d.jobs.held}</dd></div>}
      </dl>
      {!editable && <p className="mt-2 text-xs text-ink-tertiary">{t("ap.state.read_only")}</p>}
      {flash && !dialog && <p role="status" className="mt-2 text-sm text-ink-secondary">{flash}</p>}
      {error && !dialog && <p role="alert" className="mt-2 text-sm text-danger-fg">{error}</p>}

      <TypedConfirm open={dialog === "enable"} onOpenChange={(o) => !o && setDialog(null)} returnFocus={switchRef}
        title={t("ap.state.enable_title")} body={t("ap.typed.body")}
        consequence={t(d.halted ? "ap.state.enable_consequence_halted" : "ap.state.enable_consequence")}
        confirmLabel={t("ap.state.enable_confirm")} onConfirm={enable} busy={busy} error={error} />
      <TypedConfirm open={dialog === "resume"} onOpenChange={(o) => !o && setDialog(null)} returnFocus={resumeRef}
        title={t("ap.state.resume_title")} body={t("ap.typed.body")} consequence={t("ap.state.resume_consequence")}
        confirmLabel={t("ap.state.resume")} onConfirm={resume} busy={busy} error={error} />
      <Dialog open={dialog === "kill"} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent onCloseAutoFocus={(e) => { e.preventDefault(); (killRef.current || resumeRef.current)?.focus(); }}>
          <DialogHeader>
            <DialogTitle>{t("ap.state.kill_title")}</DialogTitle>
            <DialogDescription>{t("ap.state.kill_consequence")}</DialogDescription>
          </DialogHeader>
          <label className="flex flex-col gap-1.5 text-sm">
            <span>{t("ap.state.kill_note")}</span>
            <Textarea value={note} onChange={(e) => setNote(e.target.value)} className="min-h-[64px]" maxLength={500} />
          </label>
          {error && <p role="alert" className="text-sm text-danger-fg">{error}</p>}
          <DialogFooter className="gap-2">
            <Button variant="ghost" onClick={() => setDialog(null)}>{t("action.cancel")}</Button>
            <Button variant="destructive" disabled={busy} onClick={kill}><OctagonX /> {busy ? t("state.saving") : t("ap.state.kill_confirm")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
