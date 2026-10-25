import React, { useState } from "react";
import { Check, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FileUpload } from "@/components/app/FileUpload";
import { Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Loadable, LoadingRows } from "@/components/app/states";
import { apiGet, apiPost, apiUpload, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime, money, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { Readable } from "./Readable";
import { HEALTH_STATE, SEVERITY_STATE, apError, pick } from "./shared";

const HEALTH_ORDER = { fail: 0, warn: 1, ok: 2 };
const first = (o, keys) => { for (const k of keys) if (o && o[k] !== undefined && o[k] !== null) return o[k]; return undefined; };
const count = (v) => (Array.isArray(v) ? v.length : v);

function checkLabel(t, name) {
  const key = `ap.check.${name}`;
  const s = t(key);
  return s === key ? name : s;
}

function Health({ latest, onChanged }) {
  const { t, locale } = useT();
  const [fresh, setFresh] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const h = fresh || latest;
  const check = async () => {
    setBusy(true); setError(null);
    try { setFresh(await apiGet("/autopilot/health")); onChanged(); }
    catch (e) { setError(e); }
    finally { setBusy(false); }
  };
  const checks = Object.entries(h?.checks || {}).sort((a, b) => (HEALTH_ORDER[a[1].status] ?? 3) - (HEALTH_ORDER[b[1].status] ?? 3));
  return (
    <Section id="ap-health" title={t("ap.health.title")} actions={<Button size="sm" variant="outline" disabled={busy} onClick={check}><RefreshCw className={busy ? "animate-spin" : ""} /> {t("ap.health.check_now")}</Button>}>
      {error && <ErrorState className="mb-2" compact error={{ message: apError(error, t) }} onRetry={check} />}
      {!h ? <EmptyState title={t("ap.health.never")} body={t("ap.health.never_body")} /> : (
        <>
          <p className="mb-2 flex flex-wrap items-center gap-2 text-sm">
            <StatusBadge state={HEALTH_STATE[h.status] || "expired"} label={t(`ap.health.${h.status}`)} />
            <span className="text-xs text-ink-tertiary">{t("ap.health.checked", { time: relTime(h.checked_at, locale) })}</span>
          </p>
          <ul className="divide-y divide-[color:var(--border-subtle)]">
            {checks.map(([name, c]) => (
              <li key={name} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-0.5 py-2 sm:grid-cols-[12rem_7rem_minmax(0,1fr)]">
                <span className="text-sm">{checkLabel(t, name)}</span>
                <span className="justify-self-end sm:justify-self-start"><StatusBadge size="sm" state={HEALTH_STATE[c.status] || "expired"} label={t(`ap.check_state.${c.status}`)} /></span>
                <span className="col-span-2 text-sm text-ink-secondary sm:col-span-1">{pick(locale, c.fi, c.en)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </Section>
  );
}

function Alerts({ onChanged }) {
  const { t, locale } = useT();
  const [status, setStatus] = useState("open");
  const q = useApi(`/autopilot/alerts?status=${status}`, { pollMs: 30000 });
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState("");
  const ack = async (a) => {
    setBusy(a.id); setError("");
    try { await apiPost(`/autopilot/alerts/${a.id}/ack`, {}); await q.reload(); onChanged(); }
    catch (e) { setError(apError(e, t)); }
    finally { setBusy(null); }
  };
  return (
    <Section id="ap-alerts" title={t("ap.alerts.title")} actions={<Segmented label={t("ap.alerts.title")} value={status} onChange={setStatus}
      options={[{ value: "open", label: t("ap.alerts.open") }, { value: "all", label: t("ap.all") }]} />}>
      {error && <p role="alert" className="mb-2 text-sm text-danger-fg">{error}</p>}
      <Loadable q={q} isEmpty={(d) => d.items.length === 0} empty={<EmptyState title={t(status === "open" ? "ap.alerts.empty" : "ap.alerts.empty_all")} />}>
        {(d) => (
          <ul className="divide-y divide-[color:var(--border-subtle)]">
            {d.items.map((a) => {
              const title = pick(locale, a.title_fi, a.title);
              const body = pick(locale, a.body_fi, a.body);
              return (
                <li key={a.id} className="flex flex-wrap items-start gap-x-3 gap-y-1 py-2.5">
                  <StatusBadge size="sm" state={a.status === "resolved" ? "success" : SEVERITY_STATE[a.severity] || "pending"}
                    label={a.status === "resolved" ? t("ap.alerts.resolved") : t(`ap.severity.${a.severity}`)} />
                  <div className="min-w-0 flex-1 basis-60">
                    <p className="text-sm">{title}</p>
                    {body && body !== title && <p className="text-sm text-ink-secondary">{body}</p>}
                    <p className="mt-0.5 text-xs text-ink-tertiary">
                      {t("ap.alerts.opened", { time: relTime(a.opened_at, locale) })}
                      {a.count > 1 ? ` · ${t("ap.alerts.seen", { n: a.count })}` : ""}
                      {a.acknowledged_at ? ` · ${t("ap.alerts.acked", { name: a.acknowledged_by?.name || "—", time: dateTime(a.acknowledged_at, locale) })}` : ""}
                      {a.resolved_at ? ` · ${t("ap.alerts.resolved_at", { time: dateTime(a.resolved_at, locale) })}` : ""}
                    </p>
                  </div>
                  {a.status === "open" && !a.acknowledged_at && (
                    <Button size="sm" variant="outline" disabled={busy === a.id} onClick={() => ack(a)}><Check /> {t("ap.alerts.ack")}</Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Loadable>
    </Section>
  );
}

function ValueTable({ rows }) {
  return (
    <table className="w-full text-sm">
      <tbody className="divide-y divide-[color:var(--border-subtle)]">
        {rows.filter(Boolean).map((r) => (
          <tr key={r.label}>
            <th scope="row" className="py-1.5 pr-4 text-left font-normal text-ink-secondary">{r.label}</th>
            <td className="py-1.5 text-right tabular">{r.value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Digest() {
  const { t, locale } = useT();
  const q = useApi("/autopilot/reports?kind=digest&limit=1");
  const m = (c) => money(c, locale, { decimals: 2 });
  return (
    <Section title={t("ap.digest.title")} className="min-w-0" actions={<span aria-hidden className="h-7" />}>
      <Loadable q={q} isEmpty={(d) => !d.items.length} empty={<EmptyState title={t("ap.digest.empty")} body={t("ap.digest.empty_body")} />}>
        {(d) => {
          const r = d.items[0];
          const x = r.data || {};
          const dec = x.decisions || {};
          return (
            <>
              <p className="mb-2 text-xs text-ink-tertiary">
                {t("ap.digest.made", { day: r.day || "—", time: dateTime(r.created_at, locale) })} · {t("ap.digest.sent", { n: (r.sent_to || []).length })}
              </p>
              <ValueTable rows={[
                { label: t("ap.digest.decisions"), value: t("ap.digest.decisions_value", { auto: dec.auto ?? 0, owner: dec.owner_queue ?? 0, blocked: dec.blocked ?? 0 }) },
                { label: t("ap.digest.pending"), value: t("ap.digest.pending_value", { n: count(x.pending) ?? 0, late: count(x.overdue) ?? 0 }) },
                x.shipments && { label: t("ap.digest.shipments"), value: t("ap.digest.shipments_value", { booked: x.shipments.booked ?? 0, delivered: x.shipments.delivered ?? 0, active: x.shipments.active ?? 0 }) },
                x.invoices && { label: t("ap.digest.issued", { n: x.invoices.issued ?? 0 }), value: m(x.invoices.issued_cents) },
                x.invoices && { label: t("ap.digest.paid"), value: m(x.invoices.paid_cents) },
                x.invoices && { label: t("ap.digest.overdue"), value: m(x.invoices.overdue_cents) },
                { label: t("ap.digest.spend"), value: `${m(x.spend_today_cents)} / ${m(x.spend_limit_cents)}` },
                { label: t("ap.digest.alerts"), value: count(x.alerts) ?? 0 },
                { label: t("ap.digest.dead"), value: x.dead_letters ?? 0 },
              ]} />
            </>
          );
        }}
      </Loadable>
    </Section>
  );
}

function Reconciliation({ editable }) {
  const { t, locale } = useT();
  const q = useApi("/autopilot/reports?kind=reconciliation&limit=1");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const run = async () => {
    setBusy(true); setMsg(null);
    try { await apiPost("/autopilot/reconciliation/run", {}); setMsg({ ok: true, text: t("ap.rec.started") }); await q.reload(); }
    catch (e) { setMsg({ ok: false, text: e?.status === 404 || e?.status === 405 ? t("ap.rec.unavailable") : apError(e, t) }); }
    finally { setBusy(false); }
  };
  return (
    <Section title={t("ap.rec.title")} className="min-w-0" actions={editable ? <Button size="sm" variant="outline" disabled={busy} onClick={run}><RefreshCw className={busy ? "animate-spin" : ""} /> {t("ap.rec.run")}</Button> : null}>
      {msg && <p role={msg.ok ? "status" : "alert"} className={`mb-2 text-sm ${msg.ok ? "text-ink-secondary" : "text-danger-fg"}`}>{msg.text}</p>}
      <Loadable q={q} isEmpty={(d) => !d.items.length} empty={<EmptyState title={t("ap.rec.empty")} body={t("ap.rec.empty_body")} />}>
        {(d) => {
          const r = d.items[0];
          const ok = r.status === "ok";
          const summary = r.summary || r.data;
          return (
            <>
              <p className="mb-2 flex flex-wrap items-center gap-2">
                <StatusBadge size="sm" state={ok ? "success" : "pending"} label={t(ok ? "ap.rec.ok" : "ap.rec.diff")} />
                <span className="text-xs text-ink-tertiary">{dateTime(r.created_at, locale)}</span>
              </p>
              {summary && typeof summary === "object" && <details className="text-sm"><summary className="cursor-pointer text-ink-secondary">{t("ap.rec.details")}</summary><div className="mt-2"><Readable value={summary} /></div></details>}
            </>
          );
        }}
      </Loadable>
    </Section>
  );
}

function BankStatements({ editable }) {
  const { t, locale } = useT();
  const q = useApi("/autopilot/bank-statements");
  const tx = useApi("/autopilot/bank-transactions?status=unmatched");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const missing = q.error?.status === 404 || q.error?.status === 405;
  const upload = async (files) => {
    const f = files[0];
    if (!f) return;
    setBusy(true); setError(""); setResult(null);
    const form = new FormData();
    form.append("file", f);
    try { const r = await apiUpload("/autopilot/bank-statements", form); setResult({ name: f.name, r }); q.reload(); tx.reload(); }
    catch (e) { setError(e?.status === 404 || e?.status === 405 ? t("ap.bank.unavailable") : apError(e, t)); }
    finally { setBusy(false); }
  };
  const r = result?.r || {};
  const entries = count(first(r, ["entries", "transactions", "count", "entry_count"]));
  const matched = count(first(r, ["matched", "matched_count"]));
  const unmatched = count(first(r, ["unmatched", "unmatched_count"]));
  const duplicates = count(first(r, ["duplicates", "duplicate_count", "skipped"]));
  return (
    <Section title={t("ap.bank.title")}>
      <p className="mb-2 max-w-[72ch] text-sm text-ink-secondary">{t("ap.bank.hint")}</p>
      {editable && !missing && <FileUpload accept=".xml,application/xml,text/xml" onFiles={upload} busy={busy} label={t("ap.bank.choose")} hint={t("ap.bank.drop")} className="mb-3" />}
      {error && <p role="alert" className="mb-2 text-sm text-danger-fg">{error}</p>}
      {result && (
        <div role="status" className="mb-3 rounded-lg border border-[color:var(--border-subtle)] p-3 text-sm">
          <p className="mb-1 flex flex-wrap items-center gap-2"><StatusBadge size="sm" state="success" label={t("ap.bank.read")} /><span className="break-all">{result.name}</span></p>
          <p className="text-ink-secondary">
            {[entries !== undefined && t("ap.bank.entries", { n: entries }), matched !== undefined && t("ap.bank.matched", { n: matched }),
              unmatched !== undefined && t("ap.bank.unmatched", { n: unmatched }), duplicates !== undefined && t("ap.bank.duplicates", { n: duplicates })].filter(Boolean).join(" · ") || t("ap.bank.processed")}
          </p>
          <details className="mt-1"><summary className="cursor-pointer text-ink-secondary">{t("ap.rec.details")}</summary><div className="mt-2"><Readable value={r} /></div></details>
        </div>
      )}
      {missing ? <EmptyState title={t("ap.bank.unavailable")} body={t("ap.bank.unavailable_body")} />
        : q.loading && !q.data ? <LoadingRows rows={3} />
        : q.error && !q.data ? <ErrorState error={q.error} onRetry={q.reload} />
        : q.data && (
          <DataTable rows={q.data.items || []} empty={<EmptyState title={t("ap.bank.empty")} />} columns={[
            { key: "at", header: t("ap.bank.uploaded"), sort: (s) => first(s, ["created_at", "uploaded_at", "at"]), cell: (s) => <span className="whitespace-nowrap tabular">{dateTime(first(s, ["created_at", "uploaded_at", "at"]), locale)}</span> },
            { key: "file", header: t("ap.bank.file"), sortable: false, cell: (s) => <span className="break-all">{first(s, ["filename", "file_name", "name"]) || "—"}</span> },
            { key: "entries", header: t("ap.bank.col_entries"), align: "right", cell: (s) => count(first(s, ["entries", "entry_count", "transactions", "count"])) ?? "—" },
            { key: "matched", header: t("ap.bank.col_matched"), align: "right", hideBelow: "md", cell: (s) => count(first(s, ["matched", "matched_count"])) ?? "—" },
            { key: "unmatched", header: t("ap.bank.col_unmatched"), align: "right", cell: (s) => count(first(s, ["unmatched", "unmatched_count"])) ?? "—" },
          ]} />
        )}
      {tx.data && (tx.data.items || []).length > 0 && (
        <div className="mt-4">
          <h3 className="mb-2 text-sm font-medium">{t("ap.bank.unmatched_title")}</h3>
          <DataTable rows={tx.data.items} columns={[
            { key: "date", header: t("ap.bank.date"), cell: (x) => <span className="whitespace-nowrap tabular">{dateTime(first(x, ["booking_date", "value_date", "booked_at", "at", "created_at"]), locale)}</span> },
            { key: "ref", header: t("ap.bank.reference"), sortable: false, cell: (x) => <span className="break-all font-mono text-xs">{first(x, ["reference", "ref", "end_to_end_id"]) || "—"}</span> },
            { key: "payer", header: t("ap.bank.payer"), hideBelow: "md", sortable: false, cell: (x) => first(x, ["debtor_name", "payer", "name"]) || "—" },
            { key: "amount", header: t("ap.bank.amount"), align: "right", sort: (x) => first(x, ["amount_cents"]), cell: (x) => money(first(x, ["amount_cents"]), locale, { decimals: 2 }) },
          ]} />
        </div>
      )}
    </Section>
  );
}

/** Valvonta: health checks, owner alerts, the latest digest and reconciliation, and camt.054 bank statements. */
export function Monitoring({ status, onChanged }) {
  const { can } = useAuth();
  const editable = can("settings.write");
  return (
    <div>
      <Health latest={status.health} onChanged={onChanged} />
      <Alerts onChanged={onChanged} />
      <div className="grid gap-x-8 lg:grid-cols-2">
        <Digest />
        <Reconciliation editable={editable} />
      </div>
      <BankStatements editable={editable} />
    </div>
  );
}
