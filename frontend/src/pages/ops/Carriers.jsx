// Ops carrier directory. Decision: which carriers can take work now (integration, DG, valid rates).
import React, { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Clock, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar } from "@/components/app/FilterBar";
import { EmptyState, Freshness, Loadable } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { num } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Dash, DgClasses, IntegrationBadge, MODES, MobileRow, ModeIcons, OnTime, RateCounts, isBlank, useDebounced, useIsNarrow } from "@/features/quotes/ops/common";
import { CarrierDialog } from "@/features/quotes/ops/CarrierDialog";

export default function Carriers() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const navigate = useNavigate();
  const narrow = useIsNarrow();
  const [params, setParams] = useSearchParams();
  const mode = params.get("mode") || null;
  const active = params.get("active") || null;
  const [search, setSearch] = useState(params.get("q") || "");
  const q = useDebounced(search.trim(), 250);
  const [adding, setAdding] = useState(false);

  const setParam = (k, v) => {
    const next = new URLSearchParams(params);
    if (v === null || v === undefined || v === "") next.delete(k);
    else next.set(k, v);
    setParams(next, { replace: true });
  };
  const qs = new URLSearchParams();
  if (mode) qs.set("mode", mode);
  if (active) qs.set("active", active);
  if (q) qs.set("q", q);
  const res = useApi(`/carriers${qs.toString() ? `?${qs}` : ""}`);
  const counts = res.data?.counts || {};

  const summary = useMemo(() => {
    const items = res.data?.items || [];
    return {
      live: items.filter((c) => c.integration?.status === "live").length,
      expiring: items.filter((c) => (c.stats?.expiring || 0) > 0).length,
      noValid: items.filter((c) => c.active !== false && (c.stats?.valid || 0) + (c.stats?.expiring || 0) === 0).length,
    };
  }, [res.data]);

  const columns = [
    { key: "name", header: t("field.name"), sort: (c) => c.name?.toLowerCase(),
      cell: (c) => (
        <span className="flex flex-col">
          <span className="font-medium">{c.name}</span>
          <span className="font-mono text-xs text-ink-tertiary">{c.code || "—"}</span>
        </span>
      ) },
    { key: "modes", header: t("oq.carrier.modes"), sortable: false, cell: (c) => <ModeIcons modes={c.modes} /> },
    { key: "country", header: t("oq.carrier.country"), cell: (c) => c.country || <Dash /> },
    { key: "integration", header: t("oq.carrier.integration"), sort: (c) => c.integration?.status,
      cell: (c) => (
        <span className="flex items-center gap-2 whitespace-nowrap">
          <IntegrationBadge status={c.integration?.status} />
          <span className="text-xs text-ink-tertiary">{c.integration?.type ? t(`oq.integration.${c.integration.type}`) : ""}</span>
        </span>
      ) },
    { key: "on_time", header: t("oq.carrier.on_time"), align: "right", sort: (c) => c.performance?.on_time_pct ?? c.on_time_pct,
      cell: (c) => <OnTime value={c.performance?.on_time_pct ?? c.on_time_pct} source={c.performance?.source} sample={c.performance?.sample_size} /> },
    { key: "co2", header: t("oq.carrier.co2_short"), align: "right", sort: (c) => c.co2_factor_g_tkm,
      cell: (c) => (isBlank(c.co2_factor_g_tkm) ? <Dash /> : num(c.co2_factor_g_tkm, locale, 1)) },
    { key: "dg", header: t("oq.carrier.dg"), sortable: false, cell: (c) => <DgClasses classes={c.dg_classes_accepted} /> },
    { key: "rates", header: t("oq.carrier.rate_cards"), sort: (c) => c.stats?.valid, cell: (c) => <RateCounts stats={c.stats} /> },
    { key: "active", header: t("oq.carrier.state"), sort: (c) => (c.active === false ? 1 : 0),
      cell: (c) => <span className={cn("text-sm", c.active === false ? "text-ink-tertiary" : "text-ink-secondary")}>{c.active === false ? t("oq.carrier.inactive") : t("oq.carrier.active_short")}</span> },
  ];

  return (
    <div>
      <PageHeader
        title={t("nav.carriers")}
        meta={res.data ? (
          <>
            <span>{t("oq.carriers.summary", { n: num(res.data.total ?? res.data.items.length, locale), live: num(summary.live, locale) })}</span>
            {summary.expiring > 0 && <span className="inline-flex items-center gap-1 text-warning-fg"><Clock className="size-3.5" aria-hidden />{t("oq.carriers.summary_expiring", { n: num(summary.expiring, locale) })}</span>}
            {summary.noValid > 0 && <span>{t("oq.carriers.summary_no_valid", { n: num(summary.noValid, locale) })}</span>}
            <Freshness at={res.updatedAt} onRefresh={res.reload} refreshing={res.refreshing} />
          </>
        ) : null}
        actions={can("rates.write") && <Button onClick={() => setAdding(true)}><Plus />{t("oq.carriers.add")}</Button>}
      />

      <FilterBar
        search={search}
        onSearch={(v) => { setSearch(v); setParam("q", v.trim()); }}
        placeholder={t("oq.carriers.search")}
        filters={[
          { key: "mode", label: t("field.mode"), value: mode, onChange: (v) => setParam("mode", v),
            options: [{ value: null, label: t("oq.all"), count: mode ? undefined : res.data?.total },
              ...MODES.map((m) => ({ value: m, label: tEnum(t, "mode", m), count: res.data ? counts[m] || 0 : undefined }))] },
          { key: "active", label: t("oq.carrier.state"), value: active, onChange: (v) => setParam("active", v),
            options: [{ value: null, label: t("oq.all") }, { value: "true", label: t("oq.carrier.active_short") }, { value: "false", label: t("oq.carrier.inactive") }] },
        ]}
      />

      <Loadable q={res} isEmpty={(d) => d.items.length === 0}
        empty={(mode || active || q) ? (
          <EmptyState title={t("oq.carriers.empty_filtered")} action={<Button size="sm" variant="outline" onClick={() => { setSearch(""); setParams(new URLSearchParams(), { replace: true }); }}>{t("oq.clear_filters")}</Button>} />
        ) : (
          <EmptyState title={t("oq.carriers.empty")} body={t("oq.carriers.empty_body")} action={can("rates.write") && <Button size="sm" onClick={() => setAdding(true)}><Plus />{t("oq.carriers.add")}</Button>} />
        )}>
        {(d) => narrow ? (
          <ul className="divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
            {d.items.map((c) => (
              <MobileRow key={c.id} onClick={() => navigate(`/ops/carriers/${c.id}`)} dim={c.active === false}
                primary={c.name}
                status={<IntegrationBadge status={c.integration?.status} />}
                secondary={`${(c.modes || []).map((m) => tEnum(t, "mode", m)).join(", ")} · ${c.code || "—"}${c.stats?.expiring ? ` · ${t("oq.carriers.expiring_n", { n: c.stats.expiring })}` : ""}`}
                right={isBlank(c.performance?.on_time_pct ?? c.on_time_pct) ? "—" : `${num(c.performance?.on_time_pct ?? c.on_time_pct, locale, 1)} %`}
                rightSub={c.stats ? `${num(c.stats.valid || 0, locale)} ${t("oq.carriers.valid")}` : null} />
            ))}
          </ul>
        ) : (
          <div className="rounded-xl border border-[color:var(--border-subtle)]">
            <DataTable columns={columns} rows={d.items} dense className="min-w-[1040px]"
              onRowClick={(c) => navigate(`/ops/carriers/${c.id}`)}
              rowClassName={(c) => cn(c.active === false && "opacity-60")} />
          </div>
        )}
      </Loadable>

      <CarrierDialog open={adding} onOpenChange={setAdding} carrier={null}
        onSaved={(c) => { res.reload(); if (c?.id) navigate(`/ops/carriers/${c.id}`); }} />
    </div>
  );
}
