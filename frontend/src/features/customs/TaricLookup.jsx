// Tullitariffi (TARIC) lookup: goods code + country + flow + date -> measures from the live EU TARIC consultation
// (backend services/taric.py). Decision first: what duty applies, which preference, what stops or conditions the goods.
import React, { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ChevronDown, ChevronRight, ExternalLink, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DateInput } from "@/components/ui/DateInput";
import { Segmented } from "@/components/app/FilterBar";
import { Section } from "@/components/app/layout";
import { EmptyState, ErrorState, LoadingRows } from "@/components/app/states";
import { StatusBadge } from "@/components/app/StatusBadge";
import { useApi } from "@/lib/api";
import { dateShort, dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { useIsNarrow } from "@/features/customs/common";
import { ApiMissing, goodsCode, isMissingApi } from "@/features/customs/extCommon";
import { QUOTA_STATE, bestPreference, dutyText, groupMeasures, legalUrl, thirdCountry, typeCode, validateLookup } from "@/features/customs/taricModel";

const todayIso = () => new Date().toISOString().slice(0, 10);

export function taricPath({ code, country, flow, date }) {
  const qs = new URLSearchParams();
  qs.set("code", code);
  if (country) qs.set("country", country);
  qs.set("flow", flow || "import");
  if (date) qs.set("date", date);
  return `/customs/taric?${qs}`;
}

function measureLabel(m, t) {
  const key = `cx.taric.type.${m.type_code}`;
  const nameKey = `cx.taric.name.${m.type}`;
  return t(key) !== key ? t(key) : t(nameKey) !== nameKey ? t(nameKey) : m.type || t("cx.missing");
}
function amountLabel(m, t) {
  return dutyText(m) || t(m.conditions?.length ? "cx.taric.conditional" : "cx.taric.no_amount");
}
function Mono({ children, className }) {
  return <span className={cn("font-mono text-[13px]", className)}>{children}</span>;
}

export function QuotaStatus({ order, date }) {
  const { t, locale } = useT();
  const q = useApi(`/customs/taric/quota/${encodeURIComponent(order)}${date ? `?date=${date}` : ""}`);
  if (q.loading && !q.data) return <p className="text-xs text-ink-tertiary">{t("cx.taric.quota.loading")}</p>;
  if (isMissingApi(q.error, { any404: true })) return <ApiMissing />;
  if (q.error && !q.data) return <ErrorState compact error={q.error} onRetry={q.reload} />;
  const d = q.data || {};
  const nf = (v) => (v === null || v === undefined ? null : `${Number(v).toLocaleString(locale === "fi" ? "fi-FI" : "en-GB")}${d.unit ? ` ${d.unit}` : ""}`);
  const rows = [
    [t("cx.taric.quota.balance"), nf(d.balance)],
    [t("cx.taric.quota.initial"), nf(d.initial_amount)],
    [t("cx.taric.quota.period"), d.period ? [d.period.start, d.period.end].filter(Boolean).map((x) => dateShort(x, locale)).join(" – ") : null],
    [t("cx.taric.quota.origins"), d.period?.origins],
    [t("cx.taric.quota.exhausted_on"), d.exhaustion_date ? dateShort(d.exhaustion_date, locale) : null],
  ].filter(([, v]) => v);
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge size="sm" state={QUOTA_STATE[d.status] || "expired"} label={t(`cx.taric.quota.st.${d.status || "unknown"}`)} />
        <span className="text-xs text-ink-tertiary">{t("cx.taric.quota", { order: d.order_number || order })}</span>
      </div>
      {rows.length > 0 && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-xs">
          {rows.map(([k, v]) => (
            <React.Fragment key={k}>
              <dt className="text-ink-tertiary">{k}</dt>
              <dd className="tabular">{v}</dd>
            </React.Fragment>
          ))}
        </dl>
      )}
      <p className="text-[11px] text-ink-tertiary">
        {d.source_url ? <a href={d.details_url || d.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-ink-accent hover:underline">
          {d.source || t("cx.open_source")} <ExternalLink className="size-3" aria-hidden /></a> : d.source}
        {d.fetched_at && <> · {t("cx.fetched", { time: dateTime(d.fetched_at, locale) })}</>}
      </p>
    </div>
  );
}

