import React, { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import {
  Area, Bar, BarChart, CartesianGrid, ComposedChart, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { PageHeader, Section } from "@/components/app/layout";
import { Segmented } from "@/components/app/FilterBar";
import { Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { date, money, num, pct } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const PERIODS = ["30", "90", "180", "365"];
const SERIES = "var(--chart-1)";
const GRID = "var(--border-subtle)";
const AXIS = { fontSize: 11, fill: "var(--text-tertiary)" };

function isoDay(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

/** Compact euro amounts for axes and tiles: 12,3 k€ / €12.3k. */
function eurShort(cents, locale) {
  if (cents === null || cents === undefined) return "—";
  const v = cents / 100;
  if (Math.abs(v) >= 1e6) return locale === "fi" ? `${num(v / 1e6, locale, 1)} M€` : `€${num(v / 1e6, locale, 1)}M`;
  if (Math.abs(v) >= 1e4) return locale === "fi" ? `${num(v / 1e3, locale, 0)} k€` : `€${num(v / 1e3, locale, 0)}k`;
  return money(cents, locale, { decimals: 0 });
}

function bucketLabel(start, interval, locale) {
  if (!start) return "";
  return interval === "month"
    ? date(start, locale, { month: "short", year: "2-digit" })
    : date(start, locale, { day: "numeric", month: "numeric" });
}

/** Change vs the previous period: arrow + signed value. Colour only says better/worse, the arrow and sign carry it too. */
function Delta({ cur, prev, kind, betterWhen = "up", unit }) {
  const { t, locale } = useT();
  if (cur === null || cur === undefined) return null;
  if (prev === null || prev === undefined) return <span className="text-xs text-ink-tertiary">{t("ins.no_prev")}</span>;
  const diff = cur - prev;
  const Icon = diff > 0 ? ArrowUpRight : diff < 0 ? ArrowDownRight : Minus;
  const good = diff === 0 ? null : (diff > 0) === (betterWhen === "up");
  let text;
  if (kind === "money") text = `${diff > 0 ? "+" : diff < 0 ? "−" : "±"}${eurShort(Math.abs(diff), locale)}`;
  else if (kind === "pp") text = `${diff > 0 ? "+" : diff < 0 ? "−" : "±"}${num(Math.abs(diff), locale, 1)} ${locale === "fi" ? "%-yks." : "pp"}`;
  else text = `${diff > 0 ? "+" : diff < 0 ? "−" : "±"}${num(Math.abs(diff), locale, kind === "days" ? 1 : 0)}${unit || ""}`;
  return (
    <span className={cn("inline-flex items-center gap-0.5 text-xs tabular", good === true && "text-success-fg", good === false && "text-danger-fg",
      good === null && "text-ink-tertiary")}>
      <Icon className="size-3.5" aria-hidden />{text}
    </span>
  );
}

function Kpi({ label, value, sub, delta, hidden }) {
  return (
    <div className="min-w-0 border-l border-[color:var(--border-subtle)] pl-3">
      <div className="truncate text-xs text-ink-tertiary">{label}</div>
      <div className={cn("mt-0.5 text-lg leading-7 tabular", hidden && "text-ink-tertiary")}>{value}</div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5">
        {delta}
        {sub && <span className="text-xs text-ink-tertiary">{sub}</span>}
      </div>
    </div>
  );
}

function ChartTip({ active, payload, label, render }) {
  if (!active || !payload?.length) return null;
  const rows = render(payload[0].payload, label);
  if (!rows) return null;
  return (
    <div className="rounded-md border border-[color:var(--border-default)] bg-surface px-2.5 py-1.5 text-xs shadow-sm">
      {rows.map(([k, v], i) => (
        <div key={i} className={cn("flex justify-between gap-4", i === 0 && "mb-0.5 font-medium")}>
          <span className={i === 0 ? "" : "text-ink-secondary"}>{k}</span>
          <span className="tabular">{v}</span>
        </div>
      ))}
    </div>
  );
}

function ForecastPanel({ f, metric, setMetric }) {
  const { t, locale } = useT();
  const money_ = metric === "revenue";
  const fc = money_ ? f.revenue : f.volume;
  const hist = money_ ? f.history.sell_cents : f.history.volume;
  const monthly = f.interval === "month";
  const keep = monthly ? 24 : 26;
  const data = useMemo(() => {
    const rows = [];
    const start = Math.max(0, f.history.buckets.length - keep);
    f.history.buckets.slice(start).forEach((b, i) => rows.push({ start: b, actual: hist[start + i] }));
    const pts = fc?.forecast || [];
    if (rows.length && pts.length) {
      const last = rows[rows.length - 1];
      last.forecast = last.actual;
      last.band = [last.actual, last.actual];
    }
    pts.forEach((p) => rows.push({ start: p.start, forecast: p.mean, band: [p.lo80, p.hi80], lo: p.lo80, hi: p.hi80 }));
    return rows;
  }, [f, fc, hist, keep]);
  const fmt = (v) => (v === null || v === undefined ? "—" : money_ ? eurShort(v, locale) : num(v, locale, v < 10 ? 1 : 0));
  const pts = fc?.forecast || [];
  const sum = (k) => pts.reduce((a, p) => a + (p[k] || 0), 0);
  const bt = fc?.backtest;
  const options = [{ value: "volume", label: t("ins.fc.metric.volume") }];
  if (f.financials_visible) options.push({ value: "revenue", label: t("ins.fc.metric.revenue") });

  return (
    <Section title={t("ins.fc.title")} actions={options.length > 1 && <Segmented value={metric} onChange={setMetric} options={options} label={t("ins.fc.title")} />}>
      {!f.history.buckets.length ? <p className="py-2 text-sm text-ink-tertiary">{t("ins.fc.empty")}</p> : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_260px]">
          <div className="min-w-0">
            <div className="h-[260px] w-full" role="img" aria-label={`${t("ins.fc.title")}: ${t(`ins.fc.metric.${metric}`)}`}>
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid vertical={false} stroke={GRID} />
                  <XAxis dataKey="start" tickFormatter={(v) => bucketLabel(v, f.interval, locale)} tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} minTickGap={24} />
                  <YAxis tickFormatter={fmt} tick={AXIS} tickLine={false} axisLine={false} width={money_ ? 56 : 32} allowDecimals={false} />
                  <Tooltip cursor={{ stroke: "var(--border-default)" }} content={<ChartTip render={(p) => [
                    [bucketLabel(p.start, f.interval, locale), ""],
                    ...(p.actual !== undefined ? [[t("ins.fc.actual"), fmt(p.actual)]] : []),
                    ...(p.lo !== undefined ? [[t("ins.fc.forecast"), fmt(p.forecast)], [t("ins.fc.band"), `${fmt(p.lo)}–${fmt(p.hi)}`]] : []),
                  ]} />} />
                  <Area dataKey="band" stroke="none" fill={SERIES} fillOpacity={0.12} isAnimationActive={false} name={t("ins.fc.band")} />
                  <Line dataKey="actual" stroke={SERIES} strokeWidth={2} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} name={t("ins.fc.actual")} />
                  <Line dataKey="forecast" stroke={SERIES} strokeWidth={2} strokeDasharray="5 4" dot={false} activeDot={{ r: 4 }} isAnimationActive={false} name={t("ins.fc.forecast")} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-secondary">
              <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-4 rounded" style={{ background: SERIES }} />{t("ins.fc.actual")}</span>
              <span className="inline-flex items-center gap-1.5"><span className="h-0 w-4 border-t-2 border-dashed" style={{ borderColor: SERIES }} />{t("ins.fc.forecast")}</span>
              <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-4 rounded-sm" style={{ background: SERIES, opacity: 0.18 }} />{t("ins.fc.band")}</span>
            </div>
          </div>
          <div className="text-sm">
            {fc?.status === "ok" ? (
              <>
                <div className="text-xs text-ink-tertiary">{t(monthly ? "ins.fc.next_month" : "ins.fc.next", { n: pts.length })}</div>
                <div className="mt-0.5 text-lg tabular">{fmt(sum("mean"))}</div>
                <div className="text-xs text-ink-secondary">{t("ins.fc.band_note")}</div>
                <dl className="mt-3 space-y-1.5 text-xs text-ink-secondary">
                  <div>{t(`ins.fc.method.${fc.method}`)} · {t(monthly ? "ins.fc.points_month" : "ins.fc.points", { n: fc.points_used })}</div>
                  {bt ? (
                    <div>
                      {t("ins.fc.selected_error", { selected: fmt((bt.selected || bt.model).mae), base: fmt(bt.baseline.mae), n: bt.origins, h: bt.horizon })}
                      {bt.model.mape_pct != null && bt.baseline.mape_pct != null && <div>{t("ins.fc.mape", { model: num(bt.model.mape_pct, locale, 1), base: num(bt.baseline.mape_pct, locale, 1) })}</div>}
                      {fc.selection === "baseline_lower_error" && <div>{t("ins.fc.baseline_selected")}</div>}
                    </div>
                  ) : <div>{t("ins.fc.backtest_none")}</div>}
                  {fc.validation === "limited_history" && <div className="text-warning-fg">{t("ins.fc.limited")}</div>}
                  <div>{t("ins.fc.validation_note")}</div>
                  {f.horizon < f.horizon_requested && <div>{t("ins.fc.capped", { n: f.horizon, req: f.horizon_requested })}</div>}
                </dl>
              </>
            ) : (
              <p className="text-ink-secondary">{fc?.status === "incomplete_history" ? t("ins.fc.incomplete") : t("ins.fc.insufficient", { n: fc?.points_used ?? 0, min: fc?.min_points ?? 8 })}</p>
            )}
            {f.data_quality?.demo_test_records > 0 && <p className="mt-2 text-xs text-warning-fg">{t("ins.fc.demo", { n: f.data_quality.demo_test_records, total: f.data_quality.records })}</p>}
          </div>
        </div>
      )}
    </Section>
  );
}

