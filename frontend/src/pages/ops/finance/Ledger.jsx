// Ops › Finance › Bookkeeping (Kirjanpito). Decision: "do the books balance for this period, and has it been sent to
// the accounting system?" Trial balance per account (debit / credit / balance) or the journal entries with their lines;
// export to Procountor / Netvisor / CSV and download earlier exports.
// APIs: GET /finance/ledger?from&to&source {items, total, by_source, trial_balance{accounts[]}, unbalanced[]},
// POST /finance/ledger/build {from, to}, POST /finance/ledger/export {format, from, to, only_new},
// GET /finance/ledger/exports {items}, GET /finance/ledger/exports/{id}/file.
import React, { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { BookOpen, Hammer, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { DateInput } from "@/components/ui/DateInput";
import { Section } from "@/components/app/layout";
import { Segmented } from "@/components/app/FilterBar";
import { EmptyState, ErrorState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { isoDate } from "@/features/billing/invoiceFormat";
import { ActionNote, ConfirmAction, Dot, FileLink, FinBar, FinStatus, PeriodFilter } from "@/features/finance/kit";
import { FinTable } from "@/features/finance/Table";
import { eur, presetRange, qs } from "@/features/finance/format";

const FORMATS = ["procountor", "netvisor", "csv"];

function Lines({ lines }) {
  const { locale } = useT();
  return (
    <span className="flex flex-col gap-0.5 font-mono text-[12px]">
      {(lines || []).map((l, i) => (
        <span key={i} className="flex justify-between gap-3 whitespace-nowrap">
          <span>{l.account}{l.vat_code ? <span className="text-ink-tertiary"> · {l.vat_code}</span> : null}</span>
          <span className="tabular">{l.debit_cents ? `D ${eur(l.debit_cents, locale)}` : `K ${eur(l.credit_cents, locale)}`}</span>
        </span>
      ))}
    </span>
  );
}

export default function Ledger() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("billing.write");
  const [params, setParams] = useSearchParams();
  const view = params.get("view") === "entries" ? "entries" : "balance";
  const source = params.get("source") || null;
  const preset = params.get("preset") || (params.get("from") ? "custom" : "mtd");
  const range = preset !== "custom" ? presetRange(preset) : { from: params.get("from") || "", to: params.get("to") || "" };
  const set = (patch) => {
    const next = new URLSearchParams(params);
    Object.entries(patch).forEach(([k, v]) => (v === null || v === undefined || v === "" ? next.delete(k) : next.set(k, v)));
    setParams(next, { replace: true });
  };
  const ready = !!(range.from && range.to);
  const q = useApi(ready ? `/finance/ledger${qs({ from: range.from, to: range.to, source, limit: 500 })}` : null, { enabled: ready });
  const ex = useApi("/finance/ledger/exports");
  const [note, setNote] = useState(null);
  const [building, setBuilding] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [form, setForm] = useState({ format: "procountor", from: "", to: "", only_new: true });

  const tb = q.data?.trial_balance || null;
  const balanced = tb ? tb.debit_cents === tb.credit_cents && !(q.data?.unbalanced || []).length : null;
  const sources = useMemo(() => q.data?.by_source || {}, [q.data?.by_source]);
  const sourceOptions = useMemo(() => [
    { value: null, label: t("fin.all"), count: Object.values(sources).reduce((a, b) => a + b, 0) || undefined },
    ...Object.keys(sources).sort().map((s) => ({ value: s, label: tEnum(t, "fin.ledger_source", s), count: sources[s] })),
  ], [sources, t]);

  const build = async () => {
    setBuilding(true);
    setNote(null);
    try {
      const r = await apiPost("/finance/ledger/build", { from: range.from, to: range.to });
      const made = r?.created ?? r?.entries ?? r?.count ?? (Array.isArray(r?.items) ? r.items.length : null);
      const rejected = Array.isArray(r?.rejected) ? r.rejected.length : 0;
      setNote({ text: t("fin.ledger.built", { n: made ?? "—", rejected }) });
      q.reload();
    } catch (e) {
      setNote({ tone: "error", text: e.message });
    } finally {
      setBuilding(false);
    }
  };
  const doExport = async () => {
    const r = await apiPost("/finance/ledger/export", form);
    setNote({ text: t("fin.ledger.exported", { n: r?.count ?? 0, format: t(`fin.ledger.format.${r?.format || form.format}`) }) });
    ex.reload();
    q.reload();
    return true;
  };

  const tbCols = [
    { key: "account", header: t("fin.ledger.account"), sort: (r) => r.account, cell: (r) => <span className="font-mono text-[13px]">{r.account}</span> },
    { key: "name", header: t("fin.ledger.account_name"), sort: (r) => r.name_fi || "", cell: (r) => <span className="block min-w-[12rem]">{(locale === "fi" ? r.name_fi : r.name_en) || r.name_fi || "—"}</span> },
    { key: "debit_cents", header: t("fin.ledger.debit"), align: "right", cell: (r) => (r.debit_cents ? eur(r.debit_cents, locale) : ""), total: (x) => eur(x.debit_cents, locale) },
    { key: "credit_cents", header: t("fin.ledger.credit"), align: "right", cell: (r) => (r.credit_cents ? eur(r.credit_cents, locale) : ""), total: (x) => eur(x.credit_cents, locale) },
    { key: "balance_cents", header: t("fin.ledger.balance"), align: "right", cell: (r) => eur(r.balance_cents, locale),
      total: (x) => <span className={cn(x.balance_cents !== 0 && "text-danger-fg")}>{eur(x.balance_cents, locale)}</span> },
  ];
  const entryCols = [
    { key: "date", header: t("fin.col.date"), sort: (r) => r.date || "", cell: (r) => <span className="tabular whitespace-nowrap">{isoDate(r.date, locale)}</span> },
    { key: "source", header: t("fin.col.source"), sort: (r) => r.source || "", cell: (r) => <span className="whitespace-nowrap text-ink-secondary">{tEnum(t, "fin.ledger_source", r.source)}</span> },
    { key: "description", header: t("fin.col.description"), sortable: false,
      cell: (r) => (
        <span className="block min-w-[12rem] max-w-[24rem]">
          {r.description || "—"}
          {r.dimension?.shipment_ref && <span className="block text-[11px] text-ink-tertiary">{r.dimension.shipment_ref}</span>}
        </span>
      ) },
    { key: "lines", header: t("fin.ledger.lines"), sortable: false, cell: (r) => <Lines lines={r.lines} /> },
    { key: "total_cents", header: t("fin.col.amount"), align: "right", hideBelow: "md", sort: (r) => r.total_cents, cell: (r) => eur(r.total_cents, locale) },
    { key: "exported_at", header: t("fin.ledger.exported_col"), hideBelow: "lg", sort: (r) => r.exported_at || "",
      cell: (r) => (r.exported_at ? <span className="text-xs text-ink-tertiary whitespace-nowrap">{dateTime(r.exported_at, locale)}</span> : <span className="text-xs text-warning-fg">{t("fin.ledger.not_exported")}</span>) },
  ];
  const notExported = (q.data?.items || []).filter((e) => !e.exported_at).length;

  return (
    <div>
      <FinBar title={t("fin.tab.ledger")}
        meta={q.data ? (
          <>
            <span>{t("fin.ledger.entries_n", { n: q.data.total ?? 0 })}</span>
            <Dot />
            {balanced === null ? null : balanced ? <span className="text-success-fg">{t("fin.ledger.balanced")}</span>
              : <span className="text-danger-fg">{t("fin.ledger.unbalanced", { n: (q.data.unbalanced || []).length })}</span>}
            {notExported > 0 && <><Dot /><span className="text-warning-fg">{t("fin.ledger.not_exported_n", { n: notExported })}</span></>}
            <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
          </>
        ) : null}
        actions={<>
          <Button size="sm" variant="outline" onClick={build} disabled={!canWrite || building || !ready} title={canWrite ? t("fin.ledger.build_hint") : t("fin.no_permission")}>
            <Hammer /> {building ? t("fin.ledger.building") : t("fin.ledger.build")}
          </Button>
          <Button size="sm" disabled={!canWrite || !ready} title={!canWrite ? t("fin.no_permission") : undefined}
            onClick={() => { setForm((f) => ({ ...f, from: range.from, to: range.to })); setExportOpen(true); }}>
            <Upload /> {t("fin.ledger.export")}
          </Button>
        </>} />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <PeriodFilter value={{ ...range, preset }}
          onChange={(p) => set(p.preset === "custom" ? { preset: "custom", from: p.from, to: p.to } : { preset: p.preset === "mtd" ? null : p.preset, from: null, to: null })} />
        <Segmented label={t("fin.ledger.view")} value={view} onChange={(v) => set({ view: v === "balance" ? null : v })}
          options={[{ value: "balance", label: t("fin.ledger.trial_balance") }, { value: "entries", label: t("fin.ledger.entries") }]} />
      </div>
      {view === "entries" && Object.keys(sources).length > 0 && (
        <div className="mb-3"><Segmented label={t("fin.col.source")} value={source} onChange={(v) => set({ source: v })} options={sourceOptions} /></div>
      )}
      <ActionNote note={note} onClose={() => setNote(null)} className="mb-3" />
      {!ready ? <p className="text-sm text-ink-tertiary">{t("fin.period.pick")}</p> : (
        <Loadable q={q} skeleton={<LoadingRows rows={10} />}
          isEmpty={(d) => (d.items || []).length === 0 && !(d.trial_balance?.accounts || []).length}
          empty={<EmptyState icon={BookOpen} title={t("fin.ledger.empty")} body={t("fin.ledger.empty_body")} />}>
          {(d) => view === "balance" ? (
            <FinTable columns={tbCols} rows={d.trial_balance?.accounts || []} rowKey="account" totals={d.trial_balance} totalsLabel={t("fin.total")} />
          ) : (
            <>
              <FinTable columns={entryCols} rows={d.items || []} rowKey="id" pageSize={50}
                rowClassName={(r) => (d.unbalanced || []).includes(r.id) && "bg-[var(--status-failed-bg)]"} />
              {d.total > (d.items || []).length && <p className="mt-2 text-xs text-ink-tertiary">{t("fin.showing", { n: d.items.length, total: d.total })}</p>}
            </>
          )}
        </Loadable>
      )}
      <Section title={t("fin.ledger.exports")} className="mt-6">
        {ex.error && !ex.data ? <ErrorState compact error={ex.error} onRetry={ex.reload} /> : (
          <Loadable q={ex} skeleton={<LoadingRows rows={3} />} isEmpty={(d) => (d.items || []).length === 0}
            empty={<p className="py-2 text-sm text-ink-tertiary">{t("fin.ledger.no_exports")}</p>}>
            {(d) => (
              <FinTable rows={d.items} rowKey="id" columns={[
                { key: "created_at", header: t("fin.col.date"), sort: (r) => r.created_at || "", cell: (r) => <span className="tabular whitespace-nowrap">{dateTime(r.created_at, locale)}</span> },
                { key: "format", header: t("fin.ledger.format_col"), cell: (r) => t(`fin.ledger.format.${r.format}`) },
                { key: "range", header: t("fin.ledger.range"), sortable: false, cell: (r) => <span className="tabular whitespace-nowrap">{isoDate(r.from, locale)} – {isoDate(r.to, locale)}</span> },
                { key: "count", header: t("fin.ledger.entries"), align: "right", cell: (r) => r.count ?? "—" },
                { key: "status", header: t("field.status"), hideBelow: "md", cell: (r) => <FinStatus group="ledger_export" value={r.status} /> },
                { key: "file", header: "", sortable: false, className: "text-right", cell: (r) => (r.file_id || r.id ? <FileLink path={`/finance/ledger/exports/${r.id}/file`} /> : null) },
              ]} />
            )}
          </Loadable>
        )}
      </Section>
      <ConfirmAction open={exportOpen} onOpenChange={setExportOpen} title={t("fin.ledger.export")} description={t("fin.ledger.export_body")}
        submitLabel={t("fin.ledger.export")} onSubmit={() => (form.from && form.to ? doExport() : false)}>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label>{t("fin.ledger.format_col")}</Label>
            <Segmented label={t("fin.ledger.format_col")} value={form.format} onChange={(v) => setForm({ ...form, format: v })}
              options={FORMATS.map((f) => ({ value: f, label: t(`fin.ledger.format.${f}`) }))} />
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <span className="flex flex-col gap-1.5"><Label htmlFor="lx-from">{t("fin.period.from")}</Label><span className="w-36"><DateInput id="lx-from" value={form.from} onChange={(v) => setForm({ ...form, from: v })} /></span></span>
            <span className="flex flex-col gap-1.5"><Label htmlFor="lx-to">{t("fin.period.to")}</Label><span className="w-36"><DateInput id="lx-to" value={form.to} onChange={(v) => setForm({ ...form, to: v })} /></span></span>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={form.only_new} onCheckedChange={(v) => setForm({ ...form, only_new: !!v })} />
            {t("fin.ledger.only_new")}
          </label>
        </div>
      </ConfirmAction>
    </div>
  );
}
