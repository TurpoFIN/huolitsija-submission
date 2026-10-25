// Ops › Finance › Overview. Decision: "what needs a finance decision now?" Rows that need a person come first
// (carrier invoices to review or in dispute, a payment run to approve, payables due this week, collections, overdue
// receivables), then the state of the books (VAT estimate, gross profit month to date, open accruals, ECB rate date,
// last ledger export, Autopilot). Each row links to its screen. Reads GET /finance/overview {tiles, order} and
// GET /finance/payment-runs (runs waiting for approval).
import React from "react";
import { Link } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { Section } from "@/components/app/layout";
import { ErrorState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { isoDate } from "@/features/billing/invoiceFormat";
import { Dot, FinBar } from "@/features/finance/kit";
import { amount, eur, pctText, periodLabel } from "@/features/finance/format";
import { localLink } from "@/features/finance/links";

function Row({ label, to, children, attention, testid }) {
  return (
    <li data-testid={testid}>
      <Link to={to} className="flex flex-wrap items-center gap-x-3 gap-y-0.5 rounded-md px-1 py-2.5 hover:bg-hover">
        <span className={cn("w-full text-sm sm:w-64 sm:shrink-0", attention ? "font-medium" : "text-ink-secondary")}>{label}</span>
        <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2 gap-y-0.5 text-sm">{children}</span>
        <ChevronRight className="hidden size-4 shrink-0 text-ink-tertiary sm:block" aria-hidden />
      </Link>
    </li>
  );
}

const None = ({ children }) => <span className="text-ink-tertiary">{children}</span>;
const Num = ({ children, tone }) => <span className={cn("tabular font-medium", tone)}>{children}</span>;

const DECIDE = ["carrier_invoices_attention", "payment_runs_pending", "payables_due_7d", "collections_open", "ar_overdue"];
const BOOKS = ["vat_current", "gross_profit_mtd", "open_accruals", "fx", "ledger_last_export", "autopilot"];

function TileRow({ k, tile, runs }) {
  const { t, locale } = useT();
  const to = localLink(tile?.path, k);
  const label = t(`fin.ov.${k}`);
  if (k === "payment_runs_pending") {
    const pending = (runs || []).filter((r) => r.status === "pending_approval");
    const r = pending[0];
    return (
      <Row testid={`ov-${k}`} label={label} to="/ops/finance/payables?view=runs" attention={pending.length > 0}>
        {runs === null ? <None>—</None> : r ? (
          <>
            <Num tone="text-warning-fg">{t("fin.ov.run_waiting", { n: r.number || "" })}</Num>
            {r.count !== undefined && <span className="tabular text-ink-secondary">{t("fin.ov.payments_n", { n: r.count })}</span>}
            {Object.entries(r.totals || {}).map(([cur, c]) => <span key={cur} className="tabular">{amount(c, cur, locale)}</span>)}
            {r.execution_date && <span className="text-xs text-ink-tertiary">{t("fin.ov.execution", { date: isoDate(r.execution_date, locale) })}</span>}
            {pending.length > 1 && <span className="text-xs text-ink-tertiary">{t("fin.ov.more_runs", { n: pending.length - 1 })}</span>}
          </>
        ) : <None>{t("fin.ov.no_run_waiting")}</None>}
      </Row>
    );
  }
  if (!tile) return null;
  if (tile.error) {
    return <Row testid={`ov-${k}`} label={label} to={to}><span className="text-danger-fg">{t("fin.ov.tile_failed")}</span></Row>;
  }
  const n = tile.count;
  switch (k) {
    case "carrier_invoices_attention":
      return (
        <Row testid={`ov-${k}`} label={label} to={to} attention={n > 0}>
          {n > 0 ? (
            <>
              {tile.needs_review > 0 && <Num tone="text-warning-fg">{t("fin.ov.to_review", { n: tile.needs_review })}</Num>}
              {tile.disputed > 0 && <Num tone="text-danger-fg">{t("fin.ov.disputed", { n: tile.disputed })}</Num>}
              <span className="tabular text-ink-secondary">{eur(tile.eur_cents, locale)}</span>
            </>
          ) : <None>{t("fin.ov.none_to_review")}</None>}
        </Row>
      );
    case "payables_due_7d":
      return (
        <Row testid={`ov-${k}`} label={label} to={to} attention={n > 0}>
          {n > 0 ? (
            <>
              <Num>{t("fin.ov.payables_n", { n })}</Num>
              <span className="tabular">{eur(tile.eur_cents, locale)}</span>
              {Object.keys(tile.by_currency || {}).filter((c) => c !== "EUR").map((c) => (
                <span key={c} className="text-xs tabular text-ink-tertiary">{amount(tile.by_currency[c], c, locale)}</span>
              ))}
              {tile.until && <span className="text-xs text-ink-tertiary">{t("fin.ov.until", { date: isoDate(tile.until, locale) })}</span>}
            </>
          ) : <None>{t("fin.ov.nothing_due")}</None>}
        </Row>
      );
    case "collections_open":
      return (
        <Row testid={`ov-${k}`} label={label} to={to} attention={n > 0}>
          {n > 0 ? <><Num tone="text-danger-fg">{t("fin.ov.cases_n", { n })}</Num><span className="tabular">{eur(tile.eur_cents, locale)}</span></>
            : <None>{t("fin.ov.no_cases")}</None>}
        </Row>
      );
    case "ar_overdue":
      return (
        <Row testid={`ov-${k}`} label={label} to={to} attention={n > 0}>
          {n > 0 ? <><Num tone="text-danger-fg">{t("fin.ov.invoices_n", { n })}</Num><span className="tabular">{eur(tile.eur_cents, locale)}</span></>
            : <None>{t("fin.ov.no_overdue")}</None>}
        </Row>
      );
    case "vat_current":
      return (
        <Row testid={`ov-${k}`} label={tile.period ? t("fin.ov.vat_period", { period: periodLabel(tile.period, locale) }) : label} to={to}>
          {tile.eur_cents === undefined || tile.eur_cents === null ? <None>—</None> : (
            <>
              <span className="text-ink-secondary">{tile.eur_cents < 0 ? t("fin.vat.refundable") : t("fin.vat.payable")}</span>
              <Num>{eur(Math.abs(tile.eur_cents), locale)}</Num>
              <span className="text-xs text-ink-tertiary">{t("fin.ov.estimate")}</span>
              {tile.verify && <span className="text-xs text-warning-fg">{t("fin.vat.verify")}</span>}
            </>
          )}
        </Row>
      );
    case "gross_profit_mtd":
      return (
        <Row testid={`ov-${k}`} label={label} to={to}>
          {tile.eur_cents === undefined || tile.eur_cents === null ? <None>—</None> : (
            <>
              <Num tone={tile.eur_cents < 0 ? "text-danger-fg" : undefined}>{eur(tile.eur_cents, locale)}</Num>
              <span className="tabular text-ink-secondary">{pctText(tile.margin_pct, locale)}</span>
              <span className="text-xs text-ink-tertiary tabular">
                {t("fin.ov.cost_split", { actual: eur(tile.cost_actual_cents, locale), accrued: eur(tile.cost_accrued_cents, locale) })}
              </span>
              {tile.shipments !== undefined && <span className="text-xs text-ink-tertiary">{t("fin.ov.shipments_n", { n: tile.shipments })}</span>}
            </>
          )}
        </Row>
      );
    case "open_accruals":
      return (
        <Row testid={`ov-${k}`} label={label} to={to}>
          {n > 0 ? <><span className="tabular">{t("fin.ov.accruals_n", { n })}</span><span className="tabular">{eur(tile.eur_cents, locale)}</span></>
            : <None>{t("fin.ov.no_accruals")}</None>}
        </Row>
      );
    case "fx":
      return (
        <Row testid={`ov-${k}`} label={label} to={to}>
          {tile.rate_date ? (
            <>
              <span className="tabular">{isoDate(String(tile.rate_date).slice(0, 10), locale)}</span>
              <span className="text-xs text-ink-tertiary">ECB</span>
              {tile.stale && <span className="text-xs text-warning-fg">{t("fin.fx.stale_short")}</span>}
            </>
          ) : <None>{t("fin.fx.no_rates")}</None>}
        </Row>
      );
    case "ledger_last_export":
      return (
        <Row testid={`ov-${k}`} label={label} to={to}>
          {tile.id ? (
            <>
              <span>{t(`fin.ledger.format.${tile.format}`)}</span>
              <span className="tabular text-ink-secondary">{isoDate(tile.from, locale)} – {isoDate(tile.to, locale)}</span>
              {tile.count !== undefined && tile.count !== null && <span className="text-xs text-ink-tertiary">{t("fin.ledger.entries_n", { n: tile.count })}</span>}
              {tile.at && <span className="text-xs text-ink-tertiary">{dateTime(tile.at, locale)}</span>}
            </>
          ) : <None>{t("fin.ledger.no_exports")}</None>}
        </Row>
      );
    case "autopilot":
      return (
        <Row testid={`ov-${k}`} label={label} to={to}>
          {tile.halted ? <span className="font-medium text-danger-fg">{t("fin.ov.autopilot_halted")}</span>
            : tile.enabled ? <span>{t("fin.ov.autopilot_on")}</span>
            : <span className="text-ink-secondary">{t("fin.ov.autopilot_off")}</span>}
        </Row>
      );
    default:
      return (
        <Row testid={`ov-${k}`} label={tile[`label_${locale}`] || tile.label_fi || k} to={to}>
          {n !== undefined && <span className="tabular">{n}</span>}
          {tile.eur_cents !== undefined && <span className="tabular">{eur(tile.eur_cents, locale)}</span>}
        </Row>
      );
  }
}

export default function Overview() {
  const { t } = useT();
  const q = useApi("/finance/overview", { pollMs: 60000 });
  const runs = useApi("/finance/payment-runs?status=pending_approval", { pollMs: 60000 });
  const runItems = runs.data ? runs.data.items || [] : runs.error ? null : null;
  return (
    <div className="max-w-[1152px]">
      <Loadable q={q} skeleton={<LoadingRows rows={10} />}>
        {(d) => {
          const tiles = d.tiles || {};
          const pendingRuns = (runItems || []).filter((r) => r.status === "pending_approval").length;
          const ci = tiles.carrier_invoices_attention?.count || 0;
          const col = tiles.collections_open?.count || 0;
          const waiting = ci + col + pendingRuns;
          const extra = (d.order || Object.keys(tiles)).filter((k) => !DECIDE.includes(k) && !BOOKS.includes(k));
          return (
            <>
              <FinBar title={t("fin.tab.overview")}
                meta={<>
                  <span className={waiting ? "text-foreground" : undefined}>{waiting > 0 ? t("fin.ov.waiting", { n: waiting }) : t("fin.ov.nothing_waiting")}</span>
                  <Dot />
                  <Freshness at={q.updatedAt} onRefresh={() => { q.reload(); runs.reload(); }} refreshing={q.refreshing} />
                </>} />
              <Section title={t("fin.ov.decide")}>
                {runs.error && <ErrorState compact className="mb-2" error={runs.error} onRetry={runs.reload} />}
                <ul className="divide-y divide-[color:var(--border-subtle)]">
                  {DECIDE.map((k) => <TileRow key={k} k={k} tile={tiles[k]} runs={runItems} />)}
                </ul>
              </Section>
              <Section title={t("fin.ov.books")}>
                <ul className="divide-y divide-[color:var(--border-subtle)]">
                  {[...BOOKS, ...extra].map((k) => <TileRow key={k} k={k} tile={tiles[k]} />)}
                </ul>
              </Section>
            </>
          );
        }}
      </Loadable>
    </div>
  );
}