function MarginChart({ d }) {
  const { t, locale } = useT();
  const rows = d.series.map((b) => ({ ...b, m: b.margin_cents }));
  if (!rows.some((r) => r.m !== null && r.m !== undefined)) return <p className="py-2 text-sm text-ink-tertiary">{t("ins.margin.empty")}</p>;
  return (
    <div className="h-[200px] w-full" role="img" aria-label={t("ins.margin.title")}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }} barCategoryGap="30%">
          <CartesianGrid vertical={false} stroke={GRID} />
          <XAxis dataKey="start" tickFormatter={(v) => bucketLabel(v, d.interval, locale)} tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} minTickGap={16} />
          <YAxis tickFormatter={(v) => eurShort(v, locale)} tick={AXIS} tickLine={false} axisLine={false} width={56} />
          <ReferenceLine y={0} stroke="var(--border-default)" />
          <Tooltip cursor={{ fill: "var(--bg-subtle)" }} content={<ChartTip render={(p) => [
            [bucketLabel(p.start, d.interval, locale), p.partial ? t("ins.partial") : ""],
            [t("ins.k.margin"), money(p.m, locale, { decimals: 0 })], ["%", pct(p.margin_pct, locale)],
            [t("ins.col.sell"), money(p.booked_sell_cents, locale, { decimals: 0 })],
          ]} />} />
          <Bar dataKey="m" fill={SERIES} radius={[4, 4, 0, 0]} maxBarSize={28} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function OnTimeChart({ d }) {
  const { t, locale } = useT();
  const rows = d.series.map((b) => ({ ...b, v: b.on_time_pct }));
  if (!rows.some((r) => r.v !== null && r.v !== undefined)) return <p className="py-2 text-sm text-ink-tertiary">{t("ins.ontime.empty")}</p>;
  return (
    <div className="h-[200px] w-full" role="img" aria-label={t("ins.ontime.title")}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID} />
          <XAxis dataKey="start" tickFormatter={(v) => bucketLabel(v, d.interval, locale)} tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} minTickGap={16} />
          <YAxis domain={[0, 100]} ticks={[0, 50, 100]} tickFormatter={(v) => `${v} %`} tick={AXIS} tickLine={false} axisLine={false} width={40} />
          <Tooltip cursor={{ stroke: "var(--border-default)" }} content={<ChartTip render={(p) => [
            [bucketLabel(p.start, d.interval, locale), ""], [t("ins.k.on_time"), pct(p.v, locale)],
            [t("ins.fc.actual"), `${p.on_time}/${p.on_time + p.late}`],
          ]} />} />
          <Line dataKey="v" stroke={SERIES} strokeWidth={2} connectNulls dot={{ r: 3, strokeWidth: 0, fill: SERIES }} activeDot={{ r: 5 }} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function Row({ label, value, sub }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5">
      <dt className="text-sm text-ink-secondary">{label}</dt>
      <dd className="text-right text-sm tabular">{value}{sub && <span className="ml-2 text-xs text-ink-tertiary">{sub}</span>}</dd>
    </div>
  );
}

