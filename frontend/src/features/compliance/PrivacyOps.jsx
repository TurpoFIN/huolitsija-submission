// Ops privacy: data-subject requests with their one-month deadline (GDPR art. 12(3)), legal holds that stop retention
// and erasure, the retention policy and its runs, and a dry run that shows what a sweep would delete without changing data.
import React, { useState } from "react";
import { Play, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { StatusBadge } from "@/components/app/StatusBadge";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, dateTime, daysUntil, num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { toast } from "sonner";
import { ApiFail, Block, fieldCls, label } from "./common";
import { policyRows } from "./logic";

const REQ_STATE = { pending_verification: "pending", verification_sent: "pending", awaiting_verification: "pending", verified: "scheduled",
  ready: "scheduled", awaiting_approval: "pending", processing: "in-progress", in_progress: "in-progress", partially_held: "success",
  completed: "success", rejected: "failed", locked: "failed", expired: "expired" };
const OPEN = (r) => !["completed", "rejected", "expired"].includes(r.status);
const SCOPES = ["company", "shipment", "invoice", "customs_declaration", "subject_email"];  // backend gdpr.HOLD_SCOPES

function Due({ r }) {
  const { t, locale } = useT();
  if (!r.due_at) return <span className="text-ink-tertiary">—</span>;
  const d = daysUntil(r.due_at);
  const late = OPEN(r) && d < 0;
  return (
    <span className="inline-flex flex-col">
      <span className="tabular whitespace-nowrap">{date(r.due_at, locale)}</span>
      {OPEN(r) && <span className={late ? "text-[11px] font-medium text-danger-fg" : "text-[11px] text-ink-tertiary"}>{late ? t("compliance.priv.overdue") : t("compliance.priv.days_left", { n: d })}</span>}
    </span>
  );
}

function Requests() {
  const { t, locale } = useT();
  const q = useApi("/privacy/requests", { pollMs: 60000 });
  return (
    <Section title={t("compliance.priv.requests")}>
      <Block q={q} isEmpty={(d) => !d.items?.length} empty={<p className="py-2 text-sm text-ink-tertiary">{t("compliance.priv.requests_empty")}</p>}>
        {(d) => (
          <div className="overflow-x-auto">
            <DataTable dense rows={[...d.items].sort((a, b) => Number(OPEN(b)) - Number(OPEN(a)) || String(a.due_at || "").localeCompare(String(b.due_at || "")))}
              rowClassName={(r) => (OPEN(r) && r.due_at && daysUntil(r.due_at) < 0 ? "shadow-[inset_2px_0_0_var(--danger-500)]" : undefined)}
              columns={[
                { key: "type", header: t("compliance.priv.col.type"), cell: (r) => label(t, "compliance.priv.type", r.type) },
                { key: "email_masked", header: t("compliance.priv.col.email"), cell: (r) => <span className="font-mono text-[13px]">{r.email_masked || "—"}</span> },
                { key: "status", header: t("compliance.priv.col.status"), cell: (r) => <StatusBadge size="sm" state={REQ_STATE[r.status] || "expired"} label={label(t, "compliance.priv.status", r.status)} /> },
                { key: "created_at", header: t("compliance.priv.col.created"), cell: (r) => <span className="whitespace-nowrap tabular">{dateTime(r.created_at, locale)}</span>, hideBelow: "md" },
                { key: "due_at", header: t("compliance.priv.col.due"), sort: (r) => r.due_at || "", cell: (r) => <Due r={r} /> },
                { key: "completed_at", header: t("compliance.priv.col.done"), cell: (r) => (r.completed_at ? <span className="whitespace-nowrap tabular">{dateTime(r.completed_at, locale)}</span> : <span className="text-ink-tertiary">—</span>), hideBelow: "lg" },
              ]} />
          </div>
        )}
      </Block>
    </Section>
  );
}

function NewHold({ onSaved, onCancel }) {
  const { t } = useT();
  const [f, setF] = useState({ scope: "company", ref_id: "", reason: "", basis: "", until: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const save = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await apiPost("/compliance/legal-holds", { ...f, until: f.until || null });
      toast.success(t("compliance.priv.hold.saved"));
      onSaved();
    } catch (ex) { setErr(ex); } finally { setBusy(false); }
  };
  return (
    <form onSubmit={save} className="mb-3 flex flex-col gap-3 rounded-lg border border-[color:var(--border-subtle)] p-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <label className="text-sm">{t("compliance.priv.hold.scope")}
          <select className={fieldCls} value={f.scope} onChange={set("scope")}>{SCOPES.map((s) => <option key={s} value={s}>{t(`compliance.scope.${s}`)}</option>)}</select>
        </label>
        <label className="text-sm">{t("compliance.priv.hold.ref")}<Input required className="mt-1 font-mono" value={f.ref_id} onChange={set("ref_id")} /></label>
        <label className="text-sm">{t("compliance.priv.hold.until")}<Input type="date" className="mt-1" value={f.until} onChange={set("until")} /></label>
        <label className="text-sm sm:col-span-2 lg:col-span-2">{t("compliance.priv.hold.reason")}<Input required className="mt-1" value={f.reason} onChange={set("reason")} /></label>
        <label className="text-sm">{t("compliance.priv.hold.basis")}<Input className="mt-1" value={f.basis} onChange={set("basis")} /></label>
      </div>
      {err && <ApiFail compact error={err} />}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || !f.ref_id.trim() || !f.reason.trim()}>{t("compliance.priv.hold.save")}</Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>{t("privacy.erase.cancel")}</Button>
      </div>
    </form>
  );
}

