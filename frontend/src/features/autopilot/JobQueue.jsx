import React, { useRef, useState } from "react";
import { Play, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, Loadable } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { displayText } from "@/lib/localizedData";
import { useAuth } from "@/lib/auth";
import { dateTime, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { DEAD_LETTER_STATE, JOB_STATE, apError, cadence, pick } from "./shared";

const COUNT_KEYS = ["queued", "running", "retry", "done", "dead", "held"];

function ErrorText({ text }) {
  if (!text) return <span className="text-ink-tertiary">—</span>;
  return <code title={text} className="line-clamp-2 break-all font-mono text-xs text-ink-secondary">{text}</code>;
}

function Counts({ jobs, workers }) {
  const { t, locale } = useT();
  const w = workers?.[0];
  const lag = jobs?.oldest_due_lag_seconds;
  return (
    <div className="mb-6 flex flex-col gap-2">
      <dl className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
        {COUNT_KEYS.map((k) => (
          <div key={k} className="flex items-baseline gap-1.5">
            <dt className="text-ink-tertiary">{t(`ap.jobs.count.${k}`)}</dt>
            <dd className={`tabular ${k === "dead" && jobs?.[k] ? "text-danger-fg" : ""}`}>{jobs?.[k] ?? 0}</dd>
          </div>
        ))}
        <div className="flex items-baseline gap-1.5"><dt className="text-ink-tertiary">{t("ap.jobs.lag")}</dt>
          <dd className="tabular">{lag === null || lag === undefined ? t("ap.jobs.no_lag") : t("ap.jobs.lag_value", { n: Math.round(lag / 60) })}</dd></div>
      </dl>
      <p className="text-xs text-ink-tertiary">
        {w ? t("ap.jobs.worker", { host: w.host, time: relTime(w.beat_at, locale), n: w.processed ?? 0, failed: w.failed ?? 0 }) : t("ap.jobs.no_worker")}
      </p>
    </div>
  );
}

function DeadLetters({ editable, onChanged }) {
  const { t, locale } = useT();
  const [status, setStatus] = useState("open");
  const q = useApi(`/autopilot/dead-letters?status=${status}`, { pollMs: 30000 });
  const [dismiss, setDismiss] = useState(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState("");
  const [flash, setFlash] = useState("");
  const lastTrigger = useRef(null);
  const retry = async (dl) => {
    setBusy(dl.id); setError(""); setFlash("");
    try { await apiPost(`/autopilot/dead-letters/${dl.id}/retry`, {}); setFlash(t("ap.dl.retried", { job: pick(locale, dl.label_fi, dl.label_en) })); await q.reload(); onChanged(); }
    catch (e) { setError(apError(e, t)); }
    finally { setBusy(null); }
  };
  const doDismiss = async () => {
    setBusy(dismiss.id); setError("");
    try { await apiPost(`/autopilot/dead-letters/${dismiss.id}/dismiss`, { note: note.trim() }); setDismiss(null); setFlash(t("ap.dl.dismissed")); await q.reload(); onChanged(); }
    catch (e) { setError(apError(e, t)); }
    finally { setBusy(null); }
  };
  return (
    <Section id="ap-dead" title={t("ap.dl.title")} actions={<Segmented label={t("ap.dl.title")} value={status} onChange={setStatus}
      options={[{ value: "open", label: t("ap.dl.open"), count: q.data?.open }, { value: "all", label: t("ap.all") }]} />}>
      <p className="mb-2 max-w-[72ch] text-sm text-ink-secondary">{t("ap.dl.hint")}</p>
      {flash && <p role="status" className="mb-2 text-sm text-ink-secondary">{flash}</p>}
      {error && !dismiss && <p role="alert" className="mb-2 text-sm text-danger-fg">{error}</p>}
      <Loadable q={q}>
        {(d) => (
          <DataTable rows={d.items} empty={<EmptyState title={t(status === "open" ? "ap.dl.empty" : "ap.dl.empty_all")} />} columns={[
            { key: "at", header: t("ap.dl.at"), cell: (r) => <span className="whitespace-nowrap tabular">{dateTime(r.at, locale)}</span> },
            { key: "kind", header: t("ap.jobs.job"), cell: (r) => <span>{pick(locale, r.label_fi, r.label_en)}</span>, sort: (r) => r.label_fi },
            { key: "error", header: t("ap.jobs.error"), sortable: false, className: "max-w-[28rem]", cell: (r) => <ErrorText text={r.error} /> },
            { key: "attempts", header: t("ap.jobs.attempts"), align: "right", hideBelow: "md" },
            { key: "status", header: t("field.status"), cell: (r) => <StatusBadge size="sm" state={DEAD_LETTER_STATE[r.status] || "expired"} label={t(`ap.dl.status.${r.status}`)} /> },
            { key: "act", header: "", sortable: false, className: "text-right", cell: (r) => r.status === "open" && editable ? (
              <span className="inline-flex gap-1.5">
                <Button size="sm" variant="outline" disabled={busy === r.id} onClick={() => retry(r)}><RotateCcw /> {t("ap.dl.retry")}</Button>
                <Button size="sm" variant="ghost" disabled={busy === r.id} onClick={(e) => { lastTrigger.current = e.currentTarget; setNote(""); setError(""); setDismiss(r); }}>{t("ap.dl.dismiss")}</Button>
              </span>
            ) : r.status !== "open" ? <span className="text-xs text-ink-tertiary">{(r.retried_by || r.dismissed_by)?.name || ""}</span> : null },
          ]} />
        )}
      </Loadable>
      <Dialog open={!!dismiss} onOpenChange={(o) => !o && setDismiss(null)}>
        <DialogContent onCloseAutoFocus={(e) => { if (lastTrigger.current?.isConnected) { e.preventDefault(); lastTrigger.current.focus(); } }}>
          <DialogHeader>
            <DialogTitle>{t("ap.dl.dismiss_title")}</DialogTitle>
            <DialogDescription>{dismiss ? pick(locale, dismiss.label_fi, dismiss.label_en) : ""} · {t("ap.dl.dismiss_body")}</DialogDescription>
          </DialogHeader>
          <label className="flex flex-col gap-1.5 text-sm">
            <span>{t("ap.dl.note")}</span>
            <Textarea value={note} onChange={(e) => setNote(e.target.value)} className="min-h-[64px]" maxLength={500} />
          </label>
          {error && <p role="alert" className="text-sm text-danger-fg">{error}</p>}
          <DialogFooter className="gap-2">
            <Button variant="ghost" onClick={() => setDismiss(null)}>{t("action.cancel")}</Button>
            <Button disabled={note.trim().length < 5 || !!busy} onClick={doDismiss}>{busy ? t("state.saving") : t("ap.dl.dismiss")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Section>
  );
}

function Schedules({ editable, onChanged }) {
  const { t, locale } = useT();
  const q = useApi("/autopilot/schedules", { pollMs: 30000 });
  const [busy, setBusy] = useState(null);
  const [msg, setMsg] = useState(null);
  const run = async (s) => {
    setBusy(s.name); setMsg(null);
    try { await apiPost(`/autopilot/schedules/${encodeURIComponent(s.name)}/run`, {}); setMsg({ ok: true, text: t("ap.sched.queued", { name: pick(locale, s.label_fi, s.label_en) }) }); await q.reload(); onChanged(); }
    catch (e) { setMsg({ ok: false, text: apError(e, t) }); }
    finally { setBusy(null); }
  };
  return (
    <Section title={t("ap.sched.title")}>
      {msg && <p role={msg.ok ? "status" : "alert"} className={`mb-2 text-sm ${msg.ok ? "text-ink-secondary" : "text-danger-fg"}`}>{msg.text}</p>}
      <Loadable q={q}>
        {(d) => (
          <DataTable rows={d.items} rowKey="name" empty={<EmptyState title={t("ap.sched.empty")} />} columns={[
            { key: "label", header: t("ap.sched.name"), cell: (s) => <span>{pick(locale, s.label_fi, s.label_en)}{s.enabled === false && <span className="ml-2 text-xs text-ink-tertiary">{t("ap.sched.disabled")}</span>}</span>, sort: (s) => s.label_fi },
            { key: "cadence", header: t("ap.sched.cadence"), hideBelow: "md", sortable: false, cell: (s) => <span className="whitespace-nowrap text-ink-secondary">{cadence(t, s)}</span> },
            { key: "next_run_at", header: t("ap.sched.next"), cell: (s) => <span className="whitespace-nowrap tabular" title={dateTime(s.next_run_at, locale)}>{relTime(s.next_run_at, locale)}</span> },
            { key: "last", header: t("ap.sched.last"), sort: (s) => s.last_run_at || "", cell: (s) => s.last_job ? (
              <span className="flex flex-wrap items-center gap-2">
                <StatusBadge size="sm" state={JOB_STATE[s.last_job.status] || "expired"} label={t(`ap.jobs.status.${s.last_job.status}`)} />
                <span className="whitespace-nowrap text-xs text-ink-tertiary">{relTime(s.last_job.finished_at || s.last_run_at, locale)}</span>
                {s.last_job.last_error && <ErrorText text={s.last_job.last_error} />}
              </span>
            ) : <span className="text-xs text-ink-tertiary">{t("ap.sched.never")}</span> },
            { key: "runs", header: t("ap.sched.runs"), align: "right", hideBelow: "lg" },
            { key: "act", header: "", sortable: false, className: "text-right", cell: (s) => editable ? (
              <Button size="sm" variant="outline" disabled={busy === s.name} onClick={() => run(s)}><Play /> {t("ap.sched.run")}</Button>
            ) : null },
          ]} />
        )}
      </Loadable>
    </Section>
  );
}

function RecentJobs() {
  const { t, locale } = useT();
  const [status, setStatus] = useState(null);
  const q = useApi(`/autopilot/jobs?limit=100${status ? `&status=${status}` : ""}`, { pollMs: 30000 });
  return (
    <Section title={t("ap.jobs.recent")}>
      <Segmented className="mb-2" label={t("field.status")} value={status} onChange={setStatus} options={[
        { value: null, label: t("ap.all") }, ...["queued", "running", "retry", "done", "dead"].map((s) => ({ value: s, label: t(`ap.jobs.status.${s}`) })),
      ]} />
      <Loadable q={q}>
        {(d) => (
          <DataTable rows={d.items} pageSize={25} empty={<EmptyState title={t("ap.jobs.empty")} />} columns={[
            { key: "created_at", header: t("ap.jobs.created"), cell: (j) => <span className="whitespace-nowrap tabular">{dateTime(j.created_at, locale)}</span> },
            { key: "kind", header: t("ap.jobs.job"), cell: (j) => pick(locale, j.label_fi, j.label_en), sort: (j) => j.label_fi },
            { key: "status", header: t("field.status"), cell: (j) => <StatusBadge size="sm" state={JOB_STATE[j.status] || "expired"} label={t(`ap.jobs.status.${j.status}`)} /> },
            { key: "attempts", header: t("ap.jobs.attempts"), align: "right", cell: (j) => `${j.attempts ?? 0}/${j.max_attempts ?? "—"}` },
            { key: "next_run_at", header: t("ap.jobs.next"), hideBelow: "md", cell: (j) => j.status === "queued" || j.status === "retry" ? <span className="whitespace-nowrap tabular">{relTime(j.next_run_at, locale)}</span> : <span className="text-ink-tertiary">—</span> },
            { key: "detail", header: t("ap.jobs.detail"), sortable: false, hideBelow: "lg", className: "max-w-[24rem]", cell: (j) => j.waiting_for
              ? <span className="text-xs text-ink-secondary">{t("ap.jobs.waiting", { why: displayText(j.waiting_for, locale) })}</span> : <ErrorText text={j.last_error} /> },
          ]} />
        )}
      </Loadable>
    </Section>
  );
}

/** Työjono: queue counts, failed jobs (dead letters) with Retry / Dismiss, schedules with "Run now", recent jobs. */
export function JobQueue({ status, onChanged }) {
  const { can } = useAuth();
  const editable = can("settings.write");
  return (
    <div>
      <Counts jobs={status.jobs} workers={status.workers} />
      <DeadLetters editable={editable} onChanged={onChanged} />
      <Schedules editable={editable} onChanged={onChanged} />
      <RecentJobs />
    </div>
  );
}
