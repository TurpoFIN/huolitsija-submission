// Ops › Finance › Cost accruals (Kuluvaraukset). Decision: "which booked costs are still waiting for a carrier invoice,
// which shipments have no booked rate to accrue, and where did the invoice differ?" One row per shipment + supplier.
// APIs: GET /finance/accruals?status=&shipment_id= {items, total, counts, totals}, POST /finance/accruals/rebuild.
import React, { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Layers, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/app/FilterBar";
import { EmptyState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { ActionNote, Dot, FinBar, FinStatus, MoneyCell, usePost } from "@/features/finance/kit";
import { FinTable } from "@/features/finance/Table";
import { eur, periodLabel, qs, signedEur } from "@/features/finance/format";
import { laneLabel } from "./Pnl";

const STATUSES = ["open", "no_booked_rate", "partially_matched", "matched", "disputed", "released", "written_off"];

export default function Accruals() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("billing.write");
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || null;
  const shipmentId = params.get("shipment_id") || null;
  const q = useApi(`/finance/accruals${qs({ status, shipment_id: shipmentId })}`, { pollMs: 120000 });
  const all = useApi(status || shipmentId ? "/finance/accruals" : null, { enabled: !!(status || shipmentId) });
  const run = usePost();
  const [note, setNote] = useState(null);

  const counts = (status || shipmentId ? all.data?.counts : q.data?.counts) || null;
  const totals = q.data?.totals || null;
  const set = (k, v) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v); else next.delete(k);
    setParams(next, { replace: true });
  };
  const options = useMemo(() => [
    { value: null, label: t("fin.all"), count: counts ? Object.values(counts).reduce((a, b) => a + b, 0) : undefined },
    ...STATUSES.filter((s) => !counts || counts[s] || s === "open").map((s) => ({ value: s, label: tEnum(t, "fin.accrual", s), count: counts ? counts[s] || 0 : undefined })),
  ], [counts, t]);

  const rebuild = async () => {
    setNote(null);
    const r = await run.post("/finance/accruals/rebuild", {});
    if (r) {
      setNote({ text: t("fin.acc.rebuilt", { processed: r.processed ?? 0, rows: r.rows ?? 0, missing: r.no_booked_rate ?? 0 }) });
      q.reload();
      all.reload();
    } else setNote(null);
  };

  const columns = [
    { key: "shipment_ref", header: t("fin.col.shipment"), sort: (r) => r.shipment_ref || "",
      cell: (r) => r.shipment_id ? <Link to={`/ops/shipments/${r.shipment_id}`} className="font-medium whitespace-nowrap hover:underline">{r.shipment_ref || "—"}</Link> : (r.shipment_ref || "—") },
    { key: "supplier", header: t("fin.col.supplier"), sort: (r) => r.supplier?.name || "", className: "max-w-[16rem]",
      cell: (r) => (
        <span className="flex min-w-0 flex-col">
          <span className="truncate">{r.supplier?.name || "—"}</span>
          {r.supplier?.kind && <span className="text-[11px] text-ink-tertiary">{tEnum(t, "fin.supplier_kind", r.supplier.kind)}</span>}
        </span>
      ) },
    { key: "lane", header: t("fin.col.lane"), hideBelow: "lg", sort: (r) => r.lane?.key || "", cell: (r) => <span className="whitespace-nowrap text-ink-secondary">{laneLabel(r.lane?.key, t)}</span> },
    { key: "period", header: t("fin.col.period"), hideBelow: "md", sort: (r) => r.period || "", cell: (r) => <span className="whitespace-nowrap tabular text-ink-secondary">{r.period || "—"}</span> },
    { key: "expected", header: t("fin.col.accrued"), align: "right", sort: (r) => r.expected_eur_cents,
      cell: (r) => <MoneyCell cents={r.expected_cents} currency={r.currency} eurCents={r.expected_eur_cents} />, total: (x) => eur(x.expected_eur_cents, locale) },
    { key: "actual", header: t("fin.col.invoiced"), align: "right", sort: (r) => r.actual_eur_cents, cell: (r) => eur(r.actual_eur_cents, locale), total: (x) => eur(x.actual_eur_cents, locale) },
    { key: "variance", header: t("fin.col.variance"), align: "right", sort: (r) => r.variance_eur_cents,
      cell: (r) => <span className={cn(r.variance_eur_cents > 0 && "text-danger-fg")}>{signedEur(r.variance_eur_cents, locale)}</span>,
      total: (x) => <span className={cn(x.variance_eur_cents > 0 && "text-danger-fg")}>{signedEur(x.variance_eur_cents, locale)}</span> },
    { key: "status", header: t("field.status"), sort: (r) => r.status || "",
      cell: (r) => (
        <span className="flex flex-col items-start gap-0.5">
          <FinStatus group="accrual" value={r.status} />
          {r.reason && <span className="max-w-[16rem] text-[11px] text-ink-tertiary">{r.reason}</span>}
          {!r.reason && r.basis && <span className="text-[11px] text-ink-tertiary">{tEnum(t, "fin.accrual_basis", r.basis)}</span>}
        </span>
      ) },
  ];

  const openN = counts ? (counts.open || 0) + (counts.partially_matched || 0) : null;
  return (
    <div>
      <FinBar title={t("fin.tab.accruals")}
        meta={<>
          {openN !== null && <span>{t("fin.acc.open_n", { n: openN })}</span>}
          {totals?.open_eur_cents !== undefined && <span className="tabular text-foreground">{eur(totals.open_eur_cents, locale)}</span>}
          {counts?.no_booked_rate > 0 && <><Dot /><button type="button" className="text-warning-fg hover:underline" onClick={() => set("status", "no_booked_rate")}>{t("fin.acc.no_rate_n", { n: counts.no_booked_rate })}</button></>}
          {totals?.variance_eur_cents ? <><Dot /><span className="tabular">{t("fin.acc.variance_total", { amount: signedEur(totals.variance_eur_cents, locale) })}</span></> : null}
          <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
        </>}
        actions={<Button variant="outline" size="sm" onClick={rebuild} disabled={!canWrite || !!run.busy} title={!canWrite ? t("fin.no_permission") : t("fin.acc.rebuild_hint")}>
          <RefreshCw className={cn(run.busy && "animate-spin")} /> {run.busy ? t("fin.acc.rebuilding") : t("fin.acc.rebuild")}
        </Button>} />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Segmented label={t("field.status")} value={status} onChange={(v) => set("status", v)} options={options} />
        {shipmentId && <Button size="sm" variant="outline" onClick={() => set("shipment_id", null)}>{t("fin.acc.all_shipments")}</Button>}
      </div>
      <ActionNote note={note || (run.error ? { tone: "error", text: run.error.message } : null)} onClose={() => setNote(null)} className="mb-3" />
      <Loadable q={q} skeleton={<LoadingRows rows={10} />} isEmpty={(d) => (d.items || []).length === 0}
        empty={<EmptyState icon={Layers} title={status ? t("fin.none_match") : t("fin.acc.empty")} body={status ? null : t("fin.acc.empty_body")}
          action={status ? <Button size="sm" variant="outline" onClick={() => set("status", null)}>{t("action.clear")}</Button> : null} />}>
        {(d) => (
          <>
            <FinTable columns={columns} rows={d.items} rowKey="id" pageSize={50}
              totals={totals} totalsLabel={t("fin.total")} />
            {d.total > d.items.length && <p className="mt-2 text-xs text-ink-tertiary">{t("fin.showing", { n: d.items.length, total: d.total })}</p>}
            {d.period && <p className="mt-2 text-xs text-ink-tertiary">{periodLabel(d.period, locale)}</p>}
          </>
        )}
      </Loadable>
    </div>
  );
}
