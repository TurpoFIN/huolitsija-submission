// Ops rate management (ux-benchmark 3.13). Decision: which lanes/rates expire soon or are already expired.
import React, { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Clock, Info, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/app/layout";
import { FilterBar, Segmented } from "@/components/app/FilterBar";
import { EmptyState, Freshness, Loadable } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { num } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { MODES, NativeSelect, useDebounced } from "@/features/quotes/ops/common";
import { RatesTable } from "@/features/quotes/ops/RatesTable";
import { RateSheet } from "@/features/quotes/ops/RateSheet";

import PublicSurcharges from "@/features/quotes/ops/PublicSurcharges";

const STATES = ["valid", "expiring", "expired", "upcoming"];

export default function Rates() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("rates.write");
  const [params, setParams] = useSearchParams();
  const mode = params.get("mode") || null;
  const carrierId = params.get("carrier") || null;
  const state = params.get("state") || null;
  const [search, setSearch] = useState(params.get("q") || "");
  const q = useDebounced(search.trim(), 250);
  const [sheet, setSheet] = useState({ open: false, rate: null });

  const setParam = (k, v) => {
    const next = new URLSearchParams(params);
    if (v === null || v === undefined || v === "") next.delete(k);
    else next.set(k, v);
    setParams(next, { replace: true });
  };

  const qs = new URLSearchParams();
  if (mode) qs.set("mode", mode);
  if (carrierId) qs.set("carrier_id", carrierId);
  if (q) qs.set("q", q);
  if (state) qs.set("state", state);
  qs.set("expiring_days", "21");
  const rates = useApi(`/rates?${qs.toString()}`);
  const carriers = useApi("/carriers");
  const carrierList = useMemo(() => carriers.data?.items || [], [carriers.data]);

  const counts = rates.data?.counts || {};
  const totalAll = STATES.reduce((a, s) => a + (counts[s] || 0), 0);
  const indicative = (rates.data?.items || []).some((r) => r.source === "indicative");

  const openNew = () => setSheet({ open: true, rate: null });
  const openEdit = (r) => setSheet({ open: true, rate: r });
  const filtered = !!(mode || carrierId || state || q);

  return (
    <div>
      <PageHeader
        title={t("nav.rates")}
        meta={<Freshness at={rates.updatedAt} onRefresh={rates.reload} refreshing={rates.refreshing} />}
        actions={canWrite && <Button onClick={openNew}><Plus />{t("oq.rates.add")}</Button>}
      />

      <PublicSurcharges />

      {rates.data && (
        <div className="mb-3 flex flex-col gap-1.5 text-sm">
          {(counts.expiring > 0 || counts.expired > 0) ? (
            <p className="flex flex-wrap items-center gap-x-3 gap-y-1">
              {counts.expiring > 0 && (
                <span className="inline-flex items-center gap-1.5 text-warning-fg">
                  <Clock className="size-4" aria-hidden />
                  {t("oq.rates.expiring_line", { n: num(counts.expiring, locale) })}
                </span>
              )}
              {counts.expiring > 0 && state !== "expiring" && (
                <button type="button" className="text-ink-accent hover:underline" onClick={() => setParam("state", "expiring")}>{t("oq.rates.show_them")}</button>
              )}
              {counts.expired > 0 && (
                <button type="button" className="text-ink-secondary hover:underline" onClick={() => setParam("state", "expired")}>
                  {t("oq.rates.expired_line", { n: num(counts.expired, locale) })}
                </button>
              )}
            </p>
          ) : totalAll > 0 ? (
            <p className="text-ink-secondary">{t("oq.rates.all_valid")}</p>
          ) : null}
          {indicative && (
            <p className="inline-flex items-center gap-1.5 text-xs text-ink-tertiary">
              <Info className="size-3.5" aria-hidden />{t("oq.rates.indicative_notice")}
            </p>
          )}
        </div>
      )}

      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Segmented label={t("oq.rates.col.validity")} value={state} onChange={(v) => setParam("state", v)}
          options={[{ value: null, label: t("oq.all"), count: rates.data ? totalAll : undefined },
            ...STATES.map((s) => ({ value: s, label: t(`oq.rate_state.${s}`), count: rates.data ? counts[s] || 0 : undefined }))]} />
        <NativeSelect className="w-full sm:ml-auto sm:w-56" aria-label={t("field.carrier")} value={carrierId} placeholder={t("oq.rates.all_carriers")}
          onChange={(v) => setParam("carrier", v)}
          options={carrierList.map((c) => ({ value: c.id, label: c.name }))} />
      </div>
      <FilterBar
        search={search}
        onSearch={(v) => { setSearch(v); setParam("q", v.trim()); }}
        placeholder={t("oq.rates.search")}
        filters={[
          { key: "mode", label: t("field.mode"), value: mode, onChange: (v) => setParam("mode", v),
            options: [{ value: null, label: t("oq.all") }, ...MODES.map((m) => ({ value: m, label: tEnum(t, "mode", m) }))] },
        ]}
      />

      <Loadable q={rates}>
        {(d) => (
          <>
            {rates.refreshing && <p className="mb-1 text-xs text-ink-tertiary" aria-live="polite">{t("oq.updating")}</p>}
            <RatesTable
              rows={d.items}
              onEdit={openEdit}
              empty={filtered ? (
                <EmptyState title={t("oq.rates.empty_filtered")} action={<Button variant="outline" size="sm" onClick={() => { setSearch(""); setParams(new URLSearchParams(), { replace: true }); }}>{t("oq.clear_filters")}</Button>} />
              ) : (
                <EmptyState title={t("oq.rates.empty")} body={t("oq.rates.empty_body")} action={canWrite && <Button size="sm" onClick={openNew}><Plus />{t("oq.rates.add")}</Button>} />
              )}
            />
            {d.items.length > 0 && <p className="mt-2 text-xs text-ink-tertiary">{t("oq.rates.shown", { n: num(d.items.length, locale), total: num(d.total ?? d.items.length, locale) })}</p>}
          </>
        )}
      </Loadable>

      <RateSheet
        open={sheet.open}
        onOpenChange={(o) => setSheet((s) => ({ ...s, open: o }))}
        rate={sheet.rate}
        carriers={carrierList}
        canWrite={canWrite}
        onSaved={() => rates.reload()}
        onArchived={() => rates.reload()}
      />
    </div>
  );
}
