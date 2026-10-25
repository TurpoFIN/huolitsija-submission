// Ops › Finance › Profitability (Kannattavuus). Decision: "which shipments and lanes earn money, and how sure is that?"
// Revenue = issued invoices net of VAT and disbursements; cost = matched carrier invoices (actual) + open accruals.
// Per shipment (with basis actual / partly actual / estimated and variance against the quoted margin) or per lane.
// APIs: GET /finance/reports/pnl/shipments?from&to&lane, /reports/pnl/lanes?from&to, /reports/pnl/shipments/{id}.
import React, { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { BarChart3, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { KV, Section } from "@/components/app/layout";
import { Segmented } from "@/components/app/FilterBar";
import { EmptyState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { isoDate } from "@/features/billing/invoiceFormat";
import { Dot, FinBar, FinStatus, PeriodFilter } from "@/features/finance/kit";
import { FinTable } from "@/features/finance/Table";
import { eur, pctText, presetRange, qs, signedEur } from "@/features/finance/format";
import { readShipmentRow, readSumRow } from "@/features/finance/pnl";

export function laneLabel(key, t) {
  if (!key) return "—";
  const m = /^([^:]+):([^>]+)>(.+)$/.exec(key);
  if (!m) return key;
  return `${tEnum(t, "mode", m[1])} ${m[2]} → ${m[3]}`;
}

function CostCell({ r }) {
  const { t, locale } = useT();
  return (
    <span className="inline-flex flex-col items-end whitespace-nowrap">
      <span>{eur(r.cost, locale)}</span>
      {(r.accrued || 0) !== 0 && (
        <span className="text-[11px] text-ink-tertiary">{t("fin.pnl.split", { actual: eur(r.actual, locale), accrued: eur(r.accrued, locale) })}</span>
      )}
    </span>
  );
}

function VarianceCell({ cents, pts }) {
  const { t, locale } = useT();
  if (cents === null || cents === undefined) return <span className="text-ink-tertiary" title={t("fin.pnl.no_quote")}>—</span>;
  return (
    <span className="inline-flex flex-col items-end whitespace-nowrap">
      <span className={cn(cents < 0 && "text-danger-fg")}>{signedEur(cents, locale)}</span>
      {pts !== null && pts !== undefined && <span className="text-[11px] text-ink-tertiary">{t("fin.pnl.points", { n: (pts > 0 ? "+" : "") + pctText(pts, locale).replace(" %", "") })}</span>}
    </span>
  );
}

const Gp = ({ cents }) => {
  const { locale } = useT();
  return <span className={cn("font-medium", cents < 0 && "text-danger-fg")}>{eur(cents, locale)}</span>;
};

function LinesTable({ title, rows, columns }) {
  if (!rows || rows.length === 0) return null;
  return (
    <Section title={title}>
      <FinTable columns={columns} rows={rows} rowKey={(r) => r.id || JSON.stringify(r)} />
    </Section>
  );
}

function ShipmentDrill({ id, onClose }) {
  const { t, locale } = useT();
  const q = useApi(id ? `/finance/reports/pnl/shipments/${id}` : null, { enabled: !!id });
  return (
    <Sheet open={!!id} onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="w-full overflow-y-auto p-0 sm:max-w-2xl">
        <SheetHeader className="border-b border-[color:var(--border-subtle)] px-5 py-4 text-left">
          <SheetTitle>{q.data?.shipment?.reference || t("fin.pnl.shipment")}</SheetTitle>
          <SheetDescription>{q.data ? `${q.data.shipment?.customer || "—"} · ${laneLabel(q.data.shipment?.lane?.key, t)}` : " "}</SheetDescription>
        </SheetHeader>
        <div className="px-5 py-4">
          <Loadable q={q} skeleton={<LoadingRows rows={6} />}>
            {(d) => {
              const r = readShipmentRow(d);
              return (
                <>
                  <div className="mb-4 flex flex-wrap items-center gap-2">
                    <FinStatus group="pnl_basis" value={r.basis} />
                    {d.shipment?.path && <Button asChild size="xs" variant="outline"><Link to={d.shipment.path}>{t("fin.pnl.open_shipment")}</Link></Button>}
                  </div>
                  <KV cols={3} className="mb-5" items={[
                    { label: t("fin.pnl.revenue"), value: eur(r.revenue, locale), className: "tabular" },
                    { label: t("fin.pnl.cost_actual"), value: eur(r.actual, locale), className: "tabular" },
                    { label: t("fin.pnl.cost_accrued"), value: eur(r.accrued, locale), className: "tabular" },
                    { label: t("fin.pnl.gross_profit"), value: <Gp cents={r.gp} />, className: "tabular" },
                    { label: t("fin.pnl.margin"), value: pctText(r.margin, locale), className: "tabular" },
                    { label: t("fin.pnl.fx_result"), value: d.fx_result_cents ? signedEur(d.fx_result_cents, locale) : eur(d.fx_result_cents, locale), className: "tabular" },
                    { label: t("fin.pnl.quoted_margin"), value: r.quotedMargin === null ? null : `${eur(r.quotedMargin, locale)} · ${pctText(r.quotedPct, locale)}`, className: "tabular",
                      hint: d.quoted?.quote_number || null },
                    { label: t("fin.pnl.vs_quote"), value: r.variance === null ? null : <VarianceCell cents={r.variance} pts={r.variancePts} /> },
                    { label: t("fin.pnl.period"), value: d.period },
                  ]} />
                  {r.warnings.length > 0 && (
                    <ul className="mb-4 flex flex-col gap-1">
                      {r.warnings.map((w, i) => <li key={i} className="rounded-lg px-3 py-2 text-sm status-pending">{w}</li>)}
                    </ul>
                  )}
                  <LinesTable title={t("fin.pnl.lines.invoices")} rows={d.lines?.invoices} columns={[
                    { key: "number", header: t("fin.col.number"), cell: (x) => x.path ? <Link className="text-ink-accent hover:underline" to={x.path}>{x.number || t("billing.draft")}</Link> : x.number || "—" },
                    { key: "status", header: t("field.status"), cell: (x) => tEnum(t, "invoice", x.status) },
                    { key: "issue_date", header: t("fin.col.date"), cell: (x) => <span className="tabular whitespace-nowrap">{isoDate(x.issue_date, locale)}</span> },
                    { key: "services_net_eur_cents", header: t("fin.pnl.services_net"), align: "right", cell: (x) => <span className={cn(!x.counted && "text-ink-tertiary line-through")}>{eur(x.services_net_eur_cents, locale)}</span> },
                    { key: "pass_through_eur_cents", header: t("fin.pnl.pass_through"), align: "right", cell: (x) => eur(x.pass_through_eur_cents, locale) },
                  ]} />
                  <LinesTable title={t("fin.pnl.lines.accruals")} rows={d.lines?.accruals} columns={[
                    { key: "supplier", header: t("fin.col.supplier"), cell: (x) => <span className="whitespace-nowrap">{x.supplier || "—"}</span> },
                    { key: "status", header: t("field.status"), cell: (x) => <FinStatus group="accrual" value={x.status} /> },
                    { key: "expected_eur_cents", header: t("fin.col.accrued"), align: "right", cell: (x) => eur(x.expected_eur_cents, locale) },
                    { key: "actual_eur_cents", header: t("fin.col.invoiced"), align: "right", cell: (x) => eur(x.actual_eur_cents, locale) },
                    { key: "variance_eur_cents", header: t("fin.col.variance"), align: "right", cell: (x) => <span className={cn(x.variance_eur_cents > 0 && "text-danger-fg")}>{signedEur(x.variance_eur_cents, locale)}</span> },
                  ]} />
                  <LinesTable title={t("fin.pnl.lines.carrier_invoices")} rows={d.lines?.carrier_invoices} columns={[
                    { key: "number", header: t("fin.col.number"), cell: (x) => x.path ? <Link className="text-ink-accent hover:underline" to={x.path}>{x.number || "—"}</Link> : x.number || "—" },
                    { key: "supplier", header: t("fin.col.supplier"), cell: (x) => x.supplier || "—" },
                    { key: "status", header: t("field.status"), cell: (x) => <FinStatus group="carrier_invoice" value={x.status} /> },
                    { key: "invoiced_eur_cents", header: t("fin.col.invoiced"), align: "right", cell: (x) => eur(x.invoiced_eur_cents, locale) },
                    { key: "accrued_eur_cents", header: t("fin.col.accrued"), align: "right", cell: (x) => eur(x.accrued_eur_cents, locale) },
                  ]} />
                  {!d.lines?.invoices?.length && !d.lines?.accruals?.length && !d.lines?.carrier_invoices?.length && (
                    <p className="text-sm text-ink-tertiary">{t("fin.pnl.no_lines")}</p>
                  )}
                </>
              );
            }}
          </Loadable>
        </div>
      </SheetContent>
    </Sheet>
  );
}

export default function Pnl() {
  const { t, locale } = useT();
  const [params, setParams] = useSearchParams();
  const view = ["lanes", "months"].includes(params.get("view")) ? params.get("view") : "shipments";
  const lane = params.get("lane") || "";
  const companyId = params.get("company_id") || "";
  const customers = useApi("/ops/customers");
  const basis = params.get("basis") || null;
  const preset = params.get("preset") || (params.get("from") ? "custom" : "mtd");
  const range = preset !== "custom" ? presetRange(preset) : { from: params.get("from") || "", to: params.get("to") || "" };
  const [drill, setDrill] = useState(null);

  const set = (patch) => {
    const next = new URLSearchParams(params);
    Object.entries(patch).forEach(([k, val]) => (val === null || val === undefined || val === "" ? next.delete(k) : next.set(k, val)));
    setParams(next, { replace: true });
  };
  const ready = range.from && range.to;
  const sq = useApi(ready && view === "shipments" ? `/finance/reports/pnl/shipments${qs({ from: range.from, to: range.to, lane, company_id: companyId })}` : null,
    { enabled: !!ready && view === "shipments" });
  const lq = useApi(ready && view === "lanes" ? `/finance/reports/pnl/lanes${qs({ from: range.from, to: range.to, company_id: companyId })}` : null,
    { enabled: !!ready && view === "lanes" });
  const mq = useApi(ready && view === "months" ? `/finance/reports/margin${qs({ group: "month", from: range.from, to: range.to, company_id: params.get("company_id") })}` : null,
    { enabled: !!ready && view === "months" });
  const q = view === "months" ? mq : view === "lanes" ? lq : sq;
  const totals = readSumRow(q.data?.totals);
  const counts = sq.data?.basis_counts || null;

  const shipmentRows = useMemo(() => {
    const rows = (sq.data?.items || []).map(readShipmentRow);
    return basis ? rows.filter((r) => r.basis === basis) : rows;
  }, [sq.data, basis]);
  const laneRows = useMemo(() => ((view === "months" ? mq.data : lq.data)?.items || []).map(r => readSumRow({ ...r, lane: r.lane || r.key })), [lq.data, mq.data, view]);

  const shipCols = [
    { key: "reference", header: t("fin.col.shipment"), sort: (r) => r.reference || "",
      cell: (r) => <span className="font-medium whitespace-nowrap">{r.reference || "—"}</span> },
    { key: "customer", header: t("field.customer"), hideBelow: "lg", sort: (r) => r.customer || "", className: "max-w-[14rem]",
      cell: (r) => <span className="block truncate">{r.customer || "—"}</span> },
    { key: "lane", header: t("fin.col.lane"), hideBelow: "md", sort: (r) => r.lane || "",
      cell: (r) => <span className="whitespace-nowrap text-ink-secondary">{laneLabel(r.lane, t)}</span> },
    { key: "revenue", header: t("fin.pnl.revenue"), align: "right", sort: (r) => r.revenue, cell: (r) => eur(r.revenue, locale),
      total: (x) => eur(x.revenue, locale) },
    { key: "cost", header: t("fin.pnl.cost"), align: "right", sort: (r) => r.cost, cell: (r) => <CostCell r={r} />, total: (x) => <CostCell r={x} /> },
    { key: "gp", header: t("fin.pnl.gross_profit"), align: "right", sort: (r) => r.gp, cell: (r) => <Gp cents={r.gp} />, total: (x) => <Gp cents={x.gp} /> },
    { key: "margin", header: t("fin.pnl.margin_short"), align: "right", sort: (r) => r.margin, cell: (r) => pctText(r.margin, locale),
      total: (x) => pctText(x.margin, locale) },
    { key: "basis", header: t("fin.pnl.basis"), sort: (r) => r.basis || "", cell: (r) => <FinStatus group="pnl_basis" value={r.basis} /> },
    { key: "variance", header: t("fin.pnl.vs_quote"), align: "right", hideBelow: "md", sort: (r) => r.variance,
      headTitle: t("fin.pnl.vs_quote_hint"), cell: (r) => <VarianceCell cents={r.variance} pts={r.variancePts} />,
      total: (x) => <VarianceCell cents={x.variance} pts={x.variancePts} /> },
  ];
  const laneCols = [
    { key: "lane", header: t(view === "months" ? "fin.col.period" : "fin.col.lane"), sort: (r) => r.lane || "", cell: (r) => <span className="font-medium whitespace-nowrap">{laneLabel(r.lane, t)}</span> },
    { key: "shipments", header: t("fin.pnl.shipments"), align: "right", sort: (r) => r.shipments, cell: (r) => r.shipments ?? "—", total: (x) => x.shipments },
    { key: "revenue", header: t("fin.pnl.revenue"), align: "right", sort: (r) => r.revenue, cell: (r) => eur(r.revenue, locale), total: (x) => eur(x.revenue, locale) },
    { key: "cost", header: t("fin.pnl.cost"), align: "right", sort: (r) => r.cost, cell: (r) => <CostCell r={r} />, total: (x) => <CostCell r={x} /> },
    { key: "gp", header: t("fin.pnl.gross_profit"), align: "right", sort: (r) => r.gp, cell: (r) => <Gp cents={r.gp} />, total: (x) => <Gp cents={x.gp} /> },
    { key: "margin", header: t("fin.pnl.margin_short"), align: "right", sort: (r) => r.margin, cell: (r) => pctText(r.margin, locale), total: (x) => pctText(x.margin, locale) },
    { key: "perShipment", header: t("fin.pnl.per_shipment"), align: "right", hideBelow: "md", sort: (r) => r.perShipment, cell: (r) => eur(r.perShipment, locale),
      total: (x) => eur(x.perShipment, locale) },
    { key: "actualShare", header: t("fin.pnl.actual_share"), align: "right", hideBelow: "lg", sort: (r) => r.actualShare, headTitle: t("fin.pnl.actual_share_hint"),
      cell: (r) => pctText(r.actualShare, locale, 0), total: (x) => pctText(x.actualShare, locale, 0) },
    { key: "variance", header: t("fin.pnl.vs_quote"), align: "right", hideBelow: "md", sort: (r) => r.variance, headTitle: t("fin.pnl.vs_quote_hint"),
      cell: (r) => <VarianceCell cents={r.variance} pts={r.variancePts} />, total: (x) => <VarianceCell cents={x.variance} pts={x.variancePts} /> },
  ];

  const basisOptions = [
    { value: null, label: t("fin.all"), count: counts ? Object.values(counts).reduce((a, b) => a + b, 0) : undefined },
    ...["actual", "partly_actual", "estimated"].map((b) => ({ value: b, label: tEnum(t, "fin.pnl_basis", b), count: counts ? counts[b] || 0 : undefined })),
  ];

  return (
    <div>
      <FinBar title={t("fin.tab.pnl")}
        meta={totals ? (
          <>
            <span><span className={cn("tabular font-medium text-foreground", totals.gp < 0 && "text-danger-fg")}>{eur(totals.gp, locale)}</span> {t("fin.pnl.gp_label")}</span>
            <span className="tabular">{pctText(totals.margin, locale)}</span>
            <Dot />
            <span className="tabular">{t("fin.ov.shipments_n", { n: totals.shipments ?? 0 })}</span>
            {totals.actualShare !== null && <><Dot /><span className="tabular">{t("fin.pnl.actual_share_line", { pct: pctText(totals.actualShare, locale, 0) })}</span></>}
            <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
          </>
        ) : null}
        actions={<Segmented label={t("fin.pnl.view")} value={view} onChange={(v) => set({ view: v === "shipments" ? null : v })}
          options={[{ value: "shipments", label: t("fin.pnl.by_shipment") }, { value: "lanes", label: t("fin.pnl.by_lane") }, { value: "months", label: locale === "fi" ? "Kuukausittain" : "By month" }]} />} />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-sm">{t("field.customer")}
          <select aria-label={t("field.customer")} value={companyId} onChange={e => set({company_id: e.target.value})} className="h-8 max-w-[16rem] rounded-lg border px-2 bg-background">
            <option value="">{t("fin.all")}</option>
            {(customers.data?.items || []).map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>
        <PeriodFilter value={{ ...range, preset }}
          onChange={(p) => set(p.preset === "custom" ? { preset: "custom", from: p.from, to: p.to } : { preset: p.preset === "mtd" ? null : p.preset, from: null, to: null })} />
        {view === "shipments" && (
          <Segmented label={t("fin.pnl.basis")} value={basis} onChange={(b) => set({ basis: b })} options={basisOptions} />
        )}
        {view === "shipments" && lane && (
          <span className="inline-flex h-8 items-center gap-1 rounded-full bg-secondary pl-3 pr-1 text-sm">
            {laneLabel(lane, t)}
            <button type="button" onClick={() => set({ lane: null })} aria-label={t("fin.pnl.clear_lane")} className="rounded-full p-1 hover:bg-hover"><X className="size-3.5" /></button>
          </span>
        )}
      </div>
      {!ready ? <p className="text-sm text-ink-tertiary">{t("fin.period.pick")}</p> : (
        <Loadable q={q} skeleton={<LoadingRows rows={10} />}
          isEmpty={(d) => (d.items || []).length === 0}
          empty={<EmptyState icon={BarChart3} title={t("fin.pnl.empty")} body={t("fin.pnl.empty_body")} />}>
          {() => view === "shipments" ? (
            <>
              <FinTable columns={shipCols} rows={shipmentRows} totals={basis ? null : totals} totalsLabel={t("fin.total")}
                onRowClick={(r) => setDrill(r.id)} defaultSort={null}
                empty={<EmptyState title={t("fin.none_match")} action={<Button size="sm" variant="outline" onClick={() => set({ basis: null })}>{t("action.clear")}</Button>} />} />
              <p className="mt-2 text-xs text-ink-tertiary">{t("fin.pnl.basis_note")}</p>
            </>
          ) : (
            <FinTable columns={laneCols} rows={laneRows} rowKey="lane" totals={totals} totalsLabel={t("fin.total")}
              onRowClick={view === "months" ? undefined : (r) => set({ view: null, lane: r.lane })} />
          )}
        </Loadable>
      )}
      <ShipmentDrill id={drill} onClose={() => setDrill(null)} />
    </div>
  );
}