function Holds() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const q = useApi("/compliance/legal-holds");
  const [adding, setAdding] = useState(false);
  const [err, setErr] = useState(null);
  const release = async (h) => {
    setErr(null);
    try { await apiPost(`/compliance/legal-holds/${h.id}/release`, {}); toast.success(t("compliance.priv.hold.release_done")); q.reload(); } catch (e) { setErr(e); }
  };
  const write = can("settings.write") || can("kyc.approve");
  return (
    <Section title={t("compliance.priv.holds")} actions={write && !adding && <Button size="sm" variant="outline" onClick={() => setAdding(true)}><Plus /> {t("compliance.priv.hold.new")}</Button>}>
      <p className="mb-2 text-xs text-ink-tertiary">{t("compliance.priv.holds_hint")}</p>
      {adding && <NewHold onSaved={() => { setAdding(false); q.reload(); }} onCancel={() => setAdding(false)} />}
      {err && <ApiFail className="mb-2" compact error={err} />}
      <Block q={q} isEmpty={(d) => !d.items?.length} empty={<p className="py-2 text-sm text-ink-tertiary">{t("compliance.priv.holds_empty")}</p>}>
        {(d) => (
          <div className="overflow-x-auto">
            <DataTable dense rows={d.items} columns={[
              { key: "scope", header: t("compliance.priv.hold.scope"), cell: (h) => (
                <span className="inline-flex flex-col"><span>{label(t, "compliance.scope", h.scope)}</span><span className="font-mono text-[11px] text-ink-tertiary">{h.ref_id}</span></span>
              ) },
              { key: "reason", header: t("compliance.priv.hold.reason"), cell: (h) => (
                <span className="inline-flex flex-col"><span className="break-words">{h.reason || "—"}</span>{h.basis && <span className="text-[11px] text-ink-tertiary">{h.basis}</span>}</span>
              ) },
              { key: "until", header: t("compliance.priv.hold.until"), cell: (h) => <span className="whitespace-nowrap">{h.until ? date(h.until, locale) : t("compliance.priv.hold.open_ended")}</span>, hideBelow: "md" },
              { key: "status", header: t("compliance.priv.col.status"), cell: (h) => (h.released_at
                ? <StatusBadge size="sm" state="expired" label={`${t("compliance.priv.hold.released")} ${date(h.released_at, locale)}`} />
                : <StatusBadge size="sm" state="scheduled" label={t("compliance.priv.hold.active")} />) },
              { key: "act", header: "", sortable: false, cell: (h) => (!h.released_at && write ? <Button size="xs" variant="outline" onClick={() => release(h)}>{t("compliance.priv.hold.release")}</Button> : null) },
            ]} />
          </div>
        )}
      </Block>
    </Section>
  );
}

function Period({ r }) {
  const { t } = useT();
  if (r.period && r.period.value !== undefined) {  // API: {value, unit: "years after the end of the event year" | "months" | "days" ...}
    const u = String(r.period.unit || "");
    const unit = u.startsWith("year") ? "years" : u.startsWith("month") ? "months" : "days";
    return <span title={u}>{t(`compliance.priv.${unit}`, { n: r.period.value })}{u.includes("end") ? ` ${t("compliance.priv.from_year_end")}` : ""}</span>;
  }
  const y = r.years ?? r.retention_years;
  const dd = r.days ?? r.retention_days;
  if (y !== undefined && y !== null) return t("compliance.priv.years", { n: y });
  if (dd !== undefined && dd !== null) return t("compliance.priv.days", { n: dd });
  return <span className="text-ink-tertiary">—</span>;
}