function Conditions({ conditions }) {
  const { t } = useT();
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-ink-tertiary">
            <th className="py-1 pr-3 font-normal">{t("cx.taric.cond.code")}</th>
            <th className="py-1 pr-3 font-normal">{t("cx.taric.cond.certificate")}</th>
            <th className="py-1 pr-3 font-normal">{t("cx.taric.cond.action")}</th>
          </tr>
        </thead>
        <tbody>
          {conditions.map((c, i) => (
            <tr key={i} className="border-t border-[color:var(--border-subtle)] align-top">
              <td className="py-1 pr-3 whitespace-nowrap font-mono">{c.code}{c.sequence !== undefined && c.sequence !== null ? ` ${c.sequence}` : ""}</td>
              <td className="py-1 pr-3">
                {c.certificate && <span className="mr-1 font-mono font-medium">{c.certificate}</span>}
                {c.requirement && <span className="mr-1 font-mono">{c.requirement}</span>}
                <span className="text-ink-secondary">{c.text || c.condition || (!c.certificate && !c.requirement ? "—" : "")}</span>
              </td>
              <td className={cn("py-1 pr-3", c.action_kind === "not_allowed" && "text-danger-fg")}>
                {["allowed", "not_allowed"].includes(c.action_kind) ? t(`cx.taric.action.${c.action_kind}`) : c.action || t("cx.missing")}{c.duty_expression && <span className="ml-1 font-mono">{c.duty_expression}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MeasureDetail({ m, date }) {
  const { t, locale } = useT();
  const [open, setOpen] = useState(false);
  const [quota, setQuota] = useState(false);
  const conds = m.conditions || [];
  const url = legalUrl(m);
  const certs = [...new Set(conds.map((c) => c.certificate).filter(Boolean))];
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        {m.regulation && (
          <span className="text-ink-tertiary">{t("cx.taric.col.legal")}{" "}
            {url ? <a href={url} target="_blank" rel="noreferrer" title={t("cx.taric.open_legal")} className="font-mono text-ink-accent hover:underline">{m.regulation}</a>
              : <span className="font-mono text-foreground">{m.regulation}</span>}
          </span>
        )}
        {(m.start || m.end) && (
          <span className="tabular text-ink-tertiary">{m.start ? dateShort(m.start, locale) : "…"} – {m.end ? dateShort(m.end, locale) : ""}</span>
        )}
        {conds.length > 0 && (
          <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="inline-flex items-center gap-1 text-ink-secondary hover:text-foreground">
            {open ? <ChevronDown className="size-3" aria-hidden /> : <ChevronRight className="size-3" aria-hidden />}
            {t("cx.taric.conditions_n", { n: conds.length })}
            {certs.length > 0 && <span className="font-mono text-ink-tertiary">· {certs.slice(0, 5).join(" ")}{certs.length > 5 ? " …" : ""}</span>}
          </button>
        )}
        {m.has_conditions && !conds.length && <span className="text-ink-tertiary">{t("cx.taric.conditions_unread")}</span>}
        {m.order_number && (
          <button type="button" onClick={() => setQuota(!quota)} aria-expanded={quota} className="inline-flex items-center gap-1 text-ink-secondary hover:text-foreground">
            {quota ? <ChevronDown className="size-3" aria-hidden /> : <ChevronRight className="size-3" aria-hidden />}
            {t("cx.taric.quota", { order: m.order_number })}
          </button>
        )}
        {(m.footnotes || []).length > 0 && (
          <span className="text-ink-tertiary" title={(m.footnotes || []).map((f) => (typeof f === "string" ? f : `${f.code || ""} ${f.text || ""}`)).join("\n")}>
            {t("cx.taric.col.footnotes")} <span className="font-mono">{(m.footnotes || []).map((f) => (typeof f === "string" ? f : f.code)).join(" ")}</span>
          </span>
        )}
      </div>
      {open && <div className="surface-sunken rounded-lg px-3 py-2"><Conditions conditions={conds} /></div>}
      {quota && <div className="surface-sunken rounded-lg px-3 py-2"><QuotaStatus order={m.order_number} date={date} /></div>}
    </div>
  );
}

function Geo({ m }) {
  const { t } = useT();
  return (
    <>
      <span className="block">{m.geo_name || t("cx.missing")} {m.geo_code && <Mono className="text-[11px] text-ink-tertiary">{m.geo_code}</Mono>}</span>
      {(m.excluded || []).length > 0 && (
        <span className="block text-[11px] text-ink-tertiary">{t("cx.taric.excluding")} {m.excluded.map((x) => x.code || x.name).join(", ")}</span>
      )}
    </>
  );
}

function AddCodes({ codes }) {
  if (!(codes || []).length) return <span className="text-ink-tertiary">—</span>;
  return (
    <ul className="flex flex-col gap-0.5">
      {codes.map((a, j) => (
        <li key={j} className="text-xs"><Mono className="text-[12px]">{typeof a === "string" ? a : a.code}</Mono>
          {typeof a === "object" && a.description && <span className="ml-1 text-ink-secondary">{a.description}</span>}</li>
      ))}
    </ul>
  );
}

function MeasureTable({ measures, date }) {
  const { t } = useT();
  const narrow = useIsNarrow();
  const key = (m, i) => m.sid || `${typeCode(m)}-${m.geo_code}-${i}`;
  if (narrow) {
    return (
      <ul className="divide-y divide-[color:var(--border-subtle)]">
        {measures.map((m, i) => (
          <li key={key(m, i)} className="flex flex-col gap-1 py-3">
            <span className="flex items-start justify-between gap-3">
              <span className="min-w-0 text-sm">{measureLabel(m, t)} <Mono className="text-[11px] text-ink-tertiary">{typeCode(m)}</Mono></span>
              <Mono className="shrink-0 text-right">{amountLabel(m, t)}</Mono>
            </span>
            <span className="text-xs text-ink-secondary"><Geo m={m} /></span>
            {(m.additional_codes || []).length > 0 && (
              <div className="text-xs"><span className="text-ink-tertiary">{t("cx.taric.col.addcodes")}</span><AddCodes codes={m.additional_codes} /></div>
            )}
            <MeasureDetail m={m} date={date} />
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="h-8 text-left text-xs text-ink-tertiary">
            <th className="pr-4 font-normal">{t("cx.taric.col.measure")}</th>
            <th className="pr-4 font-normal">{t("cx.taric.col.duty")}</th>
            <th className="pr-4 font-normal">{t("cx.taric.col.geo")}</th>
            <th className="pr-4 font-normal">{t("cx.taric.col.addcodes")}</th>
          </tr>
        </thead>
        <tbody>
          {measures.map((m, i) => (
            <tr key={key(m, i)} className="border-t border-[color:var(--border-subtle)] align-top">
              <td className="py-2 pr-4">
                <span className="block">{measureLabel(m, t)} <Mono className="text-[11px] text-ink-tertiary">{typeCode(m)}</Mono></span>
                <div className="mt-1"><MeasureDetail m={m} date={date} /></div>
              </td>
              <td className="py-2 pr-4 whitespace-nowrap"><Mono>{amountLabel(m, t)}</Mono></td>
              <td className="py-2 pr-4"><Geo m={m} /></td>
              <td className="py-2 pr-4 max-w-[360px]"><AddCodes codes={m.additional_codes} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Decision line above the measures: duty, best preference, trade defence, restrictions. */
export function TaricSummary({ data }) {
  const { t } = useT();
  const third = thirdCountry(data);
  const pref = bestPreference(data?.preferences);
  const defence = data?.trade_defence || [];
  const restr = data?.restrictions || [];
  const item = (label, value, tone) => (
    <div className="min-w-0">
      <dt className="text-xs text-ink-tertiary">{label}</dt>
      <dd className={cn("mt-0.5 text-md", tone === "warn" && "text-warning-fg")}>{value}</dd>
    </div>
  );
  const n = (list) => (list.length ? t("cx.taric.summary.n", { n: list.length }) : t("cx.taric.summary.none"));
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
      {item(t("cx.taric.summary.third"), third ? <Mono className="text-md">{dutyText(third) || "—"}</Mono> : t("cx.taric.summary.none"))}
      {item(t("cx.taric.summary.pref"), pref ? (
        <span><Mono className="text-md">{dutyText(pref) || "—"}</Mono> <span className="text-xs text-ink-tertiary">{pref.geo_name || pref.geo_code}</span></span>
      ) : t("cx.taric.summary.none"))}
      {item(t("cx.taric.summary.defence"), n(defence), defence.length ? "warn" : null)}
      {item(t("cx.taric.summary.restr"), n(restr), restr.length ? "warn" : null)}
    </dl>
  );
}

function Subcodes({ data, onPick }) {
  const { t } = useT();
  const sums = Object.fromEntries((data.subcode_summaries || []).map((s) => [s.code, s]));
  return (
    <Section title={t("cx.taric.subcodes")} actions={<span className="text-xs tabular text-ink-tertiary">{data.subcodes.length}</span>}>
      <p className="mb-2 text-sm text-ink-secondary">{t("cx.taric.not_declarable")}</p>
      <ul className="flex flex-wrap gap-2">
        {data.subcodes.map((c) => (
          <li key={c}>
            <button type="button" onClick={() => onPick(c)} className="surface-input inline-flex h-8 items-center gap-2 rounded-lg px-2.5 text-sm hover:bg-hover">
              <Mono>{goodsCode(c)}</Mono>
              {sums[c]?.third_country_duty && <span className="text-xs text-ink-tertiary">{dutyText(sums[c].third_country_duty)}</span>}
              {sums[c]?.trade_defence > 0 && <span className="text-xs text-warning-fg">{t("cx.taric.summary.defence")}</span>}
            </button>
          </li>
        ))}
      </ul>
    </Section>
  );
}

export function TaricResult({ data, onPickCode }) {
  const { t, locale } = useT();
  const groups = useMemo(() => groupMeasures(data?.measures), [data]);
  if (data.known_code === false) return <EmptyState title={t("cx.taric.unknown_code", { code: goodsCode(data.goods_code), date: dateShort(data.date, locale) })} />;
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-lg">
          <Mono className="text-lg">{goodsCode(data.goods_code)}</Mono>
          <span className="ml-2 text-sm text-ink-secondary">{[data.country, t(`cx.taric.flow.${data.flow}`), data.date ? dateShort(data.date, locale) : null].filter(Boolean).join(" · ")}</span>
          {data.supplementary_unit && <span className="ml-2 text-xs text-ink-tertiary">{t("cx.taric.sup_unit")} <Mono className="text-[12px]">{data.supplementary_unit}</Mono></span>}
        </p>
        <p className="text-xs text-ink-tertiary">
          {t("cx.source")}: {data.source_url ? (
            <a href={data.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-ink-accent hover:underline">
              {data.source || "EU TARIC"} <ExternalLink className="size-3" aria-hidden /></a>
          ) : (data.source || "—")}
          {data.fetched_at && <> · {t("cx.fetched", { time: dateTime(data.fetched_at, locale) })}</>}
          {data.cache?.hit && <> · {t("cx.cached")}</>}
        </p>
      </div>
      <div className="mb-6"><TaricSummary data={data} /></div>
      {!data.declarable && (data.subcodes || []).length > 1 && onPickCode && <Subcodes data={data} onPick={onPickCode} />}
      {groups.length === 0 ? (
        <EmptyState title={t("cx.taric.none")} />
      ) : groups.map((g) => (
        <Section key={g.key} title={t(`cx.taric.group.${g.key}`)} actions={<span className="text-xs tabular text-ink-tertiary">{g.measures.length}</span>}>
          <MeasureTable measures={g.measures} date={data.date} />
        </Section>
      ))}
    </div>
  );
}

/** Whole lookup: the form (kept in the URL so a lookup is linkable and survives reload) and the result. */
export default function TaricLookup() {
  const { t } = useT();
  const [params, setParams] = useSearchParams();
  const cur = { code: params.get("code") || "", country: params.get("country") || "", flow: params.get("flow") || "import", date: params.get("date") || todayIso() };
  const [form, setForm] = useState(cur);
  const [errors, setErrors] = useState({});
  useEffect(() => { setForm({ code: params.get("code") || "", country: params.get("country") || "", flow: params.get("flow") || "import", date: params.get("date") || todayIso() }); }, [params]);
  const valid = validateLookup(cur);
  const path = cur.code && valid.ok ? taricPath({ ...cur, code: valid.code, country: valid.country }) : null;
  const q = useApi(path, { enabled: !!path });

  const submit = (e) => {
    e.preventDefault();
    const v = validateLookup(form);
    setErrors(v.errors);
    if (!v.ok) return;
    const n = new URLSearchParams();
    n.set("code", v.code);
    if (v.country) n.set("country", v.country);
    n.set("flow", form.flow);
    if (form.date) n.set("date", form.date);
    setParams(n);
  };
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e?.target ? e.target.value : e }));

  return (
    <div>
      <form onSubmit={submit} aria-label={t("cx.taric.form")} className="mb-6 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-tertiary">{t("cx.taric.code")}</span>
          <Input value={form.code} onChange={set("code")} placeholder={t("cx.taric.code_ph")} inputMode="numeric" autoComplete="off"
            aria-invalid={errors.code ? "true" : undefined} className="w-44 font-mono" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-tertiary">{t("cx.taric.country")}</span>
          <Input value={form.country} onChange={(e) => set("country")(e.target.value.toUpperCase())} placeholder={t("cx.taric.country_ph")} maxLength={2}
            autoComplete="off" aria-invalid={errors.country ? "true" : undefined} className="w-20 font-mono uppercase" />
        </label>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-ink-tertiary">{t("cx.taric.flow")}</span>
          <Segmented value={form.flow} onChange={set("flow")} label={t("cx.taric.flow")}
            options={[{ value: "import", label: t("cx.taric.flow.import") }, { value: "export", label: t("cx.taric.flow.export") }]} />
        </div>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-tertiary">{t("cx.taric.date")}</span>
          <DateInput value={form.date} onChange={set("date")} className="w-40" />
        </label>
        <Button type="submit"><Search /> {t("cx.taric.search")}</Button>
        {(errors.code || errors.country) && (
          <p role="alert" className="w-full text-xs text-danger-fg">{[errors.code, errors.country].filter(Boolean).map((k) => t(k)).join(" ")}</p>
        )}
      </form>
      {!path ? (
        <EmptyState icon={Search} title={t("cx.taric.empty.title")} body={t("cx.taric.empty.body")} />
      ) : q.loading && !q.data ? (
        <div><p className="mb-2 text-sm text-ink-tertiary" aria-live="polite">{t("cx.taric.loading")}</p><LoadingRows rows={6} /></div>
      ) : isMissingApi(q.error, { any404: true }) ? (
        <ApiMissing />
      ) : q.error && !q.data ? (
        <ErrorState error={q.error} onRetry={q.reload} />
      ) : q.data ? (
        <>
          {q.error && <ErrorState className="mb-3" compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
          <TaricResult data={q.data} onPickCode={(c) => { const n = new URLSearchParams(params); n.set("code", c); setParams(n); }} />
        </>
      ) : null}
    </div>
  );
}
