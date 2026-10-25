// Ops › Finance › Payments (Maksut). Decision: "which payment run do I approve or send to the bank, and what is due?"
// First the payment runs (pending approval first) with approve / send / pain.001 file; the bank's pain.002 status report
// upload; then the payables (approved carrier invoices) with due date, amount in its currency + EUR and status.
// APIs: GET /finance/payables?status=, GET|POST /finance/payment-runs, POST /finance/payment-runs/{id}/{approve|send},
// GET /finance/payment-runs/{id}/file, POST /finance/bank/pain002 (multipart file).
import React, { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Banknote, FileCheck2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { DateInput } from "@/components/ui/DateInput";
import { Section } from "@/components/app/layout";
import { Segmented } from "@/components/app/FilterBar";
import { FileUpload } from "@/components/app/FileUpload";
import { EmptyState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiPost, apiUpload, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { isoDate } from "@/features/billing/invoiceFormat";
import { ActionNote, ConfirmAction, Dot, FileLink, FinBar, FinStatus, MoneyCell } from "@/features/finance/kit";
import { FinTable } from "@/features/finance/Table";
import { amount, eur, qs, signedEur, todayHelsinki } from "@/features/finance/format";

const PAYABLE_TABS = [
  { value: "open", status: "approved,scheduled,sent" },
  { value: "paid", status: "paid" },
  { value: "all", status: null },
  { value: "closed", status: "rejected,cancelled" },
];

function Totals({ totals }) {
  const { locale } = useT();
  const entries = Object.entries(totals || {});
  if (!entries.length) return <span className="text-ink-tertiary">—</span>;
  return (
    <span className="inline-flex flex-col items-end tabular whitespace-nowrap">
      {entries.map(([cur, c]) => <span key={cur}>{amount(c, cur, locale)}</span>)}
    </span>
  );
}

function Runs({ q, canWrite, onChanged }) {
  const mode = q.data?.mode || "file";
  const { t, locale } = useT();
  const [confirm, setConfirm] = useState(null); // {action, run}
  const [creating, setCreating] = useState(false);
  const [execDate, setExecDate] = useState("");
  const [note, setNote] = useState(null);
  const act = async (action, run) => {
    const r = await apiPost(`/finance/payment-runs/${run.id}/${action}`, action === "send" && mode === "file" ? { uploaded: true } : {});
    setNote({ text: t(`fin.pay.done.${action}`, { n: r?.number || run.number || "" }) });
    onChanged();
    return true;
  };
  const create = async () => {
    const r = await apiPost("/finance/payment-runs", execDate ? { execution_date: execDate } : {});
    setNote({ text: t("fin.pay.done.create", { n: r?.number || "", count: r?.count ?? 0 }) });
    onChanged();
    return true;
  };
  const columns = [
    { key: "number", header: t("fin.col.number"), sort: (r) => r.number || "", cell: (r) => <span className="font-mono text-[13px] whitespace-nowrap">{r.number || "—"}</span> },
    { key: "execution_date", header: t("fin.pay.execution"), sort: (r) => r.execution_date || "", cell: (r) => <span className="tabular whitespace-nowrap">{isoDate(r.execution_date, locale)}</span> },
    { key: "count", header: t("fin.pay.payments"), align: "right", sort: (r) => r.count, cell: (r) => r.count ?? (r.items || []).length },
    { key: "totals", header: t("fin.col.total"), align: "right", sortable: false,
      cell: (r) => (
        <span className="inline-flex flex-col items-end">
          <Totals totals={r.totals} />
          {Object.keys(r.totals || {}).some((c) => c !== "EUR") && <span className="text-[11px] text-ink-tertiary tabular">{eur(r.eur_total_cents, locale)}</span>}
        </span>
      ) },
    { key: "status", header: t("field.status"), sort: (r) => r.status || "",
      cell: (r) => (
        <span className="flex flex-col items-start gap-0.5">
          <FinStatus group="payment_run" value={r.status} />
          {r.file_id && r.xsd_valid === false && <span className="text-[11px] text-danger-fg">{t("fin.pay.xsd_invalid")}</span>}
          {r.file_id && r.xsd_valid === true && <span className="text-[11px] text-ink-tertiary">{t("fin.pay.xsd_valid", { v: r.version || "pain.001" })}</span>}
          {(r.skipped || []).length > 0 && <span className="text-[11px] text-warning-fg">{t("fin.pay.skipped_n", { n: r.skipped.length })}</span>}
          {r.bank?.group_status && <span className="text-[11px] text-ink-tertiary">{t("fin.pay.bank_status", { s: r.bank.group_status })}</span>}
        </span>
      ) },
    { key: "actions", header: "", sortable: false, className: "text-right",
      cell: (r) => (
        <span className="inline-flex flex-wrap justify-end gap-1.5">
          {r.file_id && <FileLink path={`/finance/payment-runs/${r.id}/file`} label="pain.001" />}
          {r.status === "pending_approval" && (
            <Button size="xs" disabled={!canWrite} onClick={() => setConfirm({ action: "approve", run: r })}>{t("fin.pay.approve")}</Button>
          )}
          {r.status === "approved" && (
            <Button size="xs" disabled={!canWrite} onClick={() => setConfirm({ action: "send", run: r })}>{t(mode === "api" ? "fin.pay.send" : "fin.pay.mark_sent")}</Button>
          )}
        </span>
      ) },
  ];
  const runs = q.data?.items || [];
  return (
    <Section id="runs" title={t("fin.pay.runs")}
      actions={<Button size="xs" variant="outline" disabled={!canWrite} title={!canWrite ? t("fin.no_permission") : undefined} onClick={() => setCreating(true)}><Plus /> {t("fin.pay.create")}</Button>}>
      <ActionNote note={note} onClose={() => setNote(null)} className="mb-2" />
      <Loadable q={q} skeleton={<LoadingRows rows={3} />} isEmpty={(d) => (d.items || []).length === 0}
        empty={<p className="py-2 text-sm text-ink-tertiary">{t("fin.pay.no_runs")}</p>}>
        {() => <FinTable columns={columns} rows={runs} rowKey="id"
          defaultSort={null} rowClassName={(r) => r.status === "pending_approval" && "bg-[var(--bg-subtle)]"} />}
      </Loadable>
      <ConfirmAction open={confirm?.action === "approve"} onOpenChange={(o) => !o && setConfirm(null)}
        title={t("fin.pay.confirm.approve", { n: confirm?.run?.number || "" })}
        description={confirm?.run ? t("fin.pay.confirm.approve_body", { count: confirm.run.count ?? 0, date: isoDate(confirm.run.execution_date, locale) }) : null}
        submitLabel={t("fin.pay.approve")} onSubmit={() => act("approve", confirm.run)}>
        {confirm?.run && <div className="flex justify-end text-sm font-medium"><Totals totals={confirm.run.totals} /></div>}
      </ConfirmAction>
      <ConfirmAction open={confirm?.action === "send"} onOpenChange={(o) => !o && setConfirm(null)}
        title={t("fin.pay.confirm.send", { n: confirm?.run?.number || "" })}
        description={t(mode === "api" ? "fin.pay.confirm.send_api" : "fin.pay.confirm.send_file")}
        submitLabel={t(mode === "api" ? "fin.pay.send" : "fin.pay.mark_sent")} onSubmit={() => act("send", confirm.run)}>
        {mode !== "api" && confirm?.run?.file_id && <div><FileLink path={`/finance/payment-runs/${confirm.run.id}/file`} label="pain.001" size="sm" /></div>}
      </ConfirmAction>
      <ConfirmAction open={creating} onOpenChange={setCreating} title={t("fin.pay.create")} description={t("fin.pay.create_body")}
        submitLabel={t("fin.pay.create")} onSubmit={create}>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="exec-date">{t("fin.pay.execution")}</Label>
          <DateInput id="exec-date" value={execDate} onChange={setExecDate} min={todayHelsinki()} clearable />
          <p className="text-xs text-ink-tertiary">{t("fin.pay.execution_hint")}</p>
        </div>
      </ConfirmAction>
    </Section>
  );
}

function StatusReport({ canWrite, onDone }) {
  const { t } = useT();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null);
  const upload = async (files) => {
    setBusy(true);
    setNote(null);
    try {
      const fd = new FormData();
      fd.append("file", files[0]);
      const r = await apiUpload("/finance/bank/pain002", fd);
      const run = r?.run || null;
      const out = r?.outcome || {};
      setNote({ text: t("fin.pay.pain002_done", {
        run: run?.number || "—",
        status: run?.status ? tEnum(t, "fin.payment_run", run.status) : "—",
        ok: (out.sent || []).length + (out.paid || []).length,
        rejected: (out.rejected || []).length,
      }) });
      onDone();
    } catch (e) {
      setNote({ tone: "error", text: e.message });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Section title={t("fin.pay.pain002")}>
      {canWrite ? <FileUpload onFiles={upload} busy={busy} accept=".xml" label={t("fin.pay.pain002_choose")} hint={t("fin.pay.pain002_hint")} />
        : <p className="text-sm text-ink-tertiary">{t("fin.no_permission")}</p>}
      <ActionNote note={note} onClose={() => setNote(null)} className="mt-2" />
    </Section>
  );
}

export default function Payables() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("billing.write");
  const [params, setParams] = useSearchParams();
  const tabValue = PAYABLE_TABS.some((x) => x.value === params.get("tab")) ? params.get("tab") : "open";
  const tab = PAYABLE_TABS.find((x) => x.value === tabValue);
  const focus = params.get("focus");
  const q = useApi(`/finance/payables${qs({ status: tab.status })}`, { pollMs: 60000 });
  const runs = useApi("/finance/payment-runs", { pollMs: 60000 });
  const counts = q.data?.counts || null;
  const options = useMemo(() => PAYABLE_TABS.map((x) => ({
    value: x.value, label: t(`fin.pay.tab.${x.value}`),
    count: counts ? (x.status ? x.status.split(",").reduce((a, s) => a + (counts[s] || 0), 0) : Object.values(counts).reduce((a, b) => a + b, 0)) : undefined,
  })), [counts, t]);
  const setTab = (v) => {
    const next = new URLSearchParams(params);
    if (v !== "open") next.set("tab", v); else next.delete("tab");
    setParams(next, { replace: true });
  };

  const runNumber = Object.fromEntries((runs.data?.items || []).map((r) => [r.id, r.number]));
  const today = todayHelsinki();
  const week = new Date(Date.now() + 7 * 86400000).toISOString().slice(0, 10);
  const columns = [
    { key: "supplier", header: t("fin.col.supplier"), sort: (r) => r.supplier?.name || "", className: "max-w-[16rem]",
      cell: (r) => (
        <span className="flex min-w-0 flex-col">
          <span className="truncate font-medium">{r.supplier?.name || "—"}</span>
          {r.carrier_invoice_id && (
            <Link to={`/ops/finance/carrier-invoices/${r.carrier_invoice_id}`} onClick={(e) => e.stopPropagation()} className="truncate text-[11px] text-ink-accent hover:underline">
              {r.invoice_number || t("fin.pay.invoice")}
            </Link>
          )}
        </span>
      ) },
    { key: "due_date", header: t("fin.col.due"), sort: (r) => r.due_date || "",
      cell: (r) => {
        const open = ["approved", "scheduled"].includes(r.status);
        const late = open && r.due_date && r.due_date < today;
        const soon = open && r.due_date && r.due_date >= today && r.due_date <= week;
        return <span className={cn("tabular whitespace-nowrap", late && "text-danger-fg", soon && "text-warning-fg")}>{isoDate(r.due_date, locale)}</span>;
      } },
    { key: "amount", header: t("fin.col.amount"), align: "right", sort: (r) => r.eur_cents ?? r.amount_cents,
      cell: (r) => <MoneyCell cents={r.amount_cents} currency={r.currency} eurCents={r.eur_cents} strong /> },
    { key: "iban", header: "IBAN", hideBelow: "lg", sortable: false, cell: (r) => <span className="font-mono text-[12px] whitespace-nowrap text-ink-secondary">{r.supplier?.iban || r.iban || "—"}</span> },
    { key: "run", header: t("fin.pay.run"), hideBelow: "md", sort: (r) => runNumber[r.payment_run_id] || "",
      cell: (r) => <span className="font-mono text-[12px] whitespace-nowrap text-ink-secondary">{runNumber[r.payment_run_id] || (r.payment_run_id ? t("fin.pay.in_run") : "—")}</span> },
    { key: "status", header: t("field.status"), sort: (r) => r.status || "",
      cell: (r) => (
        <span className="flex flex-col items-start gap-0.5">
          <FinStatus group="payable" value={r.status} />
          {r.paid_at && <span className="text-[11px] text-ink-tertiary">{dateTime(r.paid_at, locale)}</span>}
          {r.realised_fx_cents ? <span className="text-[11px] text-ink-tertiary">{t("fin.pay.realised_fx", { amount: signedEur(r.realised_fx_cents, locale) })}</span> : null}
        </span>
      ) },
  ];

  const pendingRuns = (runs.data?.items || []).filter((r) => r.status === "pending_approval").length;
  return (
    <div>
      <FinBar title={t("fin.tab.payables")}
        meta={<>
          {runs.data && <span className={pendingRuns ? "text-warning-fg" : undefined}>{pendingRuns ? t("fin.ov.runs_waiting", { n: pendingRuns }) : t("fin.ov.no_run_waiting")}</span>}
          {counts && <><Dot /><span>{t("fin.pay.open_n", { n: (counts.approved || 0) + (counts.scheduled || 0) })}</span></>}
          {tabValue === "open" && q.data?.eur_total_cents !== undefined && <span className="tabular text-foreground">{eur(q.data.eur_total_cents, locale)}</span>}
          {runs.data?.mode && <><Dot /><span className="text-xs text-ink-tertiary">{t(`fin.pay.bank_mode.${runs.data.mode}`)}</span></>}
          <Freshness at={q.updatedAt} onRefresh={() => { q.reload(); runs.reload(); }} refreshing={q.refreshing} />
        </>} />
      <p className="mb-3 text-sm text-ink-secondary">{locale === "fi"
        ? "Hyväksyntä ei lähetä maksuja pankkiin. Pankkilähetys edellyttää yrityksen pankkisopimusta."
        : "Approval does not send payments to the bank. Bank submission requires the company’s bank agreement."}</p>
      <Runs q={runs} canWrite={canWrite} onChanged={() => { runs.reload(); q.reload(); }} />
      <Section title={t("fin.pay.payables")}>
        <div className="mb-3"><Segmented label={t("field.status")} value={tabValue} onChange={setTab} options={options} /></div>
        <Loadable q={q} skeleton={<LoadingRows rows={8} />} isEmpty={(d) => (d.items || []).length === 0}
          empty={<EmptyState icon={Banknote} title={tabValue === "open" ? t("fin.pay.none_open") : t("fin.none_match")} body={tabValue === "open" ? t("fin.pay.none_open_body") : null} />}>
          {(d) => (
            <>
              <FinTable columns={columns} rows={d.items} rowKey="id" pageSize={50}
                rowClassName={(r) => r.id === focus && "bg-[var(--bg-subtle)]"} />
              {d.total > d.items.length && <p className="mt-2 text-xs text-ink-tertiary">{t("fin.showing", { n: d.items.length, total: d.total })}</p>}
            </>
          )}
        </Loadable>
      </Section>
      <StatusReport canWrite={canWrite} onDone={() => { runs.reload(); q.reload(); }} />
      <p className="mt-2 flex items-center gap-1.5 text-xs text-ink-tertiary"><FileCheck2 className="size-3.5" aria-hidden />{t("fin.pay.files_note")}</p>
    </div>
  );
}