/** "users 3 · messages 12" from a run's counts / summary object; plain text if the server sent text. */
function counts(run, locale, t) {
  if (run.totals && t) {  // retention run: totals {scanned, deleted, anonymised, held, errors}
    const x = run.totals;
    return ["deleted", "anonymised", "held", "errors"].map((k) => `${t(`compliance.priv.run.${k}`)} ${num(x[k] || 0, locale)}`).join(" · ");
  }
  const c = run.counts || run.summary || run.result;
  if (!c) return null;
  if (typeof c === "string") return c;
  return Object.entries(c).map(([k, v]) => `${k} ${typeof v === "number" ? num(v, locale) : typeof v === "object" ? num(v?.count ?? v?.n ?? Object.values(v || {}).reduce((a, b) => a + (Number(b) || 0), 0), locale) : v}`).join(" · ");
}

function Retention() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const pol = useApi("/compliance/retention/policy");
  const runs = useApi("/compliance/retention/runs");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [last, setLast] = useState(null);
  const dry = async () => {
    setBusy(true);
    setErr(null);
    try { setLast(await apiPost("/compliance/retention/run", { dry_run: true })); toast.success(t("compliance.priv.dry_run_done")); runs.reload(); } catch (e) { setErr(e); } finally { setBusy(false); }
  };
  return (
    <Section title={t("compliance.priv.retention")} actions={can("settings.write") && (
      <Button size="sm" variant="outline" disabled={busy} onClick={dry}><Play /> {busy ? t("state.loading") : t("compliance.priv.dry_run")}</Button>
    )}>
      <p className="mb-2 text-xs text-ink-tertiary">{t("compliance.priv.dry_run_hint")}</p>
      {err && <ApiFail className="mb-2" compact error={err} onRetry={dry} />}
      {last && <p className="mb-3 rounded-lg bg-secondary px-3 py-2 text-sm" aria-live="polite">{t("compliance.priv.run.dry")}: {counts(last, locale, t) || "—"}</p>}
      <div className="grid gap-x-8 xl:grid-cols-2">
        <div className="min-w-0">
          <h3 className="mb-1 text-xs font-medium text-ink-tertiary">{t("compliance.priv.policy")}</h3>
          <Block q={pol} rows={3} isEmpty={(d) => policyRows(d).length === 0} empty={<p className="py-2 text-sm text-ink-tertiary">{t("compliance.priv.policy_empty")}</p>}>
            {(d) => (
              <DataTable dense rows={policyRows(d)} rowKey={(r) => r.key || r.collection || r.category || r.label} columns={[
                { key: "label", header: t("compliance.priv.col.data"), cell: (r) => (locale === "fi" ? r.label_fi : r.label_en) || r.label || r.category || r.collection || r.key },
                { key: "period", header: t("compliance.priv.col.period"), cell: (r) => <Period r={r} /> },
                { key: "basis", header: t("compliance.priv.col.basis"), cell: (r) => <span className="text-xs text-ink-secondary">{(locale === "fi" ? r.basis_fi : r.basis_en) || r.basis || r.legal_basis || "—"}</span>, hideBelow: "md" },
              ]} />
            )}
          </Block>
        </div>
        <div className="min-w-0">
          <h3 className="mb-1 mt-4 text-xs font-medium text-ink-tertiary xl:mt-0">{t("compliance.priv.runs")}</h3>
          <Block q={runs} rows={3} isEmpty={(d) => !d.items?.length} empty={<p className="py-2 text-sm text-ink-tertiary">{t("compliance.priv.runs_empty")}</p>}>
            {(d) => (
              <DataTable dense rows={d.items} columns={[
                { key: "started_at", header: t("compliance.priv.col.started"), cell: (r) => <span className="whitespace-nowrap tabular">{dateTime(r.started_at || r.created_at, locale)}</span> },
                { key: "dry_run", header: t("compliance.priv.col.kind"), cell: (r) => (r.dry_run ? t("compliance.priv.run.dry") : t("compliance.priv.run.live")) },
                { key: "result", header: t("compliance.priv.col.result"), cell: (r) => <span className="text-xs text-ink-secondary">{r.error || counts(r, locale, t) || r.status || "—"}</span> },
              ]} />
            )}
          </Block>
        </div>
      </div>
    </Section>
  );
}

export default function PrivacyOps() {
  return (
    <>
      <Requests />
      <Holds />
      <Retention />
    </>
  );
}
