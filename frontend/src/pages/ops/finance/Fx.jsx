// Ops › Finance › Currencies (Valuutat). Decision: "are the ECB reference rates current, and what does month-end
// revaluation of open foreign-currency receivables and payables book?" Rates are units per 1 EUR (ECB convention).
// APIs: GET /finance/fx/rates {date, source, fetched_at, latest{}, rates{}, history{}, currencies[]}, POST /finance/fx/refresh,
// GET /finance/fx/revaluations {items}, POST /finance/fx/revalue {as_of}.
import React, { useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { DateInput } from "@/components/ui/DateInput";
import { Section } from "@/components/app/layout";
import { Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime, num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { isoDate } from "@/features/billing/invoiceFormat";
import { ActionNote, ConfirmAction, Dot, FinBar } from "@/features/finance/kit";
import { FinTable } from "@/features/finance/Table";
import { amount, eur, pctText, periodLabel, periodRange, shiftPeriod, signedEur, todayHelsinki } from "@/features/finance/format";

const rate = (v, locale) => (v === null || v === undefined ? "—" : num(v, locale, 4));

function daysOld(d) {
  if (!d) return null;
  return Math.round((new Date(`${todayHelsinki()}T00:00:00Z`) - new Date(`${String(d).slice(0, 10)}T00:00:00Z`)) / 86400000);
}

function Revaluation({ r }) {
  const { t, locale } = useT();
  const [open, setOpen] = useState(false);
  const items = r.items || [];
  return (
    <li>
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
        className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-1 py-2 text-left hover:bg-hover">
        {open ? <ChevronDown className="size-4 text-ink-tertiary" aria-hidden /> : <ChevronRight className="size-4 text-ink-tertiary" aria-hidden />}
        <span className="w-36 text-sm font-medium">{periodLabel(r.period, locale)}</span>
        <span className="text-xs text-ink-tertiary tabular">{t("fin.fx.as_of", { date: isoDate(r.as_of, locale), rate: isoDate(r.rate_date, locale) })}</span>
        <span className="text-xs text-ink-tertiary">{t("fin.fx.items_n", { n: items.length })}</span>
        <span className="ml-auto flex items-baseline gap-3 text-sm tabular">
          <span className="text-ink-secondary">{t("fin.fx.gain")} {eur(r.gain_cents, locale)}</span>
          <span className="text-ink-secondary">{t("fin.fx.loss")} {eur(r.loss_cents, locale)}</span>
          <span className={cn("font-medium", (r.net_cents ?? 0) < 0 && "text-danger-fg")}>{signedEur(r.net_cents ?? (r.gain_cents || 0) - (r.loss_cents || 0), locale)}</span>
        </span>
      </button>
      {open && (
        <div className="pb-3 pl-6">
          {(r.missing_rates || []).length > 0 && <p className="mb-2 text-sm text-warning-fg">{t("fin.fx.missing_rates", { list: r.missing_rates.join(", ") })}</p>}
          {items.length === 0 ? <p className="text-sm text-ink-tertiary">{t("fin.fx.no_open_items")}</p> : (
            <FinTable rows={items} rowKey={(x) => `${x.type}-${x.id}`} columns={[
              { key: "type", header: t("fin.fx.side"), cell: (x) => t(`fin.fx.side_${x.type}`) },
              { key: "number", header: t("fin.col.document"), cell: (x) => <span className="whitespace-nowrap">{x.number || "—"}</span> },
              { key: "open_cents", header: t("fin.fx.open"), align: "right", cell: (x) => amount(x.open_cents, x.currency, locale) },
              { key: "rate", header: t("fin.fx.rate"), align: "right", hideBelow: "md", cell: (x) => rate(x.rate, locale) },
              { key: "booked_eur_cents", header: t("fin.fx.booked"), align: "right", hideBelow: "md", cell: (x) => eur(x.booked_eur_cents, locale) },
              { key: "revalued_eur_cents", header: t("fin.fx.revalued"), align: "right", cell: (x) => eur(x.revalued_eur_cents, locale) },
              { key: "diff_cents", header: t("fin.fx.diff"), align: "right", cell: (x) => <span className={cn(x.diff_cents < 0 && "text-danger-fg")}>{signedEur(x.diff_cents, locale)}</span> },
            ]} />
          )}
        </div>
      )}
    </li>
  );
}

export default function Fx() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("billing.write");
  const q = useApi("/finance/fx/rates");
  const rv = useApi("/finance/fx/revaluations");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null);
  const [showAll, setShowAll] = useState(false);
  const [revalue, setRevalue] = useState(false);
  const [asOf, setAsOf] = useState(() => periodRange(shiftPeriod(todayHelsinki().slice(0, 7), -1))[1]);

  const refresh = async () => {
    setBusy(true);
    setNote(null);
    try {
      const r = await apiPost("/finance/fx/refresh", { kind: "daily" });
      setNote({ text: t("fin.fx.refreshed", { date: isoDate(r?.latest, locale), n: r?.changed ?? 0 }) });
      q.reload();
    } catch (e) {
      setNote({ tone: "error", text: e.message });
    } finally {
      setBusy(false);
    }
  };
  const runRevalue = async () => {
    const r = await apiPost("/finance/fx/revalue", { as_of: asOf });
    setNote({ text: t("fin.fx.revalued_done", { period: periodLabel(r?.period, locale), net: signedEur(r?.net_cents, locale) }) });
    rv.reload();
    return true;
  };

  const age = daysOld(q.data?.date);
  return (
    <div className="max-w-[1152px]">
      <FinBar title={t("fin.tab.fx")}
        meta={q.data ? (
          <>
            {q.data.date ? <span>{t("fin.fx.latest", { date: isoDate(q.data.date, locale) })}</span> : <span className="text-warning-fg">{t("fin.fx.no_rates")}</span>}
            {age !== null && age > 4 && <span className="text-warning-fg">{t("fin.fx.stale", { n: age })}</span>}
            {q.data.fetched_at && <><Dot /><span className="text-xs text-ink-tertiary">{t("fin.fx.fetched", { at: dateTime(q.data.fetched_at, locale) })}</span></>}
            <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
          </>
        ) : null}
        actions={<Button size="sm" variant="outline" onClick={refresh} disabled={!canWrite || busy} title={!canWrite ? t("fin.no_permission") : undefined}>
          <RefreshCw className={cn(busy && "animate-spin")} /> {busy ? t("fin.fx.refreshing") : t("fin.fx.refresh")}
        </Button>} />
      <ActionNote note={note} onClose={() => setNote(null)} className="mb-3" />
      <Section title={t("fin.fx.rates")}>
        <Loadable q={q} skeleton={<LoadingRows rows={6} />}>
          {(d) => {
            const watched = d.currencies || Object.keys(d.latest || {});
            const rows = watched.map((c) => {
              const h = (d.history || {})[c] || [];
              const first = h.find((x) => x.rate !== null && x.rate !== undefined);
              const now = (d.latest || {})[c] ?? (d.rates || {})[c];
              return { c, rate: now, first: first?.rate ?? null, firstDate: first?.date ?? null,
                change: first?.rate && now ? ((now - first.rate) / first.rate) * 100 : null };
            });
            const others = Object.keys(d.rates || {}).filter((c) => !watched.includes(c)).sort();
            return (
              <>
                <FinTable rows={rows} rowKey="c" columns={[
                  { key: "c", header: t("fin.fx.currency"), cell: (r) => <span className="font-medium">{r.c}</span> },
                  { key: "rate", header: t("fin.fx.per_eur"), align: "right", cell: (r) => rate(r.rate, locale) },
                  { key: "eur", header: t("fin.fx.eur_per_unit"), align: "right", hideBelow: "md", cell: (r) => (r.rate ? num(1 / r.rate, locale, 4) : "—") },
                  { key: "change", header: t("fin.fx.change_30d"), align: "right", sort: (r) => r.change, headTitle: t("fin.fx.change_hint"),
                    cell: (r) => (r.change === null ? "—" : <span title={r.firstDate ? `${isoDate(r.firstDate, locale)}: ${rate(r.first, locale)}` : undefined}>{(r.change > 0 ? "+" : "") + pctText(r.change, locale, 2)}</span>) },
                ]} />
                <p className="mt-2 text-xs text-ink-tertiary">{t("fin.fx.source_note", { source: d.source || "ECB" })}</p>
                {others.length > 0 && (
                  <div className="mt-2">
                    <Button size="xs" variant="ghost" onClick={() => setShowAll(!showAll)} aria-expanded={showAll}>
                      {showAll ? <ChevronDown /> : <ChevronRight />} {t("fin.fx.all_rates", { n: others.length })}
                    </Button>
                    {showAll && (
                      <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4 lg:grid-cols-6">
                        {others.map((c) => (
                          <div key={c} className="flex justify-between gap-2"><dt className="text-ink-tertiary">{c}</dt><dd className="tabular">{rate(d.rates[c], locale)}</dd></div>
                        ))}
                      </dl>
                    )}
                  </div>
                )}
              </>
            );
          }}
        </Loadable>
      </Section>
      <Section title={t("fin.fx.revaluations")}
        actions={<Button size="xs" variant="outline" onClick={() => setRevalue(true)} disabled={!canWrite} title={!canWrite ? t("fin.no_permission") : undefined}>{t("fin.fx.revalue")}</Button>}>
        <Loadable q={rv} skeleton={<LoadingRows rows={3} />} isEmpty={(d) => (d.items || []).length === 0}
          empty={<p className="py-2 text-sm text-ink-tertiary">{t("fin.fx.no_revaluations")}</p>}>
          {(d) => <ul className="divide-y divide-[color:var(--border-subtle)]">{d.items.map((r) => <Revaluation key={r.id} r={r} />)}</ul>}
        </Loadable>
      </Section>
      <ConfirmAction open={revalue} onOpenChange={setRevalue} title={t("fin.fx.revalue")} description={t("fin.fx.revalue_body")}
        submitLabel={t("fin.fx.revalue")} onSubmit={() => (asOf ? runRevalue() : false)}>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="fx-asof">{t("fin.fx.as_of_label")}</Label>
          <DateInput id="fx-asof" value={asOf} onChange={setAsOf} max={todayHelsinki()} />
        </div>
      </ConfirmAction>
    </div>
  );
}