function SeriesTable({ d }) {
  const { t, locale } = useT();
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <thead><tr className="text-left text-xs text-ink-tertiary">
          {["period", "volume", "sell", "revenue", "margin", "on_time", "win"].map((c) => <th key={c} className={cn("py-1.5 font-normal", c !== "period" && "text-right")}>{t(`ins.col.${c}`)}</th>)}
        </tr></thead>
        <tbody className="divide-y divide-[color:var(--border-subtle)]">
          {d.series.map((b) => (
            <tr key={b.start}>
              <td className="py-1.5">{date(b.start, locale)}{b.partial && <span className="ml-1 text-xs text-ink-tertiary">({t("ins.partial")})</span>}</td>
              <td className="py-1.5 text-right tabular">{b.volume}</td>
              <td className="py-1.5 text-right tabular">{money(b.booked_sell_cents, locale, { decimals: 0 })}</td>
              <td className="py-1.5 text-right tabular">{money(b.revenue_cents, locale, { decimals: 0 })}</td>
              <td className="py-1.5 text-right tabular">{money(b.margin_cents, locale, { decimals: 0 })}</td>
              <td className="py-1.5 text-right tabular">{pct(b.on_time_pct, locale)}</td>
              <td className="py-1.5 text-right tabular">{pct(b.quote_win_rate_pct, locale)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Insights() {
  const { t, locale } = useT();
  const [params, setParams] = useSearchParams();
  const period = PERIODS.includes(params.get("days")) ? params.get("days") : "90";
  const interval = params.get("interval") === "month" ? "month" : "week";
  const [metric, setMetric] = useState("volume");
  const setParam = (k, v) => { const p = new URLSearchParams(params); p.set(k, v); setParams(p, { replace: true }); };
  const range = useMemo(() => {
    const to = new Date();
    const from = new Date(to);
    from.setDate(from.getDate() - (Number(period) - 1));
    return { from: isoDay(from), to: isoDay(to) };
  }, [period]);
  const q = useApi(`/insights/kpis?from=${range.from}&to=${range.to}&interval=${interval}`);
  const f = useApi(`/insights/forecast?interval=${interval}&horizon=${interval === "month" ? 6 : 12}`);

  return (
    <div>
      <PageHeader
        title={t("ins.title")}
        meta={<>
          <span className="tabular">{t("ins.range", { from: date(range.from, locale), to: date(range.to, locale) })}</span>
          <Freshness at={q.updatedAt} onRefresh={() => { q.reload(); f.reload(); }} refreshing={q.refreshing} />
        </>}
        actions={<>
          <Segmented label={t("ins.period")} value={period} onChange={(v) => setParam("days", v)}
            options={PERIODS.map((p) => ({ value: p, label: t(`ins.period.${p}`) }))} />
          <Segmented label={t("ins.interval")} value={interval} onChange={(v) => setParam("interval", v)}
            options={["week", "month"].map((p) => ({ value: p, label: t(`ins.interval.${p}`) }))} />
        </>}
      />
      <Loadable q={q} skeleton={<LoadingRows rows={8} />}>
        {(d) => {
          const k = d.kpis;
          const p = d.previous.kpis;
          const fin = d.financials_visible;
          const hasPrev = p.volume > 0 || p.invoices_issued > 0 || (p.quotes?.created || 0) > 0;
          const prev = (v) => (hasPrev ? v : null);
          return (
            <>
              <div className="mb-2 grid grid-cols-2 gap-x-4 gap-y-4 md:grid-cols-3 xl:grid-cols-6">
                <Kpi label={t("ins.k.volume")} value={num(k.volume, locale)}
                  delta={<Delta cur={k.volume} prev={prev(p.volume)} />}
                  sub={fin ? t("ins.sub.volume", { sell: eurShort(k.booked_sell_cents, locale) }) : null} />
                <Kpi label={t("ins.k.revenue")} hidden={!fin} value={fin ? eurShort(k.revenue_cents, locale) : "—"}
                  delta={fin && <Delta cur={k.revenue_cents} prev={prev(p.revenue_cents)} kind="money" />}
                  sub={fin ? t("ins.sub.revenue", { n: k.invoices_issued }) : null} />
                <Kpi label={t("ins.k.margin")} hidden={!fin || k.margin_cents === null}
                  value={fin && k.margin_cents !== null ? `${eurShort(k.margin_cents, locale)} · ${pct(k.margin_pct, locale)}` : "—"}
                  delta={fin && k.margin_pct !== null && <Delta cur={k.margin_pct} prev={prev(p.margin_pct)} kind="pp" />}
                  sub={fin ? (k.margin_coverage.known ? t("ins.sub.margin", k.margin_coverage) : t("ins.sub.margin_none")) : null} />
                <Kpi label={t("ins.k.on_time")} hidden={k.on_time_pct === null} value={pct(k.on_time_pct, locale)}
                  delta={<Delta cur={k.on_time_pct} prev={prev(p.on_time_pct)} kind="pp" />}
                  sub={k.on_time + k.late ? t("ins.sub.on_time", { on: k.on_time, all: k.on_time + k.late }) : t("ins.sub.on_time_none")} />
                <Kpi label={t("ins.k.win_rate")} hidden={k.quote_win_rate_pct === null} value={pct(k.quote_win_rate_pct, locale)}
                  delta={<Delta cur={k.quote_win_rate_pct} prev={prev(p.quote_win_rate_pct)} kind="pp" />}
                  sub={t("ins.sub.win_rate", k.quotes)} />
                <Kpi label={t("ins.k.ar_days")} hidden={!fin || k.ar_days === null}
                  value={fin && k.ar_days !== null ? t("ins.k.days", { n: num(k.ar_days, locale, 0) }) : "—"}
                  delta={fin && <Delta cur={k.ar_days} prev={prev(p.ar_days)} kind="days" betterWhen="down" />}
                  sub={fin ? t("ins.sub.ar", { open: eurShort(k.receivables_cents, locale), overdue: eurShort(k.overdue_cents, locale) }) : null} />
              </div>
              <p className="mb-5 text-xs text-ink-tertiary">
                {t("ins.vs_prev", { days: d.days })}
                {Object.keys(k.volume_by_mode).length > 0 && <> · {t("ins.mode")}: {Object.entries(k.volume_by_mode).map(([m, n]) => `${tEnum(t, "mode", m)} ${n}`).join(", ")}</>}
                {!fin && <> · {t("ins.hidden")}</>}
              </p>

              <Loadable q={f} skeleton={<LoadingRows rows={4} />}>
                {(fd) => <ForecastPanel f={fd} metric={fd.financials_visible ? metric : "volume"} setMetric={setMetric} />}
              </Loadable>

              <div className="grid gap-x-8 lg:grid-cols-2">
                {fin && <Section title={t("ins.margin.title")}><MarginChart d={d} /></Section>}
                <Section title={t("ins.ontime.title")}><OnTimeChart d={d} /></Section>
                <Section title={t("ins.quotes.title")}>
                  <dl className="divide-y divide-[color:var(--border-subtle)]">
                    <Row label={t("ins.q.created")} value={k.quotes.created} />
                    <Row label={t("ins.q.won")} value={k.quotes.won} sub={k.quote_win_rate_pct !== null ? pct(k.quote_win_rate_pct, locale) : null} />
                    <Row label={t("ins.q.lost")} value={k.quotes.lost} />
                    <Row label={t("ins.q.open")} value={k.quotes.open} />
                    {k.quotes.withdrawn > 0 && <Row label={t("ins.q.withdrawn")} value={k.quotes.withdrawn} />}
                  </dl>
                </Section>
                {fin && (
                  <Section title={t("ins.ar.title")}>
                    <dl className="divide-y divide-[color:var(--border-subtle)]">
                      <Row label={t("ins.ar.open")} value={money(k.receivables_cents, locale, { decimals: 0 })} sub={`${k.open_invoices}`} />
                      <Row label={t("ins.ar.overdue")} value={<span className={k.overdue_cents ? "text-danger-fg" : ""}>{money(k.overdue_cents, locale, { decimals: 0 })}</span>} sub={`${k.overdue_count}`} />
                      <Row label={t("ins.ar.dso")} value={k.ar_days !== null ? t("ins.k.days", { n: num(k.ar_days, locale, 1) }) : "—"} />
                      <Row label={t("ins.ar.pay")} value={k.avg_days_to_pay !== null ? t("ins.k.days", { n: num(k.avg_days_to_pay, locale, 1) }) : "—"} sub={t("ins.ar.paid", { n: k.paid_invoices })} />
                    </dl>
                  </Section>
                )}
              </div>

              <details className="mb-3 text-sm">
                <summary className="cursor-pointer text-ink-secondary">{t("ins.table")}</summary>
                <div className="mt-2"><SeriesTable d={d} /></div>
              </details>
              <details className="mb-6 text-sm">
                <summary className="cursor-pointer text-ink-secondary">{t("ins.defs")}</summary>
                <dl className="mt-2 grid gap-2 sm:grid-cols-2">
                  {Object.entries(d.definitions).map(([key, txt]) => (
                    <div key={key}><dt className="text-xs text-ink-tertiary">{t({ volume: "ins.k.volume", revenue: "ins.k.revenue", margin: "ins.k.margin", on_time: "ins.k.on_time", win_rate: "ins.k.win_rate", ar_days: "ins.k.ar_days" }[key])}</dt>
                      <dd className="text-ink-secondary">{txt[locale] || txt.en}</dd></div>
                  ))}
                </dl>
              </details>
            </>
          );
        }}
      </Loadable>
    </div>
  );
}
